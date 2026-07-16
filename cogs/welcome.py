"""
Begrüßung neuer Mitglieder – postet automatisch eine Willkommensnachricht im
"chat-all"-Kanal, sobald jemand dem Server beitritt, mit einer kurzen
Übersicht was sie hier erwartet. Unterscheidet zwischen VIP-Mitgliedern
(alle Bots) und normalen Mitgliedern (nur Snipe-Bot, plus VIP-Hinweis).

Konfiguration:
  WELCOME_CHANNEL_NAME – Teilstring des Zielkanal-Namens (Standard: "chat-all",
                         findet also z.B. auch "🇩🇪-chat-all")
  VIP_ROLE_NAME        – exakter Name der VIP-Rolle (Standard: "VIP")

Hinweis: Bei einem brandneuen Mitglied hat Discord die Rolle direkt beim
Beitritt evtl. noch nicht zugewiesen (falls die VIP-Rolle erst nach der
Whop-Zahlung automatisch vergeben wird) – die Nachricht spiegelt einfach den
Rollenstand im Moment des Beitritts wider. Bekommt jemand die VIP-Rolle
NACHTRÄGLICH (z.B. nach Whop-Kauf, siehe on_member_update), wird die goldene
VIP-Nachricht mit GIF trotzdem noch nachgereicht.

Die VIP-GIFs (assets/vip_welcome/) wechseln bei jedem neuen VIP durch – der
Bot rotiert einmal durch alle vorhandenen GIFs (zufällige Reihenfolge, aber
kein direktes Wiederholen), bevor er von vorne beginnt. Das Deck lebt nur im
Bot-Speicher und startet bei jedem Bot-Neustart neu durch.
"""

import logging
import os
import random

import discord
from discord.ext import commands

log = logging.getLogger("welcome")

COLOR = 0x09B1BA
WELCOME_CHANNEL_NAME = os.getenv("WELCOME_CHANNEL_NAME", "chat-all")
VIP_ROLE_NAME = os.getenv("VIP_ROLE_NAME", "VIP")
VIP_GIF_DIR = os.getenv("VIP_GIF_DIR", os.path.join("assets", "vip_welcome"))
GIF_EXTENSIONS = (".gif",)

INTROS = [
    "Ein neues Gesicht im Vintage-Game! 🧵",
    "Willkommen in der Reselling-Crew! ⚡",
    "Frischer Nachschub für die Community! 🔥",
]

VIP_INTROS = [
    "👑 VIP ALARM! 👑",
    "🔥🔥 NEUES VIP-MITGLIED 🔥🔥",
    "✨ Willkommen in der Top-Liga ✨",
]

VIP_UPGRADE_INTROS = [
    "👑 UPGRADE! 👑",
    "🔥🔥 JETZT VIP 🔥🔥",
    "✨ Willkommen in der Top-Liga ✨",
]


def _is_vip(member: discord.Member) -> bool:
    return any(role.name.lower() == VIP_ROLE_NAME.lower() for role in member.roles)


