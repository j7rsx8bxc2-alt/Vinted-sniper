"""
Content-Ideen-Bot – generiert automatisch TikTok-Content-Ideen (Hook,
Skript-Gliederung, Hashtags) für den eigenen Reselling-Account, basierend auf
echten Daten aus dem Geschäft: aktuellem Lagerbestand, letzten Verkäufen und
(sobald genug Historie da ist) den am schnellsten verkauften Marken aus dem
Trend-Radar. Keine generischen Ideen von der Stange, sondern welche, die zu
den echten Funden/Verkäufen passen.

Commands:
  !content-ideen [anzahl]   – sofort N Content-Ideen generieren (Standard 5)
  !content-hilfe            – Übersicht

Automatischer Post:
  Jeden Montag (Standard 9 Uhr, Europe/Berlin) frische Ideen in
  CONTENT_CHANNEL_NAME, damit nie Leere-Kopf-Problem beim Content-Planen.

Konfiguration:
  CONTENT_CHANNEL_NAME  – Kanal für den automatischen Wochen-Post (Standard: "content-ideen")
  CONTENT_WEEKLY_HOUR   – Uhrzeit (Stunde, Europe/Berlin) fürs Wochen-Update (Standard: 9)
"""

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from .access import is_vip_or_admin
from .openrouter_client import OpenRouterError, chat, is_enabled

log = logging.getLogger("content")

COLOR = 0xFF3366
CONTENT_CHANNEL_NAME = os.getenv("CONTENT_CHANNEL_NAME", "content-ideen")
BERLIN_TZ = ZoneInfo("Europe/Berlin")
CONTENT_WEEKLY_HOUR = int(os.getenv("CONTENT_WEEKLY_HOUR", "9"))
WEEKLY_TIME = dtime(hour=CONTENT_WEEKLY_HOUR, minute=0, tzinfo=BERLIN_TZ)
MONDAY = 0  # datetime.weekday(): Montag=0


CONTENT_PROMPT = """Du bist Social-Media-Stratege für einen deutschen Vintage-/Streetwear-Reseller auf TikTok
(Content rund um Vinted-Sourcing, Flips und Vintage-Fashion).

Hier ist der aktuelle Kontext aus seinem echten Geschäft:

{kontext}

Erstelle {anzahl} konkrete, sofort umsetzbare TikTok-Video-Ideen für diese Woche. Jede Idee braucht:
- Einen knackigen Hook-Satz für die ersten 3 Sekunden (Deutsch, umgangssprachlich, TikTok-Stil, keine Floskeln)
- Eine kurze Skript-Gliederung (3-5 Stichpunkte, was im Video passiert, in der Reihenfolge)
- 5-8 passende Hashtags (ohne #, nur die Wörter)

Die Ideen sollen sich echt und konkret anfühlen (basierend auf den echten Funden/Verkäufen oben), nicht
generisch. Variiere die Formate (z.B. Unboxing, "Was ich für X bezahlt habe vs. verkauft", Storytime,
Trend-Erklärung, Behind-the-scenes beim Sourcen).

Antworte AUSSCHLIESSLICH mit einem JSON-Array in genau diesem Format (kein Markdown, kein Codeblock, kein
Text davor/danach):
[
  {{"hook": "kurzer Hook-Satz", "skript": ["Punkt 1", "Punkt 2", "Punkt 3"], "hashtags": ["tag1", "tag2"]}}
]"""


def _extract_json_array(text: str) -> list:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("Kein JSON-Array gefunden", text, 0)


