import discord
from discord.ext import commands
import aiohttp
import asyncio
import os
import json
import random
import logging
from collections import deque
from dotenv import load_dotenv

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("vinted-bot")

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN            = os.getenv("DISCORD_TOKEN")
WEBSHARE_USER    = os.getenv("WEBSHARE_USER")
WEBSHARE_PASS    = os.getenv("WEBSHARE_PASS")
# Webshare Rotating Proxy Endpoint (gleiche Host/Port für alle Requests,
# Webshare rotiert die IP automatisch im Backend)
WEBSHARE_HOST    = "p.webshare.io"
WEBSHARE_PORT    = 80
PROXY_URL        = f"http://{WEBSHARE_HOST}:{WEBSHARE_PORT}"

URLS_FILE = "monitor_urls.json"
MAX_SEEN  = 10_000

# ── Proxy-Helper ──────────────────────────────────────────────────────────────
def get_proxy() -> dict | None:
    """Gibt Proxy-Konfig zurück wenn Credentials vorhanden, sonst None."""
    if WEBSHARE_USER and WEBSHARE_PASS:
        return {
            "url":  PROXY_URL,
            "auth": aiohttp.BasicAuth(WEBSHARE_USER, WEBSHARE_PASS),
        }
    log.warning("⚠️  Keine Webshare-Credentials – Requests ohne Proxy!")
    return None

PROXY = get_proxy()

# ── Monitors ──────────────────────────────────────────────────────────────────
DEFAULT_MONITORS = [
    "polos", "trackpants", "tracksuits", "pullover",
    "nike", "lacoste", "ralph-lauren", "blauer", "levis",
    "armani", "lamartina", "schuhe", "burberry",
    "true-religion", "miss-me", "versace", "fred-perry"
]

def load_monitors() -> dict[str, list[str]]:
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r") as f:
            data = json.load(f)
        for m in DEFAULT_MONITORS:
            data.setdefault(m, [])
        return data
    return {m: [] for m in DEFAULT_MONITORS}

def save_monitors(monitors: dict) -> None:
    with open(URLS_FILE, "w") as f:
        json.dump(monitors, f, indent=2)

MONITORS   = load_monitors()
seen_items : deque = deque(maxlen=MAX_SEEN)
seen_set   : set   = set()

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"✅ {bot.user} ist online!")
    log.info(f"📋 {sum(len(v) for v in MONITORS.values())} Suchen geladen.")
    proxy_status = f"{WEBSHARE_HOST}:{WEBSHARE_PORT}" if PROXY else "KEIN PROXY"
    log.info(f"🌐 Proxy: {proxy_status}")
    asyncio.create_task(sniper_loop())

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Fehlende Argumente. Tippe `!help` für eine Übersicht.")
    else:
        log.error(f"Command-Fehler: {error}")

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command()
async def add(ctx, monitor: str, *, url: str):
    monitor = monitor.lower()
    if monitor not in MONITORS:
        available = ", ".join(f"`{m}`" for m in MONITORS)
        await ctx.send(f"❌ Unbekannter Monitor.\nVerfügbar: {available}")
        return
    if url in MONITORS[monitor]:
        await ctx.send(f"⚠️ Diese URL ist in **#{monitor}** bereits vorhanden.")
        return
    MONITORS[monitor].append(url)
    save_monitors(MONITORS)
    await ctx.send(f"✅ Suche zu **#{monitor}** hinzugefügt!")

@bot.command()
async def remove(ctx, monitor: str, index: int):
    monitor = monitor.lower()
    if monitor not in MONITORS:
        await ctx.send("❌ Unbekannter Monitor.")
        return
    urls = MONITORS[monitor]
    if not urls:
        await ctx.send(f"❌ **#{monitor}** hat keine gespeicherten Suchen.")
        return
    if index < 1 or index > len(urls):
        await ctx.send(f"❌ Index muss zwischen 1 und {len(urls)} liegen.")
        return
    removed = urls.pop(index - 1)
    save_monitors(MONITORS)
    await ctx.send(f"🗑️ Suche #{index} aus **#{monitor}** entfernt:\n`{removed}`")

