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
    embed.add_field(name="📋 Suchen
