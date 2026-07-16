"""
Kanal-Hilfe – postet (und pinnt) eine kurze Erklärung, was man im jeweiligen
Kanal tun kann. Gedacht für neue VIP-Mitglieder, die nicht wissen, wo sie
welchen Command benutzen sollen.

Command:
  !kanal-hilfe   – erkennt den aktuellen Kanalnamen und postet die passende
                   Anleitung (Fallback: Übersicht aller Bot-Befehle)
"""

import discord
from discord.ext import commands

COLOR = 0x09B1BA

# Reihenfolge ist wichtig: erster passender Treffer (Substring im Kanalnamen)
# gewinnt. "sold" vor "coach" prüfen, falls ein Kanal z.B. "sold-coach" heißt.
CHANNEL_GUIDES = [
    ("sold", "👀 Verkauft-Erkennung", [
        "Hier meldet sich der Bot automatisch, wenn ein mit `!vinted-link` "
        "verknüpfter Artikel auf Vinted nicht mehr auffindbar ist (vermutlich verkauft).",
        "`!vinted-link <buchhaltungs-id> <vinted-url-oder-id>` – Artikel verknüpfen "
        "(meist in #verkauf oder #buchhaltung ausgeführt, nachdem du manuell auf Vinted eingestellt hast)",
        "`!verkauft-check` – manuell sofort prüfen statt auf den Auto-Check zu warten",
    ]),
    ("foto", "📸 Foto-Check", [
        "`!fotocheck` – schick 1-4 Fotos, die KI bewertet sie mit Score (1-10) und konkreten Tipps "
        "(Licht, Hintergrund, Bildausschnitt, fehlende Perspektiven).",
        "Läuft automatisch auch als Zwischenschritt in `!inserat` im #verkauf-Kanal.",
    ]),
    ("try", "🧍 Virtual Try-On", [
        "`!tryon` – zieht ein fotografiertes Kleidungsstück per KI auf ein Model-Foto (experimentell, "
        "kostenlose Community-KI, kann 30-90s dauern und mal ausfallen).",
        "💡 Ist eine ungefähre Stil-Vorschau, kein exaktes Abbild — Logos/Details können abweichen. "
        "Bei logo-relevanten Artikeln zusätzlich das echte Produktfoto posten.",
        "`!tryon eigenes` – erzwingt die manuelle Abfrage mit eigenem Model-Foto.",
        "`!tryon-modelle` – zeigt wie viele Model-Fotos hinterlegt sind.",
    ]),
    ("verkauf", "🛍️ Verkauf / Listings", [
        "`!inserat` – neues Verkaufs-Listing erstellen (Fotos schicken, KI schlägt Titel/Beschreibung vor, "
        "bewertet die Fotos automatisch mit)",
        "`!fotocheck` – Fotos einzeln von der KI bewerten lassen (Score + Tipps), auch außerhalb von `!inserat`",
        "`!vinted-link <id> <url>` – Inserat mit Vinted verknüpfen, damit der Bot Verkäufe erkennt",
    ]),
    ("buchhaltung", "🧾 Buchhaltung", [
        "`!kauf <name> <preis>` – Einkauf erfassen",
        "`!verkauf <id> <preis>` – Verkauf erfassen",
        "`!lager` – offene Artikel anzeigen",
        "`!bilanz` – Gesamtübersicht (Umsatz, Gewinn, Lagerwert)",
        "`!buchhaltung` – alle Buchhaltungs-Befehle im Detail",
    ]),
    ("preis", "💶 Preis-Check", [
        "`!preischeck <Suchbegriff>` – zeigt Preisspanne (Min/Median/Max) ähnlicher aktueller Vinted-Angebote.",
        "Beispiel: `!preischeck ralph lauren strickpullover`",
    ]),
    ("trend", "📡 Trend-Radar", [
        "Läuft automatisch über deine Snipe-Bot-Kanäle (kein Eintragen nötig): täglich morgens "
        "und sonntags ein Recap, hier in diesem Kanal — inkl. Abgleich mit deinen echten "
        "Verkäufen aus der Buchhaltung.",
        "`!trends <Suchbegriff>` – Ad-hoc-Check für einen beliebigen Begriff.",
        "`!trends-add <Suchbegriff>` – zusätzlichen Begriff mittracken (optional, über die "
        "Snipe-Bot-Kanäle hinaus).",
        "`!trends-liste` – zeigt was aktuell getrackt wird, `!trends-hilfe` alle Befehle im Detail.",
    ]),
    ("coach", "🎓 Reselling-Coach", [
        "`!coach <Frage>` – stell der KI jede Frage rund ums Vintage-Reselling "
        "(Fotografie, Preisstrategie, Verhandeln, Einkaufsstrategie, ...).",
        "`!coach-reset` – vergisst den bisherigen Gesprächsverlauf mit dir.",
    ]),
]

FALLBACK_TITLE = "🤖 Bot-Befehle in diesem Server"
FALLBACK_LINES = [
    "`!inserat` – neues Verkaufs-Listing erstellen",
    "`!fotocheck` – Fotos von der KI bewerten lassen",
    "`!tryon` – Kleidungsstück auf ein Model-Foto ziehen",
    "`!kauf` / `!verkauf` / `!lager` / `!bilanz` – Buchhaltung",
    "`!vinted-link` / `!verkauft-check` – Verkauft-Erkennung",
    "`!preischeck <Suchbegriff>` – Preisspanne ähnlicher Angebote",
    "`!trends <Suchbegriff>` – Trend-Radar",
    "`!coach <Frage>` – Reselling-Coach",
    "`!help` – alle Sniper-Befehle",
]


def _match_guide(channel_name: str):
    name = channel_name.lower()
    for keyword, title, lines in CHANNEL_GUIDES:
        if keyword in name:
            return title, lines
    return None


class ChannelHelp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="kanal-hilfe")
    async def kanal_hilfe(self, ctx: commands.Context):
        """!kanal-hilfe – postet (und pinnt) eine Erklärung für diesen Kanal"""
        guide = _match_guide(ctx.channel.name) if ctx.guild else None
        if guide:
            title, lines = guide
            description = "Was du hier machen kannst:"
        else:
            title, lines = FALLBACK_TITLE, FALLBACK_LINES
            description = "Konnte den Kanal nicht eindeutig zuordnen — hier die komplette Übersicht:"

        embed = discord.Embed(title=title, description=description, color=COLOR)
        embed.add_field(name="Befehle", value="\n".join(lines), inline=False)
        embed.set_footer(text="Angepinnt, damit neue Mitglieder es sofort sehen • !kanal-hilfe zum Neu-Posten")

        msg = await ctx.send(embed=embed)
        try:
            await msg.pin()
        except discord.Forbidden:
            await ctx.send(
                "ℹ️ Konnte die Nachricht nicht anpinnen (Bot fehlt die Berechtigung "
                "\"Nachrichten verwalten\" in diesem Kanal) — die Hilfe steht aber oben."
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelHelp(bot))
