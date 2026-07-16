
"""
Trend-Radar – verfolgt automatisch, wie sich die Anzahl aktiver
Vinted-Angebote zu den Marken/Kategorien verändert, die im Snipe-Bot aktiv
gesnipet werden (dieselben Kanäle wie bei !add/!list), PLUS einer festen
Markt-Watchlist bekannter Vintage-/Streetwear-Marken (MARKET_BRANDS), und
gleicht das mit den ECHTEN eigenen Verkäufen aus der Buchhaltung ab – KEINE
manuelle Pflege nötig, läuft von selbst.

Wichtig zur Markt-Watchlist: Vinted bietet keine öffentliche "was ist gerade
angesagt"-Funktion an (keine Trending-API, nur Suche pro Begriff). Die
MARKET_BRANDS-Liste ist die nächstbeste Annäherung – eine große, feste Liste
bekannter Marken wird IMMER mitgetrackt, unabhängig davon was du selbst
gerade snipst, damit die "größte Bewegung"-Liste im täglichen Update nicht
nur deine eigenen Suchbegriffe zeigt, sondern echte Marken-Gewinner über den
ganzen Vintage-Markt.

Drei Datenquellen im täglichen Update:
  1. Angebots-Trend (Vinted, deine Snipe-Bot-Kanäle + Markt-Watchlist): wie
     viele aktive Inserate es pro Marke/Kategorie gerade gibt, und ob das
     steigt oder fällt. Proxy für Konkurrenz/Interesse, KEINE echte
     Verkaufszahl – die gibt Vinted öffentlich nicht her.
  2. "Bei dir am besten weg" (Buchhaltung): wie viele deiner eigenen Artikel
     mit passendem Namen tatsächlich verkauft wurden und wie schnell –
     das sind echte Verkäufe, kein Proxy.

Nutzt dieselbe Fetch-Logik (Proxy-Rotation, Session-Cookie) wie Snipe-Bot und
Preis-Check – nur Lese-Requests, kein Login nötig.

Automatische Posts (beide in TRENDS_CHANNEL_NAME):
  - Täglich morgens (Standard 8 Uhr, Europe/Berlin): Trend-Update vs. letztem Check
  - Jeden Sonntag (Standard 18 Uhr, Europe/Berlin): Wochenrecap vs. Stand vor 7 Tagen

Commands:
  !trends <Suchbegriff>          – Ad-hoc-Check für einen beliebigen Begriff
  !trends-add <Suchbegriff>      – zusätzlichen Begriff mittracken (optional,
                                    über die Snipe-Bot-Kanäle hinaus)
  !trends-entfernen <Suchbegriff> – einen zusätzlichen Begriff wieder entfernen
  !trends-liste                  – zeigt, was aktuell getrackt wird (automatisch + manuell)
  !trends-tagesupdate            – Test: zeigt sofort, wie das tägliche Update aussieht
  !trends-wochenrecap            – Test: zeigt sofort, wie der Wochenrecap aussieht
  !trends-hilfe                  – Übersicht aller Trend-Radar-Commands

Konfiguration:
  TRENDS_CHANNEL_NAME  – Kanal für die automatischen Posts (Standard: "trend-radar")
  TREND_DAILY_HOUR     – Uhrzeit (Stunde, Europe/Berlin) fürs tägliche Update (Standard: 8)
  TREND_WEEKLY_HOUR    – Uhrzeit (Stunde, Europe/Berlin) für den Sonntags-Recap (Standard: 18)
  TRENDS_DB            – Pfad zur SQLite-Datei (Standard: "trends.db")
"""

import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks

from .access import is_vip_or_admin

log = logging.getLogger("trends")

DB_PATH = os.getenv("TRENDS_DB", "trends.db")
COLOR = 0x09B1BA
SEARCH_URL = "https://www.vinted.de/api/v2/catalog/items"
TRENDS_CHANNEL_NAME = os.getenv("TRENDS_CHANNEL_NAME", "trend-radar")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
TREND_DAILY_HOUR = int(os.getenv("TREND_DAILY_HOUR", "8"))
TREND_WEEKLY_HOUR = int(os.getenv("TREND_WEEKLY_HOUR", "18"))
DAILY_TIME = dtime(hour=TREND_DAILY_HOUR, minute=0, tzinfo=BERLIN_TZ)
WEEKLY_TIME = dtime(hour=TREND_WEEKLY_HOUR, minute=0, tzinfo=BERLIN_TZ)
SUNDAY = 6  # datetime.weekday(): Montag=0 ... Sonntag=6