@bot.command(name="list")
async def list_monitors(ctx):
    active = {k: v for k, v in MONITORS.items() if v}
    if not active:
        await ctx.send("📭 Keine Suchen aktiv. Füge welche mit `!add` hinzu.")
        return
    embed = discord.Embed(title="Aktive Vinted-Suchen", color=0x09B1BA)
    for name, urls in active.items():
        lines = "\n".join(
            f"`{i+1}.` {u[:60]}..." if len(u) > 60 else f"`{i+1}.` {u}"
            for i, u in enumerate(urls)
        )
        embed.add_field(name=f"#{name} ({len(urls)})", value=lines, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def proxy(ctx):
    """Zeigt den aktuellen Proxy-Status."""
    if PROXY:
        embed = discord.Embed(title="🌐 Proxy-Status", color=0x00C853)
        embed.add_field(name="Anbieter", value="Webshare Rotating Proxy", inline=False)
        embed.add_field(name="Endpoint", value=f"`{WEBSHARE_HOST}:{WEBSHARE_PORT}`", inline=True)
        embed.add_field(name="Auth", value="✅ Aktiv", inline=True)
    else:
        embed = discord.Embed(title="🌐 Proxy-Status", color=0xFF5252)
        embed.add_field(name="Status", value="❌ Kein Proxy konfiguriert", inline=False)
        embed.add_field(name="Fix", value="Setze `WEBSHARE_USER` und `WEBSHARE_PASS` in Railway.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(
        title="🎯 Vinted Sniper – Hilfe",
        description="Alle Befehle im Überblick",
        color=0x09B1BA
    )
    embed.add_field(
        name="📥 Suche hinzufügen",
        value="`!add <kanal> <vinted-url>`\nBeispiel: `!add nike https://www.vinted.de/...`",
        inline=False
    )
    embed.add_field(
        name="🗑️ Suche entfernen",
        value="`!remove <kanal> <index>`\nBeispiel: `!remove nike 1`",
        inline=False
    )
    embed.add_field(name="📋 Suchen anzeigen", value="`!list`", inline=True)
    embed.add_field(name="🌐 Proxy-Status",     value="`!proxy`", inline=True)
    embed.add_field(
        name="🗂️ Kategorien",
        value="`polos` `trackpants` `tracksuits` `pullover` `schuhe`",
        inline=False
    )
    embed.add_field(
        name="👕 Marken",
        value="`nike` `lacoste` `ralph-lauren` `blauer` `levis` `armani`\n"
              "`lamartina` `burberry` `true-religion` `miss-me` `versace` `fred-perry`",
        inline=False
    )
    embed.set_footer(text="Vinted Sniper • Webshare Rotating Proxies")
    await ctx.send(embed=embed)

# ── Fetch ─────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}

async def fetch_items(session: aiohttp.ClientSession, url: str) -> list:
    proxy_url  = PROXY["url"]  if PROXY else None
    proxy_auth = PROXY["auth"] if PROXY else None
    try:
        async with session.get(
            url,
            headers    = HEADERS,
            proxy      = proxy_url,
            proxy_auth = proxy_auth,
            timeout    = aiohttp.ClientTimeout(total=12),
        ) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("items", [])[:8]
            elif r.status == 429:
                log.warning("⏳ Rate-limit – warte 30s...")
                await asyncio.sleep(30)
            elif r.status == 407:
                log.error("🔐 Proxy-Auth fehlgeschlagen – prüfe WEBSHARE_USER/PASS!")
            else:
                log.warning(f"HTTP {r.status} → {url[:70]}")
    except aiohttp.ClientProxyConnectionError as e:
        log.error(f"🌐 Proxy-Verbindungsfehler: {e}")
    except aiohttp.ClientError as e:
        log.error(f"Netzwerk-Fehler: {e}")
    return []

# ── Embed ─────────────────────────────────────────────────────────────────────
async def send_item_embed(channel: discord.TextChannel, item: dict, monitor_name: str):
    item_id   = str(item.get("id", "?"))
    title     = item.get("title", "Unbekannter Artikel")
    price     = item.get("price", "?")
    currency  = item.get("currency", "€")
    size      = item.get("size_title", "–")
    brand     = item.get("brand_title", "–")
    condition = item.get("status", "–")
    photos    = item.get("photos", [])
    photo_url = None
    if photos:
        photo_url = photos[0].get("full_size_url") or photos[0].get("url")
    item_url  = f"https://www.vinted.de/items/{item_id}"

    embed = discord.Embed(title=f"🔔 {title}", url=item_url, color=0x09B1BA)
    embed.add_field(name="💶 Preis",    value=f"{price} {currency}", inline=True)
    embed.add_field(name="📏 Größe",    value=size,                  inline=True)
    embed.add_field(name="👕 Marke",    value=brand,                 inline=True)
    embed.add_field(name="✅ Zustand",  value=condition,             inline=True)
    embed.add_field(name="🗂️ Monitor", value=f"#{monitor_name}",    inline=True)
    if photo_url:
        embed.set_thumbnail(url=photo_url)
    embed.set_footer(text=f"Item ID: {item_id}")
    await channel.send(embed=embed)

# ── Sniper Loop ───────────────────────────────────────────────────────────────
async def sniper_loop():
    log.info("🚀 Sniper-Loop gestartet.")
    async with aiohttp.ClientSession() as session:
        while True:
            for monitor_name, urls in MONITORS.items():
                if not urls:
                    continue
                channel = discord.utils.get(bot.get_all_channels(), name=monitor_name)
                if not channel:
                    log.debug(f"Kanal '#{monitor_name}' nicht gefunden – übersprungen.")
                    continue
                for url in urls:
                    try:
                        items = await fetch_items(session, url)
                        for item in items:
                            item_id = str(item.get("id"))
                            if item_id and item_id not in seen_set:
                                if len(seen_items) == MAX_SEEN:
                                    seen_set.discard(seen_items[0])
                                seen_items.append(item_id)
                                seen_set.add(item_id)
                                await send_item_embed(channel, item, monitor_name)
                    except Exception as e:
                        log.error(f"[{monitor_name}] Fehler: {e}")
                    await asyncio.sleep(random.uniform(1.5, 3.5))
            await asyncio.sleep(5)

# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        log.critical("❌ DISCORD_TOKEN fehlt! In Railway unter Variables setzen.")
        raise SystemExit(1)
    bot.run(TOKEN)
