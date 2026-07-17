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

from cogs.access import admin_only
from cogs.ai_vision import assess_grail
from cogs.openrouter_client import is_enabled as ai_enabled

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
PROXIES_FILE   = os.getenv("PROXIES_FILE", "proxies.txt")   # Format pro Zeile: ip:port:username:password
URLS_FILE      = os.getenv("MONITOR_URLS_FILE", "monitor_urls.json")
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
    dirname = os.path.dirname(URLS_FILE)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(URLS_FILE, "w") as f:
        json.dump(monitors, f, indent=2)

MONITORS   = load_monitors()
seen_items : deque = deque(maxlen=MAX_SEEN)
seen_set   : set   = set()
_active_session: aiohttp.ClientSession | None = None  # wird beim Start des Sniper-Loops gesetzt

# ── Bot ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # nötig damit on_member_join (Willkommensnachricht) feuert
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
    if isinstance(error, commands.CheckFailure):
        # Stille Ablehnung: keine Berechtigung -> keine Fehlermeldung im Chat
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Fehlende Argumente. Tippe `!help` für eine Übersicht.")
    else:
        log.error(f"Command-Fehler: {error}")

# ── Commands ──────────────────────────────────────────────────────────────────
# Nur Server-Admins dürfen diese Snipe-Bot-Befehle nutzen, da sie die geteilte
# Monitor-Konfiguration für den ganzen Server verändern.
@bot.command()
@admin_only()
async def add(ctx, monitor: str, *, url: str):
    monitor = monitor.lower()
    is_new = monitor not in MONITORS
    if is_new:
        MONITORS[monitor] = []
    if url in MONITORS[monitor]:
        await ctx.send(f"⚠️ Diese URL ist in **#{monitor}** bereits vorhanden.")
        return
    MONITORS[monitor].append(url)
    save_monitors(MONITORS)

    if is_new and _active_session is not None:
        # Neuer Monitor-Name -> sofort einen eigenen Loop dafür starten
        asyncio.create_task(monitor_loop(_active_session, monitor))
        log.info(f"🆕 Neuer Monitor '{monitor}' zur Laufzeit gestartet.")

    prefix = "🆕 Neuer Kanal erkannt und " if is_new else ""
    await ctx.send(f"✅ {prefix}Suche zu **#{monitor}** hinzugefügt!")

@bot.command()
@admin_only()
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
@admin_only()
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
@admin_only()
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
    embed.add_field(
        name="🛍️ Listing & 🧾 Buchhaltung",
        value="`!inserat` – neues Verkaufs-Listing erstellen\n"
              "`!buchhaltung` – alle Buchhaltungs-Commands anzeigen",
        inline=False
    )
    embed.add_field(
        name="🎓 Coach & 💶 Preis-Check",
        value="`!coach <Frage>` – Reselling-Tipps von der KI\n"
              "`!preischeck <Suchbegriff>` – Preisspanne ähnlicher Vinted-Angebote",
        inline=False
    )
    embed.add_field(
        name="🧍 Virtual Try-On (experimentell)",
        value="`!tryon` – Kleidungsstück auf ein Model-Foto ziehen\n"
              "`!tryon-modelle` – zeigt hinterlegte Model-Fotos an",
        inline=False
    )
    embed.add_field(
        name="📸 Foto-Check & 📡 Trend-Radar",
        value="`!fotocheck` – KI bewertet Fotos vorm Posten (läuft auch automatisch in `!inserat`)\n"
              "`!trends <Suchbegriff>` – Trend-Check jetzt, `!trends-hilfe` für alle Watchlist-Commands",
        inline=False
    )
    await ctx.send(embed=embed)

# ── Fetch ─────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.vinted.de/",
}

# Vinted verlangt einen gültigen Session-Cookie bevor die API antwortet.
# Wir "besuchen" daher zuerst die normale Startseite über denselben Proxy,
# sammeln den Cookie ein und cachen ihn kurzzeitig (spart Requests/Proxy-Last).
_cookie_cache: dict[str, tuple[str, float]] = {}
COOKIE_TTL = 300  # Sekunden, wie lange ein Cookie pro Proxy wiederverwendet wird

