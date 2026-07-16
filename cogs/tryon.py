"""
Virtual Try-On – zieht ein fotografiertes Kleidungsstück auf ein Model-Foto,
damit der Artikel "getragen" statt nur flach fotografiert aussieht.

EXPERIMENTELL: nutzt eine kostenlose, community-gehostete KI auf Hugging Face
(kein API-Key, kein Kreditkarte). Dadurch: kann langsam sein (30-90s+), kann
bei viel Andrang überlastet sein, und die Space kann sich ändern ohne
Vorwarnung.

Standard-Space ist "yisol/IDM-VTON" – die originale, mit Abstand beliebteste
IDM-VTON-Space (2k+ Likes, läuft auf ZeroGPU). Vorher stand hier
"Nymbo/Virtual-Try-On" (ein Fork), der aber mit RUNTIME_ERROR abgestürzt ist
(kaputte Dependency beim Space-Betreiber, nicht reparierbar von uns aus) –
daher der Wechsel zur zuverlässigeren Original-Space. Über TRYON_SPACE in der
.env jederzeit auf eine andere IDM-VTON-artige Space mit "api_name='tryon'"
umstellbar, falls auch diese mal ausfällt.

Model-Fotos: lege KI-generierte, synthetische Fotos (z.B. von Bing Image
Creator oder thispersondoesnotexist.com – bewusst KEINE echten Personen von
Google/Pinterest/Vinted, das wäre ein Urheber-/Persönlichkeitsrechte-Risiko)
in den Ordner assets/models/. Der Bot wählt dann automatisch zufällig eins
davon aus. Ist der Ordner leer, fragt der Bot stattdessen jedes Mal nach
einem Model-Foto.

Logo-/Detail-Treue: bevor das Garment-Foto an IDM-VTON geht, beschreibt die
KI es kurz (Farbe, Schnitt, Logo-Position/-Aussehen falls erkennbar) statt
nur "clothing item" zu übergeben – das hilft dem Modell, Details wie Logos
näher am Original zu platzieren. WICHTIG: IDM-VTON ist ein generatives
Diffusionsmodell, kein 1:1-Compositing – kleine Logos, Schriftzüge und
Prints werden dadurch grundsätzlich nie pixelgenau reproduziert, auch mit
guter Beschreibung nicht. Das ist eine bekannte Grenze aller aktuellen
kostenlosen Try-On-Modelle, kein Bug in diesem Bot.

Commands:
  !tryon           – startet den Foto-Abfrage-Flow (nutzt automatisch ein
                     zufälliges Model aus assets/models/, falls vorhanden)
  !tryon eigenes   – erzwingt die manuelle Abfrage (eigenes Model-Foto schicken)
  !tryon-modelle   – zeigt wie viele Model-Fotos aktuell hinterlegt sind
"""

import asyncio
import io
import logging
import os
import random
import tempfile

import aiohttp
import discord
from discord.ext import commands

from .access import is_vip_or_admin
from .ai_vision import describe_garment

log = logging.getLogger("tryon")

COLOR = 0x09B1BA
# Über Env-Var austauschbar, falls diese Space mal offline/kaputt ist –
# einfach eine andere IDM-VTON-artige Space mit "api_name='tryon'" eintragen.
TRYON_SPACE = os.getenv("TRYON_SPACE", "yisol/IDM-VTON")
TIMEOUT_SECONDS = 180
MODELS_DIR = os.getenv("TRYON_MODELS_DIR", os.path.join("assets", "models"))
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _list_model_files() -> list[str]:
    if not os.path.isdir(MODELS_DIR):
        return []
    return [
        os.path.join(MODELS_DIR, f) for f in sorted(os.listdir(MODELS_DIR))
        if f.lower().endswith(IMAGE_EXTENSIONS)
    ]


def _gradio_available() -> bool:
    try:
        import gradio_client  # noqa: F401
        return True
    except ImportError:
        return False


