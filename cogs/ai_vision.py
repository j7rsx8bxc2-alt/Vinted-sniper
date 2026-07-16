"""
KI-Bilderkennung für den Listing-Bot – schaut sich die Fotos eines Artikels an
und schlägt Titel, Marke und eine Vinted-optimierte Beschreibung (inkl.
Hashtags) vor.

Nutzt die kostenlose OpenRouter API (kostenloser Auto-Router wählt automatisch
ein Gratis-Vision-Modell) – kein Kreditkarte nötig, Anmeldung per E-Mail,
Google oder GitHub. Ohne gesetzten OPENROUTER_API_KEY liefert
generate_listing() None zurück, der Listing-Bot fragt dann ganz normal
manuell ab.

Kostenlosen Key holen: siehe SETUP.md.
"""

import base64
import json
import logging
import re
from typing import Optional

import discord

from .openrouter_client import OpenRouterError, chat, is_enabled

log = logging.getLogger("ai-vision")
COLOR = 0x09B1BA

PROMPT = """Du bist ein Experte für Vintage-Kleidung-Resale auf Vinted.
Schau dir die Fotos dieses Artikels an und erstelle daraus einen Vinted-Listing-Vorschlag.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt in genau diesem Format (kein Markdown, kein Codeblock, kein Fließtext davor/danach):
{
  "title": "kurzer, knackiger Titel (max. 70 Zeichen, Marke falls erkennbar zuerst)",
  "brand": "erkannte Marke oder leerer String falls nicht erkennbar",
  "category": "kurzes Stichwort zur Kategorie, z.B. 'Hoodie', 'Jeans', 'Sneaker', 'Sneaker', 'Jacke'",
  "era": "vermutete Ära/Stil falls erkennbar, z.B. '90er', 'Y2K', '80er' – sonst leerer String",
  "description": "verkaufsfördernde, detaillierte Beschreibung auf Deutsch, 4-6 Sätze: Optik/Schnitt/Farbe, Styling-Tipp (wie man es kombinieren könnte), Zustand-Eindruck, kurzer Eindruck zur Ära/zum Vibe. Ehrlich, nichts erfinden was nicht auf den Fotos zu sehen ist. OHNE Hashtags am Ende – die fügt das System separat hinzu."
}

Wichtig: Erfinde keine Fakten die du aus den Fotos nicht erkennen kannst (z.B. Materialangaben) –
bleib bei dem was optisch erkennbar ist (Stil, Farbe, vermutete Marke/Ära, Zustand-Eindruck)."""

# ── Hashtag-Pool ──────────────────────────────────────────────────────────────
# Kuratierte, breit genutzte Vintage-/Resale-/Vinted-Hashtags. Wird mit
# marken- und kategoriespezifischen Tags kombiniert und an jede Beschreibung
# angehängt (siehe _build_hashtags).
CORE_HASHTAGS = [
    "vintage", "vintagestyle", "vintagefashion", "vintageclothing", "vintagefinds",
    "vintagelook", "vintagevibes", "vintagelovers", "secondhand", "secondhandfashion",
    "secondhandmode", "preloved", "thrifted", "thriftfind", "sustainablefashion",
    "slowfashion", "circularfashion", "kreislaufmode",
]
VINTED_HASHTAGS = [
    "vinted", "vintedfinds", "vintedseller", "vintedshop", "vintedgermany",
    "vintedcommunity", "vintedhaul", "vintedfashion",
]
ERA_HASHTAGS = {
    "y2k": ["y2k", "y2kfashion", "y2kstyle", "2000er", "2000erstyle"],
    "90er": ["90sstyle", "90svintage", "90sfashion", "90soutfit"],
    "80er": ["80sstyle", "80svintage", "80sfashion"],
    "": ["retro", "retrostyle", "oldschool"],
}
STYLE_HASHTAGS = [
    "streetwear", "streetwearstyle", "streetwearvintage", "outfitinspo",
    "styleinspo", "ootd", "fashionfinds", "closetcleanout",
]
# Feste Hashtags (Nischen-/Marken-Strategie des Shops) – werden IMMER
# zusätzlich zu den markenbasierten Tags oben angehängt, unabhängig von
# Marke/Kategorie/Ära des jeweiligen Artikels.
FIXED_HASHTAGS = [
    "marseillevintage", "trackjacket", "adidastrackjacket", "vintagetrackjacket",
    "marseilletrackjacket", "omjacketvintage", "nike", "nikevintage", "2000s",
    "2000erstyle", "y2k", "y2kstyle", "y2kfashion", "mensfashion", "adidasmenswear",
    "pashanim", "pashastyle", "pashanimstyle", "championsleague", "championsleaguevintage",
    "adidasfirebird", "firebirdjacke", "firebirdjacket", "firebirdadidas",
]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _build_hashtags(brand: str, category: str, era: str) -> str:
    tags: list[str] = []
    seen: set[str] = set()

    def add(tag: str):
        s = _slugify(tag)
        if s and s not in seen:
            seen.add(s)
            tags.append(s)

    # Feste Hashtags zuerst, damit sie garantiert immer mit dabei sind.
    for t in FIXED_HASHTAGS:
        add(t)

    if brand:
        add(brand)
        add(f"{brand}vintage")
    if category:
        add(category)
        add(f"vintage{category}")

    era_key = next((k for k in ERA_HASHTAGS if k and k in era.lower()), "")
    for t in ERA_HASHTAGS.get(era_key, ERA_HASHTAGS[""]):
        add(t)

    for t in CORE_HASHTAGS:
        add(t)
    for t in VINTED_HASHTAGS:
        add(t)
    for t in STYLE_HASHTAGS:
        add(t)

    return " ".join(f"#{t}" for t in tags)


