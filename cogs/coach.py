"""
Reselling-Coach – VIPs können der KI direkt Fragen zum Vintage-Reselling
stellen (Fotografie, Preisstrategie, Textbausteine, Verhandeln etc.).

Command:
  !coach <Frage>
"""

import logging

import discord
from discord.ext import commands

from .access import is_vip_or_admin
from .openrouter_client import OpenRouterError, chat, is_enabled

log = logging.getLogger("coach")

COLOR = 0x09B1BA
MAX_HISTORY = 6  # wie viele letzte Nachrichten als Kontext mitgeschickt werden

SYSTEM_PROMPT = """Du bist ein erfahrener Coach für Vintage-Kleidung-Reselling
(Vinted-Fokus, aber Wissen gilt auch für andere Plattformen). Du hilfst mit:
Fotografie, Preisstrategie, Beschreibungen/Hashtags, Verhandeln mit
Käufer:innen, Einkaufsstrategie, Zeitmanagement, und Marktkenntnis zu
Vintage-Marken und -Stilen.

Wichtig – so soll deine Antwort sein:
- Sei so konkret wie möglich: exakte Vorgehensweisen, Zahlen, Reihenfolgen,
  Formulierungsbeispiele – keine generischen Plattitüden wie "mach gute
  Fotos" oder "sei ehrlich in der Beschreibung". Wenn du sowas sagen willst,
  sag stattdessen WIE genau (z.B. welcher Winkel, welches Licht, welche
  Formulierung).
- Bevorzuge Taktiken, die nicht jeder Anfänger schon kennt, statt
  Standard-Ratschläge zu wiederholen. Wenn eine Frage aber wirklich nur eine
  einfache Standard-Antwort hat, ist das auch ok – erfinde keine
  Kompliziertheit wo keine ist.
- Sei ehrlich bei Unsicherheit: wenn eine Taktik plattform-spezifisch ist und
  du nicht sicher bist ob sie aktuell noch so funktioniert (Algorithmen/
  Features ändern sich), sag das kurz dazu, statt es als Fakt zu verkaufen.
- Antworte auf Deutsch, in normalen Sätzen (keine Bullet-Points außer explizit
  gewünscht), meist 3-8 Sätze, bei komplexeren Fragen auch gerne länger."""


class Coach(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # einfache pro-User-Historie im Speicher (kein Anspruch auf Persistenz)
        self.history: dict[int, list[dict]] = {}

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    @commands.command(name="coach")
    async def coach(self, ctx: commands.Context, *, frage: str = None):
        if not is_enabled():
            await ctx.send(
                "❌ Kein `OPENROUTER_API_KEY` gesetzt — der Coach braucht den gleichen "
                "kostenlosen Key wie `!inserat`. Siehe SETUP.md."
            )
            return
        if not frage:
            await ctx.send("Frag mich was! Z.B. `!coach wie fotografiere ich Schuhe am besten?`")
            return

        async with ctx.typing():
            user_history = self.history.setdefault(ctx.author.id, [])
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(user_history[-MAX_HISTORY:])
            messages.append({"role": "user", "content": frage})

            try:
                answer = await chat(messages, temperature=0.6)
            except OpenRouterError as e:
                await ctx.send(f"❌ Coach gerade nicht erreichbar: `{e}`")
                return

            user_history.append({"role": "user", "content": frage})
            user_history.append({"role": "assistant", "content": answer})
            # Historie klein halten
            del user_history[:-MAX_HISTORY]

        embed = discord.Embed(description=answer, color=COLOR)
        embed.set_author(name="🎓 Reselling-Coach")
        embed.set_footer(text=f"Gefragt von {ctx.author.display_name} • !coach <Frage>")
        await ctx.send(embed=embed)

    @commands.command(name="coach-reset")
    async def coach_reset(self, ctx: commands.Context):
        """!coach-reset – vergisst den bisherigen Gesprächsverlauf mit dir"""
        self.history.pop(ctx.author.id, None)
        await ctx.send("🔄 Gesprächsverlauf mit dem Coach zurückgesetzt.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Coach(bot))