def _stock_items(limit: int = 8) -> list[str]:
    """Aktueller Lagerbestand (noch nicht verkauft) — gutes Content-Material
    für 'Check das mal an'-Videos."""
    db_path = os.getenv("BUCHHALTUNG_DB", "buchhaltung.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name FROM artikel WHERE status = 'lager' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [r["name"] for r in rows]
    except sqlite3.Error:
        return []


def _recent_sales(limit: int = 5) -> list[dict]:
    """Zuletzt verkaufte Artikel — gutes Material für 'Storytime'/Flex-Content."""
    db_path = os.getenv("BUCHHALTUNG_DB", "buchhaltung.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT name, kauf_datum, verkauf_datum
            FROM artikel WHERE status = 'verkauft' ORDER BY verkauf_datum DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    result = []
    for r in rows:
        entry = {"name": r["name"]}
        try:
            tage = (datetime.fromisoformat(r["verkauf_datum"]) - datetime.fromisoformat(r["kauf_datum"])).days
            entry["tage"] = tage
        except (ValueError, TypeError, KeyError):
            pass
        result.append(entry)
    return result


def _hot_brands(limit: int = 5) -> list[dict]:
    """Best-effort: schnellste Verkaufsgeschwindigkeit aus dem Trend-Radar,
    falls schon genug Historie da ist. Liefert leere Liste statt Fehler,
    wenn das Trend-Radar (noch) keine Daten hat oder nicht geladen ist —
    das darf die Content-Ideen nie blockieren."""
    try:
        from .trends import _top_velocity
        return _top_velocity(limit=limit)
    except Exception as e:
        log.debug(f"Konnte Trend-Radar-Daten nicht laden: {e}")
        return []


def _build_context() -> str:
    parts = []
    stock = _stock_items()
    if stock:
        parts.append("Aktueller Lagerbestand (noch nicht verkauft): " + ", ".join(stock))
    sales = _recent_sales()
    if sales:
        lines = []
        for s in sales:
            tage = f" (in {s['tage']} Tagen verkauft)" if "tage" in s else ""
            lines.append(f"{s['name']}{tage}")
        parts.append("Zuletzt verkauft: " + "; ".join(lines))
    hot = _hot_brands()
    if hot:
        lines = [f"{h['term']} (⌀ {h['avg_days']:.1f} Tage bis verkauft)" for h in hot]
        parts.append("Aktuell am schnellsten verkaufte Marken laut Trend-Radar: " + "; ".join(lines))
    if not parts:
        parts.append(
            "Noch keine Geschäftsdaten vorhanden — nutz allgemeines Wissen über "
            "Vintage-/Streetwear-Reselling auf Vinted für die Ideen."
        )
    return "\n".join(parts)


async def generate_content_ideas(anzahl: int = 5) -> list[dict]:
    """Gibt eine Liste von {"hook", "skript", "hashtags"} zurück, oder eine
    leere Liste wenn kein API-Key gesetzt ist. Wirft OpenRouterError bei
    API-Fehlern (Aufrufer entscheidet, wie er das anzeigt)."""
    if not is_enabled():
        return []
    kontext = _build_context()
    prompt = CONTENT_PROMPT.format(kontext=kontext, anzahl=anzahl)
    text = await chat([{"role": "user", "content": prompt}], temperature=0.8)
    try:
        ideas = _extract_json_array(text)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error(f"Konnte Content-Ideen-Antwort nicht parsen: {e} — Rohdaten: {text[:400]}")
        raise OpenRouterError("Antwort der KI konnte nicht gelesen werden.")

    cleaned = []
    for idea in ideas[:anzahl]:
        cleaned.append({
            "hook": str(idea.get("hook", "")).strip(),
            "skript": [str(s).strip() for s in idea.get("skript", []) if str(s).strip()],
            "hashtags": [str(h).strip().lstrip("#") for h in idea.get("hashtags", []) if str(h).strip()],
        })
    return cleaned


def _build_content_embeds(ideas: list[dict]) -> list[discord.Embed]:
    embeds = []
    for i, idea in enumerate(ideas, 1):
        title = f"🎬 Idee {i}: {idea['hook']}" if idea["hook"] else f"🎬 Idee {i}"
        # Discord erlaubt max. 256 Zeichen im Embed-Titel.
        if len(title) > 256:
            title = title[:253] + "…"
        embed = discord.Embed(title=title, color=COLOR)
        if idea["skript"]:
            embed.add_field(
                name="📝 Skript-Gliederung",
                value="\n".join(f"{j+1}. {s}" for j, s in enumerate(idea["skript"])),
                inline=False,
            )
        if idea["hashtags"]:
            embed.add_field(
                name="🏷️ Hashtags",
                value=" ".join(f"#{h}" for h in idea["hashtags"]),
                inline=False,
            )
        embeds.append(embed)
    return embeds


class Content(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_content_loop.start()

    def cog_unload(self):
        self.weekly_content_loop.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    @commands.command(name="content-ideen")
    async def content_ideen(self, ctx: commands.Context, anzahl: int = 5):
        """!content-ideen [anzahl] – generiert sofort N TikTok-Content-Ideen (Standard 5)"""
        anzahl = max(1, min(anzahl, 8))
        async with ctx.typing():
            try:
                ideas = await generate_content_ideas(anzahl)
            except OpenRouterError as e:
                await ctx.send(f"❌ Content-Ideen fehlgeschlagen: `{e}`")
                return
        if not ideas:
            await ctx.send(
                "⚠️ Kein OpenRouter-Key gesetzt oder keine Ideen zurückbekommen — "
                "siehe SETUP.md für die KI-Konfiguration."
            )
            return
        await ctx.send(f"🎬 **{len(ideas)} Content-Ideen für dich:**")
        for embed in _build_content_embeds(ideas):
            await ctx.send(embed=embed)

    @commands.command(name="content-hilfe")
    async def content_hilfe(self, ctx: commands.Context):
        """!content-hilfe – Übersicht Content-Ideen-Bot"""
        embed = discord.Embed(
            title="🎬 Content-Ideen-Bot – Hilfe",
            description="Generiert TikTok-Video-Ideen (Hook, Skript, Hashtags) basierend auf deinem "
                        "echten Lagerbestand, deinen letzten Verkäufen und dem Trend-Radar — kein "
                        "Setup nötig, läuft mit den gleichen Daten wie Buchhaltung und Trend-Radar.",
            color=COLOR,
        )
        embed.add_field(name="Sofort Ideen generieren", value="`!content-ideen [anzahl]` (Standard 5)", inline=False)
        embed.set_footer(text=f"Automatisch: montags {CONTENT_WEEKLY_HOUR} Uhr in #{CONTENT_CHANNEL_NAME}")
        await ctx.send(embed=embed)

    @tasks.loop(time=WEEKLY_TIME)
    async def weekly_content_loop(self):
        if datetime.now(BERLIN_TZ).weekday() != MONDAY:
            return
        try:
            ideas = await generate_content_ideas(5)
        except Exception:
            log.exception("Fehler bei automatischer Content-Ideen-Generierung")
            return
        if not ideas:
            return
        channel = discord.utils.get(self.bot.get_all_channels(), name=CONTENT_CHANNEL_NAME)
        if not channel:
            log.warning(f"Content-Update: Kanal '{CONTENT_CHANNEL_NAME}' nicht gefunden.")
            return
        await channel.send("🎬 **Deine Content-Ideen für diese Woche:**")
        for embed in _build_content_embeds(ideas):
            await channel.send(embed=embed)

    @weekly_content_loop.before_loop
    async def before_weekly_content_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Content(bot))