def _list_vip_gifs() -> list[str]:
    if not os.path.isdir(VIP_GIF_DIR):
        return []
    return [
        os.path.join(VIP_GIF_DIR, f) for f in sorted(os.listdir(VIP_GIF_DIR))
        if f.lower().endswith(GIF_EXTENSIONS)
    ]


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Rotiert durch alle GIFs bevor eins wiederholt wird (statt reinem
        # Zufall, der zufällig zweimal hintereinander dasselbe GIF ziehen
        # könnte) — so wechseln die GIFs bei jedem neuen VIP durch.
        self._gif_deck: list[str] = []
        self._last_gif: str | None = None

    def _next_vip_gif(self) -> str | None:
        files = _list_vip_gifs()
        if not files:
            self._gif_deck = []
            self._last_gif = None
            return None

        # Deck neu mischen, falls leer oder falls sich die Dateien geändert
        # haben (z.B. neues GIF hinzugefügt).
        if not self._gif_deck or not set(self._gif_deck).issubset(set(files)):
            deck = files.copy()
            random.shuffle(deck)
            # Verhindert, dass beim Neu-Mischen direkt wieder dasselbe GIF
            # wie zuletzt an erster Stelle landet.
            if self._last_gif and len(deck) > 1 and deck[0] == self._last_gif:
                deck[0], deck[1] = deck[1], deck[0]
            self._gif_deck = deck

        gif = self._gif_deck.pop(0)
        self._last_gif = gif
        return gif

    def _build_embed(self, member: discord.Member, *, upgrade: bool = False) -> tuple[discord.Embed, discord.File | None]:
        is_vip = _is_vip(member)
        gif_file = None

        if is_vip:
            intro_pool = VIP_UPGRADE_INTROS if upgrade else VIP_INTROS
            embed = discord.Embed(title=random.choice(intro_pool), color=0xFFD700)  # gold für VIP
            gif_path = self._next_vip_gif()
            if gif_path:
                filename = os.path.basename(gif_path)
                gif_file = discord.File(gif_path, filename=filename)
                embed.set_image(url=f"attachment://{filename}")
            if upgrade:
                embed.description = (
                    f"Hey {member.mention}, du bist jetzt **VIP**! 🎉\n\n"
                    f"Damit hast du Zugriff auf alle Premium-Bots. Kurzer Überblick, wo du hin musst:"
                )
            else:
                embed.description = (
                    f"Hey {member.mention}, schön dass du da bist! 👋\n\n"
                    f"Hier läuft alles rund ums Vintage-Reselling — mit ein paar Bots, die dir "
                    f"den Alltag abnehmen. Kurzer Überblick, wo du hin musst:"
                )
            embed.add_field(
                name="🛍️ Verkaufen",
                value="Im **#verkauf**-Kanal: `!inserat` erstellt dir ein fertiges Listing "
                      "(KI schreibt Titel & Beschreibung).",
                inline=False,
            )
            embed.add_field(
                name="📸 Fotos checken",
                value="Im **#foto-check**-Kanal: `!fotocheck` bewertet deine Fotos mit Score und "
                      "konkreten Tipps, bevor du einstellst.",
                inline=False,
            )
            embed.add_field(
                name="🧍 Virtual Try-On",
                value="Im **#try-on**-Kanal: `!tryon` zieht dein Kleidungsstück auf ein Model-Foto "
                      "(experimentell, ungefähre Stil-Vorschau).",
                inline=False,
            )
            embed.add_field(
                name="🧾 Buchhaltung",
                value="Einkauf/Verkauf/Marge trackt der Bot automatisch mit — `!bilanz` gibt dir "
                      "jederzeit den Überblick.",
                inline=False,
            )
            embed.add_field(
                name="💶 Preise checken & 📡 Trends",
                value="`!preischeck <Artikel>` bevor du einstellst, im **#trend-radar**-Kanal zeigt "
                      "`!trends <Begriff>` ob gerade Angebot/Nachfrage steigt oder fällt.",
                inline=False,
            )
            embed.add_field(
                name="🎓 Coach fragen",
                value="Nicht sicher wie du fotografierst oder verhandelst? `!coach <Frage>` — "
                      "die KI hat konkrete Tipps, keine 08/15-Antworten.",
                inline=False,
            )
            embed.add_field(
                name="📌 Tipp",
                value="In jedem Kanal steht oben angepinnt, was du dort genau machen kannst — "
                      "einfach kurz hochscrollen.",
                inline=False,
            )
            embed.set_footer(text="Viel Erfolg beim Verkaufen! 🚀")
        else:
            embed = discord.Embed(title=random.choice(INTROS), color=COLOR)
            embed.description = (
                f"Hey {member.mention}, schön dass du da bist! 👋\n\n"
                f"Hier läuft alles rund ums Vintage-Reselling."
            )
            embed.add_field(
                name="🎯 Dein Sniper",
                value="Du hast Zugriff auf den Vinted-Snipe-Bot: `!add <kanal> <vinted-suchlink>` "
                      "richtet eine Suche ein, `!list` zeigt deine aktiven Suchen, `!help` alle Befehle.",
                inline=False,
            )
            embed.add_field(
                name="✨ Mehr geht mit VIP",
                value="Mit VIP kommen noch dazu: automatische Listing-Erstellung mit KI (`!inserat`), "
                      "Buchhaltung, Preis-Check und ein Reselling-Coach obendrauf. Schau dich um, "
                      "falls dich das interessiert.",
                inline=False,
            )
            embed.set_footer(text="Viel Erfolg beim Sniping! 🎯")

        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        return embed, gif_file

    @staticmethod
    def _find_channel(guild: discord.Guild) -> discord.TextChannel | None:
        return discord.utils.find(
            lambda c: WELCOME_CHANNEL_NAME.lower() in c.name.lower(),
            guild.text_channels,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel = self._find_channel(member.guild)
        if not channel:
            log.warning(f"Willkommens-Kanal (Suchbegriff '{WELCOME_CHANNEL_NAME}') nicht gefunden.")
            return
        embed, gif_file = self._build_embed(member)
        try:
            if gif_file:
                await channel.send(embed=embed, file=gif_file)
            else:
                await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning(f"Keine Berechtigung, im Kanal '{channel.name}' zu posten.")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Feuert die goldene VIP-Willkommensnachricht (mit GIF) auch dann, wenn
        jemand die VIP-Rolle erst NACHTRÄGLICH bekommt (z.B. nach einer
        Whop-Zahlung), nicht nur direkt beim Server-Beitritt."""
        was_vip = _is_vip(before)
        is_vip = _is_vip(after)
        if is_vip and not was_vip:
            channel = self._find_channel(after.guild)
            if not channel:
                log.warning(f"Willkommens-Kanal (Suchbegriff '{WELCOME_CHANNEL_NAME}') nicht gefunden.")
                return
            embed, gif_file = self._build_embed(after, upgrade=True)
            try:
                if gif_file:
                    await channel.send(embed=embed, file=gif_file)
                else:
                    await channel.send(embed=embed)
            except discord.Forbidden:
                log.warning(f"Keine Berechtigung, im Kanal '{channel.name}' zu posten.")

    @commands.command(name="willkommen-test")
    async def willkommen_test(self, ctx: commands.Context, member: discord.Member = None):
        """!willkommen-test [@person] – schickt die Willkommensnachricht manuell
        (zum Testen, ohne dass jemand wirklich neu beitreten muss)."""
        if not ctx.guild:
            await ctx.send("❌ Nur auf einem Server nutzbar.")
            return
        target = member or ctx.author
        channel = self._find_channel(ctx.guild)
        if not channel:
            await ctx.send(
                f"❌ Kein Kanal gefunden, dessen Name '{WELCOME_CHANNEL_NAME}' enthält. "
                "Prüfe `WELCOME_CHANNEL_NAME` in der `.env` oder den Kanalnamen."
            )
            return
        embed, gif_file = self._build_embed(target)
        try:
            if gif_file:
                await channel.send(embed=embed, file=gif_file)
            else:
                await channel.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(f"❌ Keine Berechtigung, im Kanal '{channel.name}' zu posten.")
            return
        if channel.id != ctx.channel.id:
            await ctx.send(f"✅ Test-Willkommensnachricht in {channel.mention} gepostet.")

    @commands.command(name="meine-rollen")
    async def meine_rollen(self, ctx: commands.Context, member: discord.Member = None):
        """!meine-rollen [@person] – zeigt exakte Rollennamen (zum Debuggen von VIP_ROLE_NAME)"""
        target = member or ctx.author
        rollen = [r.name for r in target.roles if r.name != "@everyone"]
        if not rollen:
            await ctx.send(f"{target.display_name} hat keine Rollen.")
            return
        liste = "\n".join(f"`{r}`" for r in rollen)
        erkannt = "✅ als VIP erkannt" if _is_vip(target) else "❌ NICHT als VIP erkannt"
        await ctx.send(
            f"Rollen von {target.display_name}:\n{liste}\n\n"
            f"Aktuell gesuchter VIP-Rollenname: `{VIP_ROLE_NAME}`\n{erkannt}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
