"""
Listing-Bot – fragt einen Artikel per Discord ab, postet ein formatiertes
Verkaufs-Listing in einen Channel und versucht (falls konfiguriert) das
Inserat automatisch auf Vinted zu erstellen.

Commands:
  !inserat          – startet den interaktiven Frage-Flow im aktuellen Kanal
  !inserat abbrechen – bricht einen laufenden Flow ab
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord.ext import commands

from .access import is_vip_or_admin
from .ai_vision import AIVisionError, build_photo_check_embed, check_photo_quality, generate_listing
from .vinted_client import VintedAPIError, VintedClient

log = logging.getLogger("listing-bot")

COLOR = 0x09B1BA
LISTING_CHANNEL_NAME = os.getenv("LISTING_CHANNEL_NAME", "verkauf")
QUESTION_TIMEOUT = 180  # Sekunden pro Frage


class ListingFlowCancelled(Exception):
    pass


class Listing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_flows: set[int] = set()  # user_ids mit laufendem Flow
        self.vinted = VintedClient()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    async def _ask(self, ctx: commands.Context, prompt: str, *, allow_images: bool = False):
        await ctx.send(prompt)

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=QUESTION_TIMEOUT)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout – Inserat-Erstellung abgebrochen. Starte neu mit `!inserat`.")
            raise ListingFlowCancelled()

        if msg.content.strip().lower() in ("abbrechen", "cancel"):
            await ctx.send("❌ Abgebrochen.")
            raise ListingFlowCancelled()

        if allow_images:
            return msg
        return msg.content.strip()

    @commands.command(name="inserat")
    async def inserat(self, ctx: commands.Context, action: str = None):
        if action and action.lower() == "abbrechen":
            self.active_flows.discard(ctx.author.id)
            await ctx.send("❌ Laufender Inserat-Flow abgebrochen (falls vorhanden).")
            return

        if ctx.author.id in self.active_flows:
            await ctx.send("⚠️ Du hast schon ein Inserat in Arbeit. Tippe `abbrechen` um neu zu starten.")
            return

        self.active_flows.add(ctx.author.id)
        try:
            await self._run_flow(ctx)
        except ListingFlowCancelled:
            pass
        except Exception as e:
            log.exception("Fehler im Inserat-Flow")
            await ctx.send(f"❌ Unerwarteter Fehler: `{e}`")
        finally:
            self.active_flows.discard(ctx.author.id)

    async def _run_flow(self, ctx: commands.Context):
        await ctx.send(
            "🛍️ **Neues Inserat** — jederzeit `abbrechen` tippen zum Stoppen.\n"
            "Ich frage dich Schritt für Schritt ab."
        )

        # ── Fotos zuerst ─────────────────────────────────────────────────────
        img_msg = await self._ask(
            ctx,
            "**1/5** Sende jetzt 1-4 Fotos des Artikels als Anhang in einer Nachricht.",
            allow_images=True,
        )
        image_attachments = [a for a in img_msg.attachments if a.content_type and a.content_type.startswith("image/")]
        if not image_attachments:
            await ctx.send("❌ Keine Bilder erkannt. Starte neu mit `!inserat`.")
            return

        images_bytes = []
        async with aiohttp.ClientSession() as dl_session:
            for a in image_attachments[:4]:
                async with dl_session.get(a.url) as r:
                    images_bytes.append(await r.read())

        # ── Foto-Check (kurzes Feedback, bevor die KI das Listing generiert) ──
        # Best-effort: schlägt der Check fehl, überspringen wir ihn einfach
        # still (das darf den eigentlichen Inserat-Flow nicht blockieren).
        try:
            photo_result = await check_photo_quality(images_bytes)
        except AIVisionError as e:
            photo_result = None
            log.warning(f"Foto-Check übersprungen (Fehler): {e}")
        if photo_result:
            await ctx.send(embed=build_photo_check_embed(photo_result))

        # ── KI-Vorschlag (Titel/Marke/Beschreibung) ─────────────────────────
        title = brand = description = category = ""
        ai_suggested = False
        try:
            proposal = await generate_listing(images_bytes)
        except AIVisionError as e:
            proposal = None
            await ctx.send(f"⚠️ KI-Analyse fehlgeschlagen ({e}) — du trägst Titel/Marke/Beschreibung manuell ein.")

        if proposal:
            title, brand, category, description = (
                proposal["title"], proposal["brand"], proposal["category"], proposal["description"]
            )
            # Discord erlaubt max. 1024 Zeichen pro Embed-Feld – die volle
            # Beschreibung (inkl. Hashtags) kann länger sein, für die Vorschau
            # kürzen wir sie daher; das eigentliche Listing bekommt trotzdem
            # den vollen Text (embed.description erlaubt bis 4096 Zeichen).
            preview_description = description or "–"
            if len(preview_description) > 1021:
                preview_description = preview_description[:1021] + "…"

            preview = discord.Embed(title="🤖 KI-Vorschlag", color=COLOR)
            preview.add_field(name="Titel", value=title or "–", inline=False)
            preview.add_field(name="Marke", value=brand or "–", inline=True)
            preview.add_field(name="Kategorie", value=category or "–", inline=True)
            preview.add_field(name="Beschreibung", value=preview_description, inline=False)
            await ctx.send(embed=preview)
            confirm = await self._ask(
                ctx,
                "Passt das so? Tippe `ja` zum Übernehmen, oder schreib direkt deinen eigenen Titel "
                "(dann frage ich Marke/Beschreibung trotzdem nochmal einzeln ab)."
            )
            if confirm.strip().lower() not in ("ja", "j", "yes", "passt"):
                title = confirm
                brand = await self._ask(ctx, "Marke? (z.B. Nike — oder `-` für keine)")
                description = await self._ask(ctx, "Beschreibung? (gerne mit Hashtags)")
            else:
                ai_suggested = True
        else:
            # Kein API-Key gesetzt oder KI ausgefallen → normale manuelle Abfrage
            title = await self._ask(ctx, "Titel des Artikels?")
            brand = await self._ask(ctx, "Marke? (z.B. Nike — oder `-` für keine)")
            description = await self._ask(ctx, "Beschreibung? (gerne mit Hashtags)")

        # ── Rest manuell: Zustand, Größe, Verkaufspreis, Einkaufspreis ──────
        size = await self._ask(ctx, "**2/5** Größe?")
        condition = await self._ask(
            ctx,
            "**3/5** Zustand? (`neu mit etikett` / `neu ohne etikett` / `sehr gut` / `gut` / `zufriedenstellend`)"
        )
        verkaufspreis_raw = await self._ask(ctx, "**4/5** Verkaufspreis in €? (z.B. 24.90)")
        try:
            price = float(verkaufspreis_raw.replace(",", "."))
        except ValueError:
            await ctx.send("❌ Ungültiger Preis. Starte neu mit `!inserat`.")
            return

        einkauf_raw = await self._ask(ctx, "**5/5** Was hast du selbst dafür bezahlt (Einkaufspreis in €)?")
        try:
            einkaufspreis = float(einkauf_raw.replace(",", "."))
        except ValueError:
            await ctx.send("❌ Ungültiger Einkaufspreis. Starte neu mit `!inserat`.")
            return

        # ── Discord-Listing posten ──────────────────────────────────────────
        channel = discord.utils.get(ctx.guild.text_channels, name=LISTING_CHANNEL_NAME) if ctx.guild else None
        target_channel = channel or ctx.channel

        # Discord erlaubt max. 4096 Zeichen für embed.description – zur
        # Sicherheit kappen, falls die KI mal ungewöhnlich viel Text liefert.
        final_description = description if len(description) <= 4096 else description[:4093] + "…"

        embed = discord.Embed(
            title=f"{title} | {price:.2f} €",
            description=final_description,
            color=COLOR,
        )
        embed.add_field(name="🏷️ Marke", value=brand or "–", inline=True)
        embed.add_field(name="📏 Größe", value=size or "–", inline=True)
        embed.add_field(name="📦 Zustand", value=condition or "–", inline=True)
        footer = f"Eingestellt von {ctx.author.display_name}"
        if ai_suggested:
            footer += " • Titel/Beschreibung KI-generiert"
        embed.set_footer(text=footer)
        embed.set_thumbnail(url=image_attachments[0].url)

        await target_channel.send(embed=embed)
        for extra in image_attachments[1:4]:
            e = discord.Embed(color=COLOR)
            e.set_image(url=extra.url)
            await target_channel.send(embed=e)

        note = f"✅ Listing in #{target_channel.name} gepostet." if channel else \
               f"✅ Listing gepostet (Kanal `#{LISTING_CHANNEL_NAME}` nicht gefunden, hier stattdessen)."
        await ctx.send(note)

        # ── Buchhaltung direkt mitloggen ────────────────────────────────────
        # Nutzt die Buchhaltungs-Cog falls geladen, damit der Artikel sofort
        # im Lager auftaucht (ohne dass du !kauf manuell nochmal eintippen musst).
        buchhaltung_cog = self.bot.get_cog("Buchhaltung")
        if buchhaltung_cog:
            await ctx.invoke(self.bot.get_command("kauf"), name=title, preis=einkaufspreis,
                              notiz=f"via !inserat, Verkaufspreis {price:.2f} €")
            await ctx.send(f"ℹ️ Artikel mit **{einkaufspreis:.2f} €** Einkaufspreis in der Buchhaltung angelegt.")

        # ── Vinted Auto-Post (experimentell, opt-in) ────────────────────────
        if not self.vinted.enabled:
            await ctx.send(
                "ℹ️ Kein `VINTED_SESSION_COOKIE` gesetzt — Vinted-Inserat musst du noch manuell erstellen. "
                "Siehe SETUP.md für die automatische Variante."
            )
            return

        await ctx.send("🚀 Versuche Vinted-Inserat automatisch zu erstellen (experimentell)…")
        try:
            item = await self.vinted.create_listing(
                title=title, description=description, price=price,
                brand=brand, category=category or title, condition=condition,
                images=images_bytes,
            )
            item_id = item.get("id")
            item_url = f"https://www.vinted.de/items/{item_id}" if item_id else None
            if item_url:
                await ctx.send(f"✅ Vinted-Inserat erstellt: {item_url}")
            else:
                await ctx.send("⚠️ Vinted hat geantwortet, aber ohne erkennbare Item-ID. Bitte manuell prüfen.")
        except VintedAPIError as e:
            await ctx.send(
                f"❌ Vinted-Auto-Post fehlgeschlagen: `{e}`\n"
                "Discord-Listing steht trotzdem — Vinted-Inserat bitte manuell erstellen. "
                "(Das ist erwartbar solange wir die Endpunkte noch nicht gegen deinen echten "
                "Account verifiziert haben — lass uns das gemeinsam debuggen.)"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Listing(bot))
