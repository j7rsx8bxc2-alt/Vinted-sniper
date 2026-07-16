"""
Foto-Check – KI bewertet Verkaufsfotos VOR dem Posten (Licht, Hintergrund,
Bildausschnitt, fehlende Perspektiven wie Rückseite/Etikett), damit Artikel
von Anfang an besser aussehen und sich schneller verkaufen. Läuft auch
automatisch als kurzer Zwischenschritt in `!inserat`, direkt nachdem die
Fotos hochgeladen wurden.

Braucht denselben kostenlosen OPENROUTER_API_KEY wie `!inserat`/`!coach`.

Command:
  !fotocheck – schickt 1-4 Fotos, die KI bewertet sie mit Score + Tipps
"""

import asyncio
import logging

import aiohttp
import discord
from discord.ext import commands

from .access import is_vip_or_admin
from .ai_vision import AIVisionError, build_photo_check_embed, check_photo_quality
from .openrouter_client import is_enabled

log = logging.getLogger("photo-check")

QUESTION_TIMEOUT = 120


class PhotoCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    @commands.command(name="fotocheck")
    async def fotocheck(self, ctx: commands.Context):
        """!fotocheck – KI bewertet 1-4 Fotos mit Score + konkreten Verbesserungstipps"""
        if not is_enabled():
            await ctx.send(
                "❌ Kein `OPENROUTER_API_KEY` gesetzt — der Foto-Check braucht den gleichen "
                "kostenlosen Key wie `!inserat`. Siehe SETUP.md."
            )
            return

        await ctx.send("📸 Schick mir jetzt 1-4 Fotos des Artikels als Anhang in einer Nachricht.")

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=QUESTION_TIMEOUT)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout – starte neu mit `!fotocheck`.")
            return

        if msg.content.strip().lower() in ("abbrechen", "cancel"):
            await ctx.send("❌ Abgebrochen.")
            return

        image_attachments = [
            a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")
        ]
        if not image_attachments:
            await ctx.send("❌ Keine Bilder erkannt. Starte neu mit `!fotocheck`.")
            return

        images_bytes = []
        async with aiohttp.ClientSession() as dl_session:
            for a in image_attachments[:4]:
                async with dl_session.get(a.url) as r:
                    images_bytes.append(await r.read())

        async with ctx.typing():
            try:
                result = await check_photo_quality(images_bytes)
            except AIVisionError as e:
                await ctx.send(f"❌ Foto-Check fehlgeschlagen: `{e}`\nEinfach nochmal probieren.")
                return

        if result is None:
            await ctx.send("❌ Kein `OPENROUTER_API_KEY` gesetzt.")
            return

        await ctx.send(embed=build_photo_check_embed(result))


async def setup(bot: commands.Bot):
    await bot.add_cog(PhotoCheck(bot))