def _connect() -> sqlite3.Connection:
    dirname = os.path.dirname(DB_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            term         TEXT NOT NULL,
            total_count  INTEGER NOT NULL,
            checked_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trend_watchlist (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            term       TEXT NOT NULL UNIQUE,
            added_by   TEXT,
            added_at   TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_snapshot(term: str) -> sqlite3.Row | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM trend_snapshots WHERE term = ? ORDER BY checked_at DESC LIMIT 1",
        (term,),
    ).fetchone()
    conn.close()
    return row


def _snapshot_before(term: str, cutoff_iso: str) -> sqlite3.Row | None:
    """Letzter Snapshot für den Begriff, der VOR (oder genau an) cutoff_iso liegt –
    dient als Baseline für den Wochenrecap (Stand vor ~7 Tagen)."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM trend_snapshots WHERE term = ? AND checked_at <= ? ORDER BY checked_at DESC LIMIT 1",
        (term, cutoff_iso),
    ).fetchone()
    conn.close()
    return row


def _insert_snapshot(term: str, total: int) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO trend_snapshots (term, total_count, checked_at) VALUES (?, ?, ?)",
        (term, total, _now_iso()),
    )
    conn.commit()
    conn.close()


def _add_watchlist(term: str, added_by: str) -> bool:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO trend_watchlist (term, added_by, added_at) VALUES (?, ?, ?)",
            (term, added_by, _now_iso()),
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok


def _remove_watchlist(term: str) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM trend_watchlist WHERE term = ?", (term,))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed


def _fetch_watchlist() -> list[sqlite3.Row]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM trend_watchlist ORDER BY term ASC").fetchall()
    conn.close()
    return rows


def _auto_terms() -> list[str]:
    """Automatisch aus den aktiven Snipe-Bot-Kanälen (!add/!list) abgeleitete
    Suchbegriffe – das sind schon genau die Marken/Kategorien, die im
    Geschäft aktiv gesnipet werden. Kein manuelles Pflegen nötig: sobald ein
    neuer Kanal per !add eine Suche bekommt, taucht er hier automatisch auf."""
    try:
        from vinted_bot import MONITORS
    except ImportError:
        return []
    return [name.replace("-", " ").strip() for name, urls in MONITORS.items() if urls]


# Breite Markt-Abdeckung, unabhängig davon was du selbst gerade snipst –
# Vinted bietet KEINE echte "was ist gerade angesagt"-Funktion an (keine
# öffentliche Trending-API, nur Suche pro Begriff). Das hier ist die
# nächstbeste Annäherung: eine große Liste bekannter Vintage-/Streetwear-
# Resale-Marken wird mitgetrackt, damit die tägliche Bewegung nicht nur
# deine eigenen Suchbegriffe zeigt, sondern die größten Marken-Gewinner
# über den ganzen Vintage-Markt hinweg – ohne dass du selbst was eingeben musst.
MARKET_BRANDS = [
    "nike", "adidas", "puma", "reebok", "champion", "carhartt", "levis",
    "wrangler", "lee", "the north face", "patagonia", "tommy hilfiger",
    "calvin klein", "ralph lauren", "lacoste", "fred perry", "burberry",
    "stone island", "cp company", "diesel", "guess", "versace",
    "dolce gabbana", "armani", "moschino", "ellesse", "kappa", "fila",
    "umbro", "sergio tacchini", "helly hansen", "timberland",
    "dr martens", "new balance", "converse", "vans", "supreme", "stussy",
    "palace", "off white", "north sails", "napapijri", "woolrich",
    "barbour", "schott", "true religion", "von dutch", "ed hardy",
    "jordan", "la martina", "blauer", "miss me",
]


def _market_terms() -> list[str]:
    """Feste Markt-Watchlist (siehe MARKET_BRANDS) – läuft immer mit,
    unabhängig von deinen eigenen Snipe-Bot-Kanälen."""
    return MARKET_BRANDS


def _effective_terms() -> list[str]:
    """Deine Snipe-Bot-Begriffe + manuell mit !trends-add hinzugefügte Extras
    + die feste Markt-Watchlist (MARKET_BRANDS), dedupliziert (Groß-/
    Kleinschreibung egal). Die Markt-Watchlist sorgt dafür, dass die
    'größte Bewegung'-Liste im täglichen Update nicht nur deine eigenen
    Suchbegriffe zeigt, sondern echte Marken-Gewinner über den ganzen
    Vintage-Markt – auch Marken, die du selbst gar nicht snipst."""
    seen: set[str] = set()
    merged: list[str] = []
    for term in _auto_terms() + [r["term"] for r in _fetch_watchlist()] + _market_terms():
        key = term.lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(term)
    return merged


def _buchhaltung_sales_stats(term: str) -> dict | None:
    """Best-effort-Abgleich mit der Buchhaltungs-DB: wie viele eigene Artikel
    mit passendem Namen (z.B. Marke) tatsächlich schon verkauft wurden, und
    wie schnell im Schnitt. Echte Verkaufszahlen statt nur Angebots-Proxy.
    Gibt None zurück, wenn die Buchhaltungs-DB fehlt oder nichts passt."""
    db_path = os.getenv("BUCHHALTUNG_DB", "buchhaltung.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, kauf_datum, verkauf_datum FROM artikel WHERE name LIKE ? COLLATE NOCASE",
            (f"%{term}%",),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    verkauft = [r for r in rows if r["status"] == "verkauft" and r["verkauf_datum"]]
    tage_liste = []
    for r in verkauft:
        try:
            kauf = datetime.fromisoformat(r["kauf_datum"])
            verk = datetime.fromisoformat(r["verkauf_datum"])
            tage_liste.append((verk - kauf).days)
        except (ValueError, TypeError):
            continue
    avg_tage = (sum(tage_liste) / len(tage_liste)) if tage_liste else None

    return {"anzahl_gesamt": len(rows), "anzahl_verkauft": len(verkauft), "avg_tage": avg_tage}


class TrendsError(Exception):
    pass


async def _fetch_total_count(term: str) -> int:
    # Import hier drin (nicht am Modulanfang), aus demselben Grund wie in
    # price_check.py: vinted_bot.py ist zur Ladezeit dieser Cog evtl. noch
    # nicht vollständig initialisiert.
    from vinted_bot import HEADERS, get_random_proxy, get_session_cookie

    p = get_random_proxy()
    proxy_url = p["url"] if p else None
    proxy_auth = p["auth"] if p else None

    params = {
        "search_text": term,
        "per_page": "1",
        "order": "newest_first",
    }

    async with aiohttp.ClientSession() as session:
        cookie_header = await get_session_cookie(session, proxy_url, proxy_auth)
        headers = dict(HEADERS)
        if cookie_header:
            headers["Cookie"] = cookie_header

        async with session.get(
            SEARCH_URL, params=params, headers=headers,
            proxy=proxy_url, proxy_auth=proxy_auth,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.error(f"Trend-Check Fehler (HTTP {r.status}): {body[:300]}")
                raise TrendsError(f"Vinted antwortete mit HTTP {r.status}.")
            data = await r.json()
            pagination = data.get("pagination") or {}
            total = pagination.get("total_entries")
            if total is not None:
                return int(total)
            # Fallback falls Vinted mal kein pagination-Feld liefert – grobe
            # Schätzung über die zurückgegebene Item-Liste.
            return len(data.get("items", []))


class Trends(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _init_db()
        self.daily_trend_loop.start()
        self.weekly_recap_loop.start()

    def cog_unload(self):
        self.daily_trend_loop.cancel()
        self.weekly_recap_loop.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    @staticmethod
    def _term_key(text: str) -> str:
        return text.strip().lower()

    # ── On-demand Check ──────────────────────────────────────────────────────
    @commands.command(name="trends")
    async def trends(self, ctx: commands.Context, *, suchbegriff: str = None):
        """!trends <Suchbegriff> – Trend-Check jetzt, mit Vergleich zum letzten Mal"""
        if not suchbegriff:
            await ctx.send("Gib einen Suchbegriff an, z.B. `!trends ralph lauren strickpullover`")
            return

        term_key = self._term_key(suchbegriff)
        async with ctx.typing():
            try:
                total = await _fetch_total_count(term_key)
            except Exception as e:
                await ctx.send(
                    f"❌ Trend-Check fehlgeschlagen: `{e}`\n"
                    "Vinted blockt manchmal einzelne Anfragen — meist hilft es, es gleich nochmal zu probieren."
                )
                return

            last = await asyncio.to_thread(_last_snapshot, term_key)
            await asyncio.to_thread(_insert_snapshot, term_key, total)

        embed = discord.Embed(title=f"📡 Trend-Radar: {suchbegriff}", color=COLOR)
        embed.add_field(name="Aktive Angebote jetzt", value=str(total), inline=True)

        if last:
            prev = last["total_count"]
            diff = total - prev
            pct = (diff / prev * 100) if prev else 0.0
            emoji = "📈" if diff > 0 else "📉" if diff < 0 else "➖"
            last_dt = datetime.fromisoformat(last["checked_at"])
            tage = max((datetime.now(timezone.utc) - last_dt).days, 0)
            embed.add_field(
                name=f"Vergleich zum letzten Check (vor {tage}d)",
                value=f"{emoji} {prev} → {total} ({pct:+.1f}%)",
                inline=True,
            )
            embed.description = (
                "Mehr aktive Angebote heißt mehr Konkurrenz, kann aber auch mehr Käufer-Interesse "
                "fürs Thema bedeuten. Am aussagekräftigsten zusammen mit `!preischeck` — "
                "steigende Angebote + stabile Preise = echte Nachfrage."
            )
        else:
            embed.description = (
                f"Erster Check für diesen Begriff — noch kein Vergleichswert. "
                f"Beim nächsten `!trends {suchbegriff}` siehst du dann die Veränderung."
            )
        await ctx.send(embed=embed)

    # ── Zusätzliche Begriffe (optional – die Snipe-Bot-Kanäle laufen automatisch) ──
    @commands.command(name="trends-add")
    async def trends_add(self, ctx: commands.Context, *, suchbegriff: str = None):
        """!trends-add <Suchbegriff> – zusätzlichen Begriff mittracken, über die
        automatisch erfassten Snipe-Bot-Kanäle hinaus (optional)"""
        if not suchbegriff:
            await ctx.send("Gib einen Suchbegriff an, z.B. `!trends-add ralph lauren strickpullover`")
            return
        term_key = self._term_key(suchbegriff)
        ok = await asyncio.to_thread(_add_watchlist, term_key, str(ctx.author))
        if not ok:
            await ctx.send(f"⚠️ **{suchbegriff}** wird schon getrackt.")
            return
        await ctx.send(
            f"✅ **{suchbegriff}** zusätzlich zur Watchlist hinzugefügt — kommt ab jetzt mit ins "
            f"tägliche Trend-Update und den Sonntags-Recap in #{TRENDS_CHANNEL_NAME}."
        )

    @commands.command(name="trends-entfernen")
    async def trends_entfernen(self, ctx: commands.Context, *, suchbegriff: str = None):
        """!trends-entfernen <Suchbegriff> – einen manuell hinzugefügten Begriff wieder
        entfernen (die automatischen Snipe-Bot-Kanäle lassen sich hierüber nicht entfernen,
        die folgen einfach deinen !add/!remove-Kanälen)"""
        if not suchbegriff:
            await ctx.send("Gib einen Suchbegriff an, den du entfernen willst.")
            return
        term_key = self._term_key(suchbegriff)
        removed = await asyncio.to_thread(_remove_watchlist, term_key)
        if removed:
            await ctx.send(f"🗑️ **{suchbegriff}** von der Watchlist entfernt.")
        else:
            await ctx.send(
                f"❌ **{suchbegriff}** war nicht als zusätzlicher Begriff hinterlegt. "
                "Kommt er automatisch über einen Snipe-Bot-Kanal? Dann einfach `!remove` beim "
                "Sniper nutzen, dann fällt er auch hier automatisch raus."
            )

    @commands.command(name="trends-liste")
    async def trends_liste(self, ctx: commands.Context):
        """!trends-liste – zeigt, was aktuell getrackt wird (automatisch + manuell + Markt-Watchlist)"""
        auto = await asyncio.to_thread(_auto_terms)
        manuell = await asyncio.to_thread(_fetch_watchlist)
        markt = await asyncio.to_thread(_market_terms)
        embed = discord.Embed(title="📋 Trend-Radar – was wird getrackt?", color=COLOR)
        if auto:
            embed.add_field(
                name=f"🎯 Automatisch (aus deinen Snipe-Bot-Kanälen, {len(auto)})",
                value="\n".join(f"• {t}" for t in auto), inline=False,
            )
        if manuell:
            embed.add_field(
                name=f"➕ Manuell hinzugefügt ({len(manuell)})",
                value="\n".join(f"• {r['term']}" for r in manuell), inline=False,
            )
        embed.add_field(
            name=f"🔥 Markt-Watchlist ({len(markt)}, immer dabei)",
            value=f"{', '.join(markt[:20])}, …" if len(markt) > 20 else ", ".join(markt),
            inline=False,
        )
        embed.set_footer(
            text=f"Markt-Watchlist sorgt für allgemeine Trends unabhängig von deinen eigenen Suchen • "
                 f"Tägliches Update + Sonntags-Recap in #{TRENDS_CHANNEL_NAME}"
        )
        await ctx.send(embed=embed)

    # ── Manuelle Test-Läufe (sofort sehen wie's aussieht, statt zu warten) ───
    @commands.command(name="trends-tagesupdate")
    async def trends_tagesupdate(self, ctx: commands.Context):
        """!trends-tagesupdate – Test: zeigt sofort, wie das tägliche Update aussieht"""
        async with ctx.typing():
            await self._run_daily_update(ctx=ctx, is_test=True)

    @commands.command(name="trends-wochenrecap")
    async def trends_wochenrecap(self, ctx: commands.Context):
        """!trends-wochenrecap – Test: zeigt sofort, wie der Sonntags-Recap aussieht"""
        async with ctx.typing():
            await self._run_weekly_recap(ctx=ctx, is_test=True)

    # ── Hilfe ────────────────────────────────────────────────────────────────
    @commands.command(name="trends-hilfe")
    async def trends_hilfe(self, ctx: commands.Context):
        """!trends-hilfe – Übersicht aller Trend-Radar-Commands"""
        embed = discord.Embed(
            title="📡 Trend-Radar – Hilfe",
            description="Läuft automatisch über deine Snipe-Bot-Kanäle plus einer festen "
                        "Markt-Watchlist bekannter Vintage-Marken — kein Setup nötig. Die "
                        "folgenden Befehle sind nur für Ad-hoc-Checks oder Extras.",
            color=COLOR,
        )
        embed.add_field(name="Ad-hoc-Check jetzt", value="`!trends <Suchbegriff>`", inline=False)
        embed.add_field(name="Zusätzlichen Begriff tracken", value="`!trends-add <Suchbegriff>`", inline=True)
        embed.add_field(name="Zusatz-Begriff entfernen", value="`!trends-entfernen <Suchbegriff>`", inline=True)
        embed.add_field(name="Was wird getrackt?", value="`!trends-liste`", inline=True)
        embed.add_field(name="Test: Tages-Update jetzt sehen", value="`!trends-tagesupdate`", inline=True)
        embed.add_field(name="Test: Wochenrecap jetzt sehen", value="`!trends-wochenrecap`", inline=True)
        embed.set_footer(
            text=f"Automatisch: täglich {TREND_DAILY_HOUR} Uhr + sonntags {TREND_WEEKLY_HOUR} Uhr "
                 f"in #{TRENDS_CHANNEL_NAME} (Europe/Berlin)"
        )
        await ctx.send(embed=embed)

    # ── Gemeinsame Logik für Tages-Update & Wochenrecap ──────────────────────
    async def _collect_results(self, baseline_fn) -> list[dict]:
        terms = await asyncio.to_thread(_effective_terms)
        if not terms:
            return []
        results = []
        for i, term in enumerate(terms):
            try:
                total = await _fetch_total_count(term)
            except Exception:
                log.warning(f"Trend-Check für '{term}' fehlgeschlagen, überspringe.")
                continue
            baseline = await asyncio.to_thread(baseline_fn, term)
            await asyncio.to_thread(_insert_snapshot, term, total)
            if baseline:
                prev = baseline["total_count"]
                diff = total - prev
                pct = (diff / prev * 100) if prev else 0.0
            else:
                diff = pct = None
            sales = await asyncio.to_thread(_buchhaltung_sales_stats, term)
            results.append({"term": term, "total": total, "diff": diff, "pct": pct, "sales": sales})
            # Kleine, zufällige Pause zwischen den Anfragen – jetzt wo die
            # Markt-Watchlist mit dazu kommt, sind das deutlich mehr Requests
            # pro Lauf (~70 statt ~20), daher etwas Abstand um Vinted nicht
            # mit vielen Anfragen in kurzer Zeit aufzufallen.
            if i < len(terms) - 1:
                await asyncio.sleep(random.uniform(1.5, 3.0))
        return results

    @staticmethod
    def _build_trend_embed(title: str, results: list[dict], *, no_baseline_label: str, is_test: bool) -> discord.Embed:
        movers = sorted([r for r in results if r["pct"] is not None], key=lambda r: r["pct"], reverse=True)
        new_terms = [r for r in results if r["pct"] is None]

        embed = discord.Embed(title=title, color=COLOR)
        if movers:
            lines = []
            for r in movers[:15]:
                emoji = "📈" if r["diff"] > 0 else "📉" if r["diff"] < 0 else "➖"
                lines.append(f"{emoji} **{r['term']}** — {r['total']} Angebote ({r['pct']:+.1f}%)")
            embed.add_field(name="📡 Angebots-Bewegung (Vinted)", value="\n".join(lines), inline=False)
        if new_terms:
            lines = [f"• **{r['term']}** — {r['total']} Angebote ({no_baseline_label})" for r in new_terms[:15]]
            embed.add_field(name="Noch kein Vergleichswert", value="\n".join(lines), inline=False)

        verkaeufer = sorted(
            [r for r in results if r["sales"] and r["sales"]["anzahl_verkauft"] > 0],
            key=lambda r: r["sales"]["anzahl_verkauft"], reverse=True,
        )
        if verkaeufer:
            lines = []
            for r in verkaeufer[:10]:
                s = r["sales"]
                tage = f", ⌀ {s['avg_tage']:.0f}d bis Verkauf" if s["avg_tage"] is not None else ""
                lines.append(f"🏆 **{r['term']}** — {s['anzahl_verkauft']}x verkauft{tage}")
            embed.add_field(name="Bei dir am besten weg (echte Verkäufe)", value="\n".join(lines), inline=False)

        footer = "Angebots-Bewegung = Proxy für Konkurrenz/Interesse. Echte Verkäufe kommen aus deiner Buchhaltung."
        if is_test:
            footer = "🧪 Test-Lauf (manuell ausgelöst, nicht der echte Zeitplan) • " + footer
        embed.set_footer(text=footer)
        return embed

    async def _post_result(self, ctx: commands.Context | None, embed: discord.Embed | None):
        if embed is None:
            if ctx:
                await ctx.send(
                    "📭 Nichts zu tracken — leg mit `!add <kanal> <vinted-url>` einen Snipe-Bot-Kanal an "
                    "(wird automatisch mitgetrackt) oder füge manuell einen Begriff mit "
                    "`!trends-add <Begriff>` hinzu."
                )
            return
        if ctx is not None:
            await ctx.send(embed=embed)
            return
        channel = discord.utils.get(self.bot.get_all_channels(), name=TRENDS_CHANNEL_NAME)
        if channel:
            await channel.send(embed=embed)
        else:
            log.warning(f"Trend-Update: Kanal '{TRENDS_CHANNEL_NAME}' nicht gefunden.")

    async def _run_daily_update(self, ctx: commands.Context = None, is_test: bool = False):
        results = await self._collect_results(_last_snapshot)
        embed = None
        if results:
            embed = self._build_trend_embed(
                "📡 Trend-Update von heute", results,
                no_baseline_label="Basiswert", is_test=is_test,
            )
        await self._post_result(ctx, embed)

    async def _run_weekly_recap(self, ctx: commands.Context = None, is_test: bool = False):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        def baseline_fn(term: str):
            return _snapshot_before(term, cutoff)

        results = await self._collect_results(baseline_fn)
        embed = None
        if results:
            embed = self._build_trend_embed(
                "📊 Wochenrecap", results,
                no_baseline_label="noch keine 7 Tage Historie", is_test=is_test,
            )
        await self._post_result(ctx, embed)

    @tasks.loop(time=DAILY_TIME)
    async def daily_trend_loop(self):
        try:
            await self._run_daily_update()
        except Exception:
            log.exception("Fehler im täglichen Trend-Update")

    @daily_trend_loop.before_loop
    async def before_daily_trend_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=WEEKLY_TIME)
    async def weekly_recap_loop(self):
        if datetime.now(BERLIN_TZ).weekday() != SUNDAY:
            return
        try:
            await self._run_weekly_recap()
        except Exception:
            log.exception("Fehler im wöchentlichen Trend-Recap")

    @weekly_recap_loop.before_loop
    async def before_weekly_recap_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Trends(bot))