class TryOn(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: set[int] = set()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    async def _wait_for_image(self, ctx: commands.Context, prompt: str) -> bytes | None:
        await ctx.send(prompt)

        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout – `!tryon` abgebrochen, starte einfach neu.")
            return None

        if msg.content.strip().lower() in ("abbrechen", "cancel"):
            await ctx.send("❌ Abgebrochen.")
            return None

        att = next(
            (a for a in msg.attachments if a.content_type and a.content_type.startswith("image/")),
            None,
        )
        if not att:
            await ctx.send("❌ Kein Bild erkannt – bitte als Anhang schicken. Starte neu mit `!tryon`.")
            return None

        async with aiohttp.ClientSession() as session:
            async with session.get(att.url) as r:
                return await r.read()

    def _run_tryon(self, model_bytes: bytes, garment_bytes: bytes, garment_des: str) -> bytes:
        """Läuft blockierend (gradio_client ist synchron) – wird per
        asyncio.to_thread aufgerufen, damit der Bot nicht einfriert."""
        from gradio_client import Client, file

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f_model, \
             tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f_garment:
            f_model.write(model_bytes)
            f_model.flush()
            f_garment.write(garment_bytes)
            f_garment.flush()
            model_path, garment_path = f_model.name, f_garment.name

        try:
            client = Client(TRYON_SPACE)
            result = client.predict(
                {"background": file(model_path), "layers": [], "composite": None},
                file(garment_path),
                garment_des,  # konkrete KI-Beschreibung (inkl. Logo/Farbe/Schnitt) statt Platzhalter
                True,   # is_checked: automatische Maskierung
                False,  # is_checked_crop
                30,     # denoise_steps
                42,     # seed
                api_name="/tryon",
            )
        finally:
            os.unlink(model_path)
            os.unlink(garment_path)

        image_out = result[0] if isinstance(result, (list, tuple)) else result
        if not isinstance(image_out, str) or not os.path.exists(image_out):
            raise RuntimeError(f"Unerwartetes Antwortformat von der KI: {image_out!r}")
        with open(image_out, "rb") as f:
            return f.read()

    @commands.command(name="tryon-modelle")
    async def tryon_modelle(self, ctx: commands.Context):
        """!tryon-modelle – zeigt wie viele Model-Fotos hinterlegt sind"""
        files = _list_model_files()
        if not files:
            await ctx.send(
                f"📭 Noch keine Model-Fotos in `{MODELS_DIR}/` hinterlegt. "
                "Leg dort ein paar KI-generierte Fotos ab (siehe SETUP.md), oder nutze `!tryon eigenes`."
            )
            return
        await ctx.send(f"🧍 {len(files)} Model-Foto(s) hinterlegt in `{MODELS_DIR}/` — `!tryon` wählt automatisch eins davon.")

    @commands.command(name="tryon")
    async def tryon(self, ctx: commands.Context, modus: str = None):
        if not _gradio_available():
            await ctx.send(
                "❌ Fehlende Abhängigkeit `gradio_client`. Einmalig im Terminal: "
                "`pip3 install -r requirements.txt`, dann Bot neu starten."
            )
            return

        if ctx.author.id in self.active:
            await ctx.send("⚠️ Du hast schon einen `!tryon`-Vorgang laufen. Kurz warten oder `abbrechen` tippen.")
            return

        erzwinge_eigenes = (modus or "").strip().lower() == "eigenes"
        model_files = [] if erzwinge_eigenes else _list_model_files()

        self.active.add(ctx.author.id)
        try:
            await ctx.send(
                "🧍 **Virtual Try-On** (experimentell) — jederzeit `abbrechen` tippen zum Stoppen.\n"
                "💡 Stil-Vorschau, kein exaktes Abbild: Farbe/Schnitt/Logo-Position kommen grob hin, "
                "Details wie Logos können die KI aber leicht abweichen oder dazuerfinden."
            )
            garment = await self._wait_for_image(ctx, "**1/1** Schick mir das Foto vom Kleidungsstück." if model_files
                                                  else "**1/2** Schick mir das Foto vom Kleidungsstück.")
            if garment is None:
                return

            if model_files:
                chosen_path = random.choice(model_files)
                with open(chosen_path, "rb") as f:
                    model = f.read()
                await ctx.send(f"🧍 Nutze zufälliges Model-Foto (`{os.path.basename(chosen_path)}`).")
            else:
                model = await self._wait_for_image(
                    ctx,
                    "**2/2** Jetzt das Model-Referenzfoto (KI-generiert, z.B. von Bing Image Creator "
                    "oder thispersondoesnotexist.com – keine echten Personen von Google o.ä.)."
                )
                if model is None:
                    return

            garment_des = await describe_garment(garment) or "clothing item"

            await ctx.send("🎨 Generiere... kann 30-90 Sekunden dauern (kostenlose, geteilte KI-Rechenleistung).")
            async with ctx.typing():
                try:
                    result_bytes = await asyncio.to_thread(self._run_tryon, model, garment, garment_des)
                except Exception as e:
                    log.exception("Try-On fehlgeschlagen")
                    await ctx.send(
                        f"❌ Try-On fehlgeschlagen: `{e}`\n"
                        "Das ist eine kostenlose Community-KI — kann an Überlastung liegen oder daran, dass "
                        "sich an der Space etwas geändert hat. Einfach nochmal probieren; falls es wiederholt "
                        "fehlschlägt, schick mir den Fehlertext, dann schauen wir's uns zusammen an."
                    )
                    return

            embed = discord.Embed(title="🧍 Virtual Try-On – Stil-Vorschau", color=COLOR)
            embed.set_footer(
                text=f"Angefragt von {ctx.author.display_name} • experimentell, ungefähre Vorschau — "
                     "für Logo-genaue Ansicht zusätzlich das echte Produktfoto posten"
            )
            file_obj = discord.File(io.BytesIO(result_bytes), filename="tryon_result.jpg")
            embed.set_image(url="attachment://tryon_result.jpg")
            await ctx.send(embed=embed, file=file_obj)
        finally:
            self.active.discard(ctx.author.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TryOn(bot))