class AIVisionError(Exception):
    pass


def _image_content(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


async def _chat_with_image_fallback(prompt: str, images: list[bytes], *, temperature: float) -> str:
    """Schickt Prompt + Bilder an OpenRouter. Manche der kostenlosen Auto-Router-Modelle
    haben Probleme mit mehreren Bildern in einer Anfrage (z.B. Fehler wie
    "Failed to apply prompt replacement for mm_items['image'][1]") – schlägt die
    Mehrbild-Anfrage fehl, wird automatisch EINMAL mit nur dem ersten Bild
    nachversucht, bevor aufgegeben wird."""
    content = [{"type": "text", "text": prompt}] + [_image_content(img) for img in images]
    messages = [{"role": "user", "content": content}]
    try:
        return await chat(messages, temperature=temperature)
    except OpenRouterError as e:
        if len(images) <= 1:
            raise
        log.warning(f"Mehrbild-Anfrage an OpenRouter fehlgeschlagen ({e}) – versuche nochmal mit nur einem Bild.")
        content_single = [{"type": "text", "text": prompt}, _image_content(images[0])]
        messages_single = [{"role": "user", "content": content_single}]
        return await chat(messages_single, temperature=temperature)


def _extract_json(text: str) -> dict:
    """Versucht das JSON-Objekt aus der Modell-Antwort zu extrahieren, auch wenn
    das Modell trotz Anweisung noch Text drumrum gepackt hat."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("Kein JSON-Objekt gefunden", text, 0)


async def generate_listing(images: list[bytes]) -> Optional[dict]:
    """Gibt {"title", "brand", "category", "description"} zurück, oder None
    wenn kein API-Key gesetzt ist. Wirft AIVisionError bei API-Fehlern."""
    if not is_enabled():
        return None
    if not images:
        raise AIVisionError("Keine Bilder übergeben.")

    try:
        text = await _chat_with_image_fallback(PROMPT, images[:4], temperature=0.4)
    except OpenRouterError as e:
        raise AIVisionError(str(e))

    try:
        result = _extract_json(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.error(f"Konnte OpenRouter-Antwort nicht parsen: {e} — Rohdaten: {text[:400]}")
        raise AIVisionError("Antwort der KI konnte nicht gelesen werden.")

    brand = str(result.get("brand", "")).strip()
    category = str(result.get("category", "")).strip()
    era = str(result.get("era", "")).strip()
    description = str(result.get("description", "")).strip()
    hashtags = _build_hashtags(brand, category, era)
    if hashtags:
        description = f"{description}\n\n{hashtags}"

    return {
        "title": str(result.get("title", "")).strip(),
        "brand": brand,
        "category": category,
        "description": description,
    }


# ── Foto-Check ────────────────────────────────────────────────────────────────
PHOTO_CHECK_PROMPT = """Du bist Experte für Verkaufsfotos auf Vinted (Vintage-Kleidung-Resale).
Bewerte die folgenden Fotos eines Artikels kritisch danach, wie gut sie zum Verkauf geeignet sind.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt in genau diesem Format (kein Markdown, kein Codeblock, kein Fließtext davor/danach):
{
  "score": <Zahl von 1 bis 10, wie verkaufsfördernd die Fotos insgesamt sind>,
  "positives": ["was an den Fotos schon gut ist, kurz und konkret", ...],
  "issues": ["was verbessert werden sollte, kurz und KONKRET (z.B. 'zu dunkel, ans Fenster stellen' statt nur 'schlechtes Licht')", ...],
  "fehlende_perspektiven": ["welche zusätzlichen Foto-Perspektiven fehlen, z.B. Nahaufnahme Etikett/Größe, Rückseite, Detail auf Beschädigungen – nur nennen falls wirklich erkennbar fehlend"]
}

Bewertungskriterien: Licht (natürliches Licht vs. Blitz/dunkel), Hintergrund (aufgeräumt/neutral vs.
ablenkend), Bildausschnitt (ganzer Artikel sichtbar, gerade, nicht verzerrt), Schärfe/Fokus, ob
Etikett/Größe/Material erkennbar ist, ob die Präsentation (Kleiderbügel/Model/flach liegend) hochwertig
wirkt. Sei konkret und ehrlich, keine generischen Plattitüden wie "mach bessere Fotos"."""


async def check_photo_quality(images: list[bytes]) -> Optional[dict]:
    """Gibt {"score", "positives", "issues", "fehlende_perspektiven"} zurück,
    oder None wenn kein API-Key gesetzt ist. Wirft AIVisionError bei API-Fehlern."""
    if not is_enabled():
        return None
    if not images:
        raise AIVisionError("Keine Bilder übergeben.")

    try:
        text = await _chat_with_image_fallback(PHOTO_CHECK_PROMPT, images[:4], temperature=0.3)
    except OpenRouterError as e:
        raise AIVisionError(str(e))

    try:
        result = _extract_json(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.error(f"Konnte OpenRouter-Antwort (Foto-Check) nicht parsen: {e} — Rohdaten: {text[:400]}")
        raise AIVisionError("Antwort der KI konnte nicht gelesen werden.")

    score_raw = result.get("score")
    try:
        score = round(float(score_raw))
    except (TypeError, ValueError):
        score = None

    return {
        "score": score,
        "positives": [str(p).strip() for p in result.get("positives", []) if str(p).strip()],
        "issues": [str(i).strip() for i in result.get("issues", []) if str(i).strip()],
        "fehlende_perspektiven": [
            str(p).strip() for p in result.get("fehlende_perspektiven", []) if str(p).strip()
        ],
    }


GARMENT_DESC_PROMPT = """Beschreibe das Kleidungsstück auf diesem Foto in EINEM knappen Satz auf
Englisch, für eine Bild-KI die es auf ein Model-Foto zieht. Nenne konkret: Farbe(n)/Muster, Schnitt
(z.B. crewneck sweater, striped, button-up shirt), und falls ein Logo oder Markenschriftzug sichtbar
ist, WO genau es sitzt und wie es aussieht (z.B. "small green embroidered logo on upper left chest").
Erfinde nichts, was nicht wirklich zu sehen ist. Antworte NUR mit dem einen Satz, kein Markdown, keine
Anführungszeichen, kein Text davor/danach."""


async def describe_garment(image_bytes: bytes) -> Optional[str]:
    """Kurze KI-Beschreibung des Kleidungsstücks (inkl. Logo-Position/-Aussehen falls
    erkennbar), damit Try-On-Modelle wie IDM-VTON Details wie Logos besser treffen statt
    nur einen generischen Platzhalter zu bekommen. Gibt None zurück wenn kein API-Key
    gesetzt ist oder die Anfrage fehlschlägt (Aufrufer sollte dann selbst einen
    Fallback-Text wie "clothing item" nutzen – Try-On soll daran nicht scheitern)."""
    if not is_enabled():
        return None
    try:
        content = [{"type": "text", "text": GARMENT_DESC_PROMPT}, _image_content(image_bytes)]
        text = await chat([{"role": "user", "content": content}], temperature=0.2)
    except OpenRouterError as e:
        log.warning(f"Garment-Beschreibung fehlgeschlagen, nutze Fallback: {e}")
        return None
    text = text.strip().strip('"').strip()
    return text or None


def build_photo_check_embed(result: dict) -> discord.Embed:
    score = result.get("score")
    if isinstance(score, (int, float)):
        emoji = "🟢" if score >= 8 else "🟡" if score >= 5 else "🔴"
        titel = f"{emoji} Foto-Check: {score}/10"
    else:
        titel = "📸 Foto-Check"
    embed = discord.Embed(title=titel, color=COLOR)
    if result.get("positives"):
        embed.add_field(
            name="✅ Gut so", value="\n".join(f"• {p}" for p in result["positives"][:5]), inline=False
        )
    if result.get("issues"):
        embed.add_field(
            name="⚠️ Verbessern", value="\n".join(f"• {i}" for i in result["issues"][:5]), inline=False
        )
    if result.get("fehlende_perspektiven"):
        embed.add_field(
            name="📷 Fehlt noch",
            value="\n".join(f"• {p}" for p in result["fehlende_perspektiven"][:5]),
            inline=False,
        )
    return embed