async def get_session_cookie(session: aiohttp.ClientSession, proxy_url, proxy_auth) -> str | None:
    now = asyncio.get_event_loop().time()
    cache_key = proxy_url or "no-proxy"
    cached = _cookie_cache.get(cache_key)
    if cached and (now - cached[1]) < COOKIE_TTL:
        return cached[0]
    try:
        async with session.get(
            "https://www.vinted.de/",
            headers={"User-Agent": HEADERS["User-Agent"]},
            proxy=proxy_url, proxy_auth=proxy_auth,
            timeout=aiohttp.ClientTimeout(total=12)
        ) as r:
            cookies = r.cookies
            if cookies:
                cookie_header = "; ".join(f"{k}={v.value}" for k, v in cookies.items())
                _cookie_cache[cache_key] = (cookie_header, now)
                return cookie_header
    except aiohttp.ClientError as e:
        log.debug(f"Cookie-Abruf fehlgeschlagen: {e}")
    return None

async def fetch_items(session: aiohttp.ClientSession, url: str, retries: int = 3) -> list:
    for attempt in range(retries):
        p = get_random_proxy()
        proxy_url  = p["url"]  if p else None
        proxy_auth = p["auth"] if p else None
        try:
            # Schritt 1: Session-Cookie über denselben Proxy holen
            cookie_header = await get_session_cookie(session, proxy_url, proxy_auth)
            req_headers = dict(HEADERS)
            if cookie_header:
                req_headers["Cookie"] = cookie_header

            # Schritt 2: eigentliche API-Anfrage mit Cookie
            async with session.get(
                url, headers=req_headers,
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
                elif r.status in (401, 403):
                    log.warning(f"HTTP {r.status} (blockiert) → versuche anderen Proxy...")
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

def extract_price(item: dict) -> tuple[str, str]:
    """Vinted liefert 'price' inzwischen als verschachteltes Objekt
    {'amount': '20.0', 'currency_code': 'EUR'} statt als einfache Zahl."""
    price_raw = item.get("price", {})
    if isinstance(price_raw, dict):
        amount   = price_raw.get("amount", "?")
        currency = price_raw.get("currency_code", "EUR")
    else:
        amount   = price_raw
        currency = item.get("currency", "EUR")
    currency_symbol = {"EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "CZK": "Kč"}.get(currency, currency)
    return str(amount), currency_symbol

def format_price(amount, currency) -> str:
    try:
        val = float(str(amount).replace(",", "."))
        converted = val * 1.12  # grobe Schätzung inkl. Käuferschutz
        return f"{val:.2f} {currency} ( ≈ {converted:.2f} {currency} )"
    except (ValueError, TypeError):
        return f"{amount} {currency}"

# ── Embed mit Buttons und Bilder-Grid ──────────────────────────────────────────
async def send_item(channel: discord.TextChannel, item: dict, monitor_name: str,
                     session: aiohttp.ClientSession | None = None):
    item_id    = str(item.get("id", "?"))
    title      = item.get("title", "Unbekannter Artikel")
    amount, currency = extract_price(item)
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

    # ── Grail-Erkennung: KI schätzt anhand des ersten Fotos ein, ob der Fund
    # ungewöhnlich selten/begehrt ist (Limited Edition, seltene Colorway, Kult-Stück).
    # Best-effort: läuft nur wenn ein OpenRouter-Key gesetzt ist, und ein Fehler
    # hier darf den Sniper nie blockieren – der Fund wird dann einfach ganz normal
    # ohne 🔥-Badge gepostet.
    grail = None
    if photo_urls and session is not None and ai_enabled():
        try:
            async with session.get(
                photo_urls[0], timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    img_bytes = await r.read()
                    grail = await assess_grail(img_bytes, title, brand)
        except Exception as e:
            log.debug(f"Grail-Check Foto-Download fehlgeschlagen: {e}")

    is_grail = bool(grail and grail.get("is_grail"))
    title_prefix = "🔥 " if is_grail else ""

    # ── Hauptembed: Verkäufer + Titel + Beschreibung ────────────────────────────
    embed = discord.Embed(
        title=f"{title_prefix}{flag} {title} | {amount} {currency}",
        url=item_url,
        description=f"👤 **{seller}**",
        color=0xFFD700 if is_grail else 0x09B1BA
    )
    if is_grail and grail.get("grund"):
        embed.add_field(name="🔥 Seltener Fund", value=grail["grund"], inline=False)
    embed.add_field(name="📅 Aktualisiert", value="Gerade eben", inline=True)
    embed.add_field(name="📏 Größe",        value=size,           inline=True)
    embed.add_field(name="🏷️ Marke",        value=brand,          inline=True)
    embed.add_field(name="📦 Zustand",      value=condition,      inline=True)
    embed.add_field(name="🌟 Bewertung",    value=f"({rating_stars})", inline=True)
    embed.add_field(name="💰 Preis",        value=format_price(amount, currency), inline=True)

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
# Statt alle Kanäle nacheinander abzuklappern, läuft jeder Monitor als eigener
# Task parallel. Ein Semaphore begrenzt gleichzeitige Proxy-Requests, damit wir
# Webshare nicht wieder mit zu vielen Requests/Sekunde überlasten (402-Fehler).
MAX_CONCURRENT_REQUESTS = 5
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

async def monitor_loop(session: aiohttp.ClientSession, monitor_name: str):
    """Ein eigener Dauer-Loop pro Monitor/Kanal – läuft unabhängig von den anderen."""
    while True:
        urls = MONITORS.get(monitor_name, [])
        if not urls:
            await asyncio.sleep(30)
            continue
        channel = discord.utils.get(bot.get_all_channels(), name=monitor_name)
        if not channel:
            await asyncio.sleep(30)
            continue
        for url in urls:
            try:
                async with request_semaphore:
                    items = await fetch_items(session, url)
                for item in items:
                    item_id = str(item.get("id"))
                    if item_id and item_id not in seen_set:
                        if len(seen_items) == MAX_SEEN:
                            seen_set.discard(seen_items[0])
                        seen_items.append(item_id)
                        seen_set.add(item_id)
                        await send_item(channel, item, monitor_name, session=session)
            except Exception as e:
                log.error(f"[{monitor_name}] Fehler: {e}")
            # Moderate Pause je URL innerhalb eines Monitors
            await asyncio.sleep(random.uniform(8, 15))
        # Pause bevor dieser Monitor erneut alle seine URLs prüft
        await asyncio.sleep(random.uniform(10, 20))

async def sniper_loop():
    global _active_session
    log.info("🚀 Sniper-Loop gestartet (parallele Monitore).")
    async with aiohttp.ClientSession() as session:
        _active_session = session
        tasks = [
            asyncio.create_task(monitor_loop(session, name))
            for name in MONITORS.keys()
        ]
        await asyncio.gather(*tasks)


# ── Erweiterungen (Listing-Bot, Buchhaltungs-Bot) ────────────────────────────
EXTENSIONS = ["cogs.buchhaltung", "cogs.listing", "cogs.coach", "cogs.price_check", "cogs.tryon",
              "cogs.channel_help", "cogs.welcome", "cogs.photo_check", "cogs.trends"]

async def load_extensions():
    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info(f"🧩 Erweiterung geladen: {ext}")
        except Exception:
            log.exception(f"❌ Erweiterung konnte nicht geladen werden: {ext}")

# ── Start ─────────────────────────────────────────────────────────────────────
async def main():
    if not TOKEN:
        log.critical("❌ DISCORD_TOKEN fehlt!")
        raise SystemExit(1)
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
