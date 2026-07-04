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
TOKEN          = os.getenv("DISCORD_TOKEN")
PROXIES_FILE   = "proxies.txt"   # Format pro Zeile: ip:port:username:password
URLS_FILE      = "monitor_urls.json"
MAX_SEEN       = 10_000

def load_proxy_pool() -> list[dict]:
    """Lädt alle Proxys aus proxies.txt und baut daraus eine Pool-Liste.
    Format je Zeile: ip:port:username:password"""
    pool = []
    if not os.path.exists(PROXIES_FILE):
        log.warning(f"⚠️  {PROXIES_FILE} nicht gefunden – Requests ohne Proxy!")
        return pool
    with open(PROXIES_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.count(":") != 3:
                continue
            ip, port, user, pw = line.split(":")
            pool.append({
                "url": f"http://{ip}:{port}",
                "auth": aiohttp.BasicAuth(user, pw)
            })
    log.info(f"🌐 {len(pool)} Proxys aus {PROXIES_FILE} geladen.")
    return pool

PROXY_POOL = load_proxy_pool()

def get_random_proxy():
    """Wählt zufällig einen Proxy aus dem Pool – das ist unsere eigene Rotation."""
    if not PROXY_POOL:
        return None
    return random.choice(PROXY_POOL)

# ── Monitors ──────────────────────────────────────────────────────────────────
DEFAULT_MONITORS = [
    "polos", "trackpants", "tracksuits", "pullover",
    "nike", "lacoste", "ralph-lauren", "blauer", "levis",
    "armani", "lamartina", "schuhe", "burberry",
    "true-religion", "miss-me", "versace", "fred-perry"
]

def load_monitors():
    if os.path.exists(URLS_FILE):
        with open(URLS_FILE, "r") as f:
            data = json.load(f)
        for m in DEFAULT_MONITORS:
            data.setdefault(m, [])
        return data
    return {m: [] for m in DEFAULT_MONITORS}

def save_monitors(monitors):
    with open(URLS_FILE, "w") as f:
        json.dump(monitors, f, indent=2)

MONITORS   = load_monitors()
seen_items : deque = deque(maxlen=MAX_SEEN)
seen_set   : set   = set()

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── Buttons ───────────────────────────────────────────────────────────────────
class ArticleButtons(discord.ui.View):
    def __init__(self, item_url: str, buy_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="🔗 Ansehen",
            style=discord.ButtonStyle.link,
            url=item_url
        ))
        self.add_item(discord.ui.Button(
            label="🛒 Jetzt kaufen",
            style=discord.ButtonStyle.link,
            url=buy_url
        ))
        self.add_item(discord.ui.Button(
            label="✉️ Angebot senden",
            style=discord.ButtonStyle.link,
            url=f"{item_url}#make-offer"
        ))
        self.add_item(discord.ui.Button(
            label="❤️ Favorit",
            style=discord.ButtonStyle.link,
            url=f"{item_url}#favourite"
        ))

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"✅ {bot.user} ist online!")
    log.info(f"📋 {sum(len(v) for v in MONITORS.values())} Suchen geladen.")
    log.info(f"🌐 Proxy-Pool: {len(PROXY_POOL)} Proxys geladen (eigene Rotation).")
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
    if PROXY_POOL:
        embed = discord.Embed(title="🌐 Proxy-Status", color=0x00C853)
        embed.add_field(name="Anbieter", value="Webshare (eigene Rotation)", inline=False)
        embed.add_field(name="Proxys im Pool", value=f"{len(PROXY_POOL)}", inline=True)
        embed.add_field(name="Auth", value="✅ Aktiv", inline=True)
    else:
        embed = discord.Embed(title="🌐 Proxy-Status", color=0xFF5252)
        embed.add_field(name="Status", value="❌ Kein Proxy-Pool geladen (proxies.txt fehlt)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="🎯 Vinted Sniper – Hilfe", color=0x09B1BA)
    embed.add_field(name="📥 Suche hinzufügen", value="`!add <kanal> <vinted-url>`", inline=False)
    embed.add_field(name="🗑️ Suche entfernen",  value="`!remove <kanal> <index>`",   inline=False)
    embed.add_field(name="📋 Suchen anzeigen",  value="`!list`",                      inline=True)
    embed.add_field(name="🌐 Proxy-Status",     value="`!proxy`",                     inline=True)
    embed.add_field(
        name="🗂️ Kanäle",
        value="`polos` `trackpants` `tracksuits` `pullover` `schuhe`\n"
              "`nike` `lacoste` `ralph-lauren` `blauer` `levis` `armani`\n"
              "`lamartina` `burberry` `true-religion` `miss-me` `versace` `fred-perry`",
        inline=False
    )
    await ctx.send(embed=embed)

# ── Fetch ─────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
}

