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
V