async def fetch_items(session: aiohttp.ClientSession, url: str, retries: int = 3) -> list:
    for attempt in range(retries):
        p = get_random_proxy()
        proxy_url  = p["url"]  if p else None
        proxy_auth = p["auth"] if p else None
        try:
            async with session.get(
                url, headers=HEADERS,
                proxy=proxy_url, proxy_auth=proxy_auth,
                timeout=aiohttp.ClientTimeout(total=12)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("items", [])[:8]
                elif r.status == 429:
                    log.warning("⏳ Rate-limit – warte 30s...")
                    await asyncio.sleep(30)
                elif r.status == 407:
                    log.error(f"🔐 Proxy-Auth fehlgeschlagen ({proxy_url}) – versuche anderen Proxy...")
                    continue
                else:
                    log.warning(f"HTTP {r.status} → {url[:70]}")
        except aiohttp.ClientError as e:
            log.error(f"Netzwerk-Fehler (Versuch {attempt+1}/{retries}, Proxy {proxy_url}): {e}")
            continue
    return []

# ── Sprach-Flaggen für Titel ───────────────────────────────────────────────────
COUNTRY_FLAGS = {
    "DE": "🇩🇪", "FR": "🇫🇷", "IT": "🇮🇹", "ES": "🇪🇸",
    "NL": "🇳🇱", "BE": "🇧🇪", "AT": "🇦🇹", "PL": "🇵🇱",
    "LU": "🇱🇺", "CZ": "🇨🇿", "PT": "🇵🇹"
}

def format_price(price, currency) -> str:
    try:
        val = float(str(price).replace(",", "."))
        converted = val * 1.12  # grobe Schätzung inkl. Käuferschutz
        return f"{val:.2f} {currency} ( ≈ {converted:.2f} {currency} )"
    except (ValueError, TypeError):
        return f"{price} {currency}"

# ── Embed mit Buttons und Bilder-Grid ──────────────────────────────────────────
async def send_item(channel: discord.TextChannel, item: dict, monitor_name: str):
    item_id    = str(item.get("id", "?"))
    title      = item.get("title", "Unbekannter Artikel")
    price      = item.get("price", "?")
    currency   = item.get("currency", "€")
    size       = item.get("size_title", "–")
    brand      = item.get("brand_title", "–")
    condition  = item.get("status", "–")
    seller     = item.get("user", {}).get("login", "Unbekannt")
    rating_raw = item.get("user", {}).get("feedback_reputation", 0) or 0
    country    = item.get("user", {}).get("country_iso_code", "DE")
    flag       = COUNTRY_FLAGS.get(country, "🌍")

    photos = item.get("photos", [])
    photo_urls = []
    for p in photos[:4]:
        url = p.get("full_size_url") or p.get("url")
        if url:
            photo_urls.append(url)

    item_url = f"https://www.vinted.de/items/{item_id}"
    buy_url  = f"https://www.vinted.de/transaction/buy/new?source_screen=item&transaction%5Bitem_id%5D={item_id}"

    rating_stars = round(float(rating_raw) * 5, 1)

    # ── Hauptembed: Verkäufer + Titel + Beschreibung ────────────────────────────
    embed = discord.Embed(
        title=f"{flag} {title} | {price} {currency}",
        url=item_url,
        description=f"👤 **{seller}**",
        color=0x09B1BA
    )
    embed.add_field(name="📅 Aktualisiert", value="Gerade eben", inline=True)
    embed.add_field(name="📏 Größe",        value=size,           inline=True)
    embed.add_field(name="🏷️ Marke",        value=brand,          inline=True)
    embed.add_field(name="📦 Zustand",      value=condition,      inline=True)
    embed.add_field(name="🌟 Bewertung",    value=f"({rating_stars})", inline=True)
    embed.add_field(name="💰 Preis",        value=format_price(price, currency), inline=True)

    if photo_urls:
        embed.set_thumbnail(url=photo_urls[0])

    embed.set_footer(text=f"🚚 Link Public Channel • #{monitor_name}")

    view = ArticleButtons(item_url=item_url, buy_url=buy_url)

    # ── Zweites Embed: großes Bilder-Grid (bis zu 4 Bilder in einer Nachricht) ──
    if photo_urls:
        gallery_embed = discord.Embed(color=0x09B1BA, url=item_url)
        gallery_embed.set_image(url=photo_urls[0])
        await channel.send(embeds=[embed, gallery_embed], view=view)

        # Bei mehreren Bildern: restliche als kleines Grid direkt darunter
        if len(photo_urls) > 1:
            extra_embeds = []
            for extra_url in photo_urls[1:4]:
                e = discord.Embed(color=0x09B1BA, url=item_url)
                e.set_image(url=extra_url)
                extra_embeds.append(e)
            await channel.send(embeds=extra_embeds)
    else:
        await channel.send(embed=embed, view=view)

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
                                await send_item(channel, item, monitor_name)
                    except Exception as e:
                        log.error(f"[{monitor_name}] Fehler: {e}")
                    await asyncio.sleep(random.uniform(1.5, 3.5))
            await asyncio.sleep(5)

# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        log.critical("❌ DISCORD_TOKEN fehlt!")
        raise SystemExit(1)
    bot.run(TOKEN)
