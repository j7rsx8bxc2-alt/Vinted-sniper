"""
Buchhaltungs-Bot – trackt Einkauf/Verkauf/Marge pro Artikel.

Speichert alles in einer lokalen SQLite-Datei (buchhaltung.db), damit nichts
verloren geht, auch wenn der Bot neu deployed wird (auf Railway ggf. Volume
mounten, siehe SETUP.md).

Commands:
  !kauf <name> <preis> [notiz...]   – neuen Artikel einkaufen (Lagerbestand +1)
  !verkauf <id> <preis>             – Artikel als verkauft markieren, Marge wird berechnet
  !lager                            – alle Artikel die noch nicht verkauft sind
  !verkauft [tage]                  – verkaufte Artikel (optional: nur letzte X Tage)
  !artikel <id>                     – Details zu einem Artikel
  !loeschen <id>                    – Artikel löschen (z.B. Fehleingabe)
  !bilanz [tage]                    – Gesamtübersicht: Einkauf, Umsatz, Marge, Lagerwert
  !export                           – komplette Buchhaltung als CSV
  !vinted-link <id> <url/id>        – Artikel mit einem Vinted-Inserat verknüpfen
  !verkauft-check                   – manuell prüfen ob verknüpfte Artikel noch aktiv sind
"""

import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from .access import is_vip_or_admin

log = logging.getLogger("buchhaltung")

DB_PATH = os.getenv("BUCHHALTUNG_DB", "buchhaltung.db")
COLOR = 0x09B1BA
VERKAUFT_CHECK_MINUTES = int(os.getenv("VERKAUFT_CHECK_MINUTES", "60"))
VERKAUFT_CHECK_CHANNEL = os.getenv("VERKAUFT_CHECK_CHANNEL") or os.getenv("LISTING_CHANNEL_NAME", "verkauf")


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
        CREATE TABLE IF NOT EXISTS artikel (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            einkaufspreis  REAL NOT NULL,
            verkaufspreis  REAL,
            status         TEXT NOT NULL DEFAULT 'lager',
            kauf_datum     TEXT NOT NULL,
            verkauf_datum  TEXT,
            notiz          TEXT
        )
        """
    )
    # Nachträglich hinzugefügte Spalten – bei bereits bestehenden Datenbanken
    # per ALTER TABLE ergänzen (schlägt fehl wenn schon vorhanden, das ist ok).
    for ddl in (
        "ALTER TABLE artikel ADD COLUMN vinted_item_id TEXT",
        "ALTER TABLE artikel ADD COLUMN vinted_notified INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # Spalte existiert schon
    conn.commit()
    conn.close()


def _extract_vinted_id(text: str) -> str | None:
    """Akzeptiert entweder eine reine Vinted-Item-ID oder einen kompletten
    Link wie https://www.vinted.de/items/1234567890-titel-slug."""
    text = text.strip()
    if text.isdigit():
        return text
    match = re.search(r"/items/(\d+)", text)
    return match.group(1) if match else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_eur(value: float) -> str:
    return f"{value:.2f} €"


class Buchhaltung(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _init_db()
        self.check_sold_loop.start()

    def cog_unload(self):
        self.check_sold_loop.cancel()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    # ── Einkauf ──────────────────────────────────────────────────────────────
    @commands.command(name="kauf")
    async def kauf(self, ctx: commands.Context, name: str, preis: float, *, notiz: str = ""):
        """!kauf <name> <einkaufspreis> [notiz]"""

        def _insert():
            conn = _connect()
            cur = conn.execute(
                "INSERT INTO artikel (name, einkaufspreis, status, kauf_datum, notiz) "
                "VALUES (?, ?, 'lager', ?, ?)",
                (name, preis, _now_iso(), notiz or None),
            )
            conn.commit()
            new_id = cur.lastrowid
            conn.close()
            return new_id

        new_id = await asyncio.to_thread(_insert)
        embed = discord.Embed(title="🧾 Einkauf erfasst", color=COLOR)
        embed.add_field(name="ID", value=f"`{new_id}`", inline=True)
        embed.add_field(name="Artikel", value=name, inline=True)
        embed.add_field(name="Einkaufspreis", value=_fmt_eur(preis), inline=True)
        if notiz:
            embed.add_field(name="Notiz", value=notiz, inline=False)
        embed.set_footer(text=f"Verkaufen mit: !verkauf {new_id} <preis>")
        await ctx.send(embed=embed)

    # ── Verkauf ──────────────────────────────────────────────────────────────
    @commands.command(name="verkauf")
    async def verkauf(self, ctx: commands.Context, artikel_id: int, preis: float):
        """!verkauf <id> <verkaufspreis>"""

        def _update():
            conn = _connect()
            row = conn.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,)).fetchone()
            if row is None:
                conn.close()
                return None
            conn.execute(
                "UPDATE artikel SET verkaufspreis = ?, status = 'verkauft', verkauf_datum = ? "
                "WHERE id = ?",
                (preis, _now_iso(), artikel_id),
            )
            conn.commit()
            conn.close()
            return row

        row = await asyncio.to_thread(_update)
        if row is None:
            await ctx.send(f"❌ Kein Artikel mit ID `{artikel_id}` gefunden.")
            return
        if row["status"] == "verkauft":
            await ctx.send(f"⚠️ Artikel `{artikel_id}` war bereits als verkauft markiert – Preis aktualisiert.")

        marge = preis - row["einkaufspreis"]
        marge_pct = (marge / row["einkaufspreis"] * 100) if row["einkaufspreis"] else 0
        emoji = "📈" if marge >= 0 else "📉"

        embed = discord.Embed(title=f"{emoji} Verkauf erfasst", color=COLOR)
        embed.add_field(name="ID", value=f"`{artikel_id}`", inline=True)
        embed.add_field(name="Artikel", value=row["name"], inline=True)
        embed.add_field(name="Verkaufspreis", value=_fmt_eur(preis), inline=True)
        embed.add_field(name="Einkaufspreis", value=_fmt_eur(row["einkaufspreis"]), inline=True)
        embed.add_field(name="Marge", value=f"{_fmt_eur(marge)} ({marge_pct:+.1f}%)", inline=True)
        await ctx.send(embed=embed)

    # ── Lagerbestand ─────────────────────────────────────────────────────────
    @commands.command(name="lager")
    async def lager(self, ctx: commands.Context):
        """!lager – zeigt alle noch nicht verkauften Artikel"""

        def _fetch():
            conn = _connect()
            rows = conn.execute(
                "SELECT * FROM artikel WHERE status = 'lager' ORDER BY kauf_datum ASC"
            ).fetchall()
            conn.close()
            return rows

        rows = await asyncio.to_thread(_fetch)
        if not rows:
            await ctx.send("📭 Lager ist leer – keine offenen Artikel.")
            return

        total_invested = sum(r["einkaufspreis"] for r in rows)
        embed = discord.Embed(
            title=f"📦 Lager ({len(rows)} Artikel)",
            description=f"Gebundenes Kapital: **{_fmt_eur(total_invested)}**",
            color=COLOR,
        )
        for r in rows[:25]:
            kauf_dt = datetime.fromisoformat(r["kauf_datum"])
            tage = (datetime.now(timezone.utc) - kauf_dt).days
            embed.add_field(
                name=f"#{r['id']} – {r['name']}",
                value=f"{_fmt_eur(r['einkaufspreis'])} • seit {tage}d",
                inline=True,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"… und {len(rows) - 25} weitere. Nutze !bilanz für die Gesamtübersicht.")
        await ctx.send(embed=embed)

    # ── Verkaufte Artikel ────────────────────────────────────────────────────
    @commands.command(name="verkauft")
    async def verkauft(self, ctx: commands.Context, tage: int = None):
        """!verkauft [tage] – zeigt verkaufte Artikel, optional nur letzte X Tage"""

        def _fetch():
            conn = _connect()
            if tage is not None:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=tage)).isoformat()
                rows = conn.execute(
                    "SELECT * FROM artikel WHERE status = 'verkauft' AND verkauf_datum >= ? "
                    "ORDER BY verkauf_datum DESC",
                    (cutoff,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM artikel WHERE status = 'verkauft' ORDER BY verkauf_datum DESC"
                ).fetchall()
            conn.close()
            return rows

        rows = await asyncio.to_thread(_fetch)
        if not rows:
            await ctx.send("📭 Keine verkauften Artikel im gewählten Zeitraum.")
            return

        titel = f"💰 Verkauft (letzte {tage} Tage)" if tage else "💰 Alle Verkäufe"
        embed = discord.Embed(title=titel, color=COLOR)
        for r in rows[:25]:
            marge = r["verkaufspreis"] - r["einkaufspreis"]
            embed.add_field(
                name=f"#{r['id']} – {r['name']}",
                value=f"{_fmt_eur(r['einkaufspreis'])} → {_fmt_eur(r['verkaufspreis'])} "
                      f"({'+' if marge >= 0 else ''}{marge:.2f} €)",
                inline=False,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"… und {len(rows) - 25} weitere.")
        await ctx.send(embed=embed)

    # ── Einzelner Artikel ────────────────────────────────────────────────────
    @commands.command(name="artikel")
    async def artikel(self, ctx: commands.Context, artikel_id: int):
        """!artikel <id> – Details zu einem Artikel"""

        def _fetch():
            conn = _connect()
            row = conn.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,)).fetchone()
            conn.close()
            return row

        row = await asyncio.to_thread(_fetch)
        if row is None:
            await ctx.send(f"❌ Kein Artikel mit ID `{artikel_id}` gefunden.")
            return

        embed = discord.Embed(title=f"🗂️ Artikel #{row['id']} – {row['name']}", color=COLOR)
        embed.add_field(name="Status", value=row["status"], inline=True)
        embed.add_field(name="Einkaufspreis", value=_fmt_eur(row["einkaufspreis"]), inline=True)
        if row["verkaufspreis"] is not None:
            marge = row["verkaufspreis"] - row["einkaufspreis"]
            embed.add_field(name="Verkaufspreis", value=_fmt_eur(row["verkaufspreis"]), inline=True)
            embed.add_field(name="Marge", value=_fmt_eur(marge), inline=True)
        embed.add_field(name="Eingekauft am", value=row["kauf_datum"][:10], inline=True)
        if row["verkauf_datum"]:
            embed.add_field(name="Verkauft am", value=row["verkauf_datum"][:10], inline=True)
        if row["notiz"]:
            embed.add_field(name="Notiz", value=row["notiz"], inline=False)
        await ctx.send(embed=embed)

    # ── Löschen ──────────────────────────────────────────────────────────────
    @commands.command(name="loeschen")
    async def loeschen(self, ctx: commands.Context, artikel_id: int):
        """!loeschen <id> – Artikel aus der Buchhaltung entfernen"""

        def _delete():
            conn = _connect()
            row = conn.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,)).fetchone()
            if row is not None:
                conn.execute("DELETE FROM artikel WHERE id = ?", (artikel_id,))
                conn.commit()
            conn.close()
            return row

        row = await asyncio.to_thread(_delete)
        if row is None:
            await ctx.send(f"❌ Kein Artikel mit ID `{artikel_id}` gefunden.")
            return
        await ctx.send(f"🗑️ Artikel #{artikel_id} ({row['name']}) gelöscht.")

    # ── Bilanz ───────────────────────────────────────────────────────────────
    @commands.command(name="bilanz")
    async def bilanz(self, ctx: commands.Context, tage: int = None):
        """!bilanz [tage] – Gesamtübersicht über Einkauf, Umsatz und Marge"""

        def _fetch():
            conn = _connect()
            if tage is not None:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=tage)).isoformat()
                verkauft = conn.execute(
                    "SELECT * FROM artikel WHERE status = 'verkauft' AND verkauf_datum >= ?",
                    (cutoff,),
                ).fetchall()
            else:
                verkauft = conn.execute("SELECT * FROM artikel WHERE status = 'verkauft'").fetchall()
            lager = conn.execute("SELECT * FROM artikel WHERE status = 'lager'").fetchall()
            conn.close()
            return verkauft, lager

        verkauft, lager = await asyncio.to_thread(_fetch)

        umsatz = sum(r["verkaufspreis"] for r in verkauft)
        einkauf_verkauft = sum(r["einkaufspreis"] for r in verkauft)
        gewinn = umsatz - einkauf_verkauft
        marge_pct = (gewinn / einkauf_verkauft * 100) if einkauf_verkauft else 0
        lagerwert = sum(r["einkaufspreis"] for r in lager)

        titel = f"📊 Bilanz (letzte {tage} Tage)" if tage else "📊 Gesamtbilanz"
        embed = discord.Embed(title=titel, color=COLOR)
        embed.add_field(name="Verkaufte Artikel", value=str(len(verkauft)), inline=True)
        embed.add_field(name="Umsatz", value=_fmt_eur(umsatz), inline=True)
        embed.add_field(name="Einkaufswert (verkauft)", value=_fmt_eur(einkauf_verkauft), inline=True)
        embed.add_field(name="Gewinn", value=f"{_fmt_eur(gewinn)} ({marge_pct:+.1f}%)", inline=True)
        embed.add_field(name="Artikel im Lager", value=str(len(lager)), inline=True)
        embed.add_field(name="Gebundenes Kapital (Lager)", value=_fmt_eur(lagerwert), inline=True)
        await ctx.send(embed=embed)

    # ── CSV-Export ───────────────────────────────────────────────────────────
    @commands.command(name="export")
    async def export(self, ctx: commands.Context):
        """!export – komplette Buchhaltung als CSV-Datei"""

        def _fetch():
            conn = _connect()
            rows = conn.execute("SELECT * FROM artikel ORDER BY id ASC").fetchall()
            conn.close()
            return rows

        rows = await asyncio.to_thread(_fetch)
        if not rows:
            await ctx.send("📭 Noch keine Daten zum Exportieren.")
            return

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "name", "einkaufspreis", "verkaufspreis", "marge", "status",
                          "kauf_datum", "verkauf_datum", "notiz"])
        for r in rows:
            marge = (r["verkaufspreis"] - r["einkaufspreis"]) if r["verkaufspreis"] is not None else ""
            writer.writerow([r["id"], r["name"], r["einkaufspreis"], r["verkaufspreis"] or "",
                              marge, r["status"], r["kauf_datum"], r["verkauf_datum"] or "",
                              r["notiz"] or ""])
        buf.seek(0)
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        filename = f"buchhaltung_{datetime.now().strftime('%Y%m%d')}.csv"
        await ctx.send("📥 Export bereit:", file=discord.File(data, filename=filename))

    # ── Vinted-Verknüpfung ───────────────────────────────────────────────────
    @commands.command(name="vinted-link")
    async def vinted_link(self, ctx: commands.Context, artikel_id: int, vinted_ref: str):
        """!vinted-link <id> <vinted-url-oder-id> – Artikel mit Vinted-Inserat verknüpfen,
        damit der Bot automatisch prüfen kann ob er noch aktiv ist."""
        vinted_id = _extract_vinted_id(vinted_ref)
        if not vinted_id:
            await ctx.send("❌ Konnte keine Vinted-Item-ID aus deiner Eingabe lesen. "
                            "Gib entweder die reine ID oder den vollen Link an.")
            return

        def _update():
            conn = _connect()
            row = conn.execute("SELECT * FROM artikel WHERE id = ?", (artikel_id,)).fetchone()
            if row is None:
                conn.close()
                return None
            conn.execute(
                "UPDATE artikel SET vinted_item_id = ?, vinted_notified = 0 WHERE id = ?",
                (vinted_id, artikel_id),
            )
            conn.commit()
            conn.close()
            return row

        row = await asyncio.to_thread(_update)
        if row is None:
            await ctx.send(f"❌ Kein Artikel mit ID `{artikel_id}` gefunden.")
            return
        await ctx.send(
            f"🔗 Artikel #{artikel_id} ({row['name']}) mit Vinted-Item `{vinted_id}` verknüpft. "
            f"Ich prüfe alle {VERKAUFT_CHECK_MINUTES} Minuten automatisch, ob er noch aktiv ist."
        )

    async def _check_vinted_item(self, vinted_id: str) -> str:
        """Gibt 'aktiv', 'verkauft_oder_entfernt' oder 'fehler' zurück."""
        from vinted_bot import HEADERS, get_random_proxy, get_session_cookie

        p = get_random_proxy()
        proxy_url = p["url"] if p else None
        proxy_auth = p["auth"] if p else None

        try:
            async with aiohttp.ClientSession() as session:
                cookie_header = await get_session_cookie(session, proxy_url, proxy_auth)
                headers = dict(HEADERS)
                if cookie_header:
                    headers["Cookie"] = cookie_header
                url = f"https://www.vinted.de/api/v2/items/{vinted_id}"
                async with session.get(
                    url, headers=headers, proxy=proxy_url, proxy_auth=proxy_auth,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 404:
                        return "verkauft_oder_entfernt"
                    if r.status != 200:
                        return "fehler"
                    data = await r.json()
                    item = data.get("item", {})
                    # Best-effort: Vintes Feldnamen dafür sind nicht offiziell dokumentiert.
                    if item.get("is_reserved") or str(item.get("status", "")).lower() in ("sold", "reserved"):
                        return "verkauft_oder_entfernt"
                    return "aktiv"
        except (aiohttp.ClientError, TimeoutError):
            return "fehler"

    async def _run_sold_check(self, ctx: commands.Context = None) -> list[dict]:
        """Prüft alle verknüpften Lager-Artikel, gibt Liste der auffälligen zurück."""

        def _fetch():
            conn = _connect()
            rows = conn.execute(
                "SELECT * FROM artikel WHERE status = 'lager' AND vinted_item_id IS NOT NULL "
                "AND vinted_item_id != '' AND vinted_notified = 0"
            ).fetchall()
            conn.close()
            return rows

        rows = await asyncio.to_thread(_fetch)
        auffaellig = []
        for row in rows:
            result = await self._check_vinted_item(row["vinted_item_id"])
            if result == "verkauft_oder_entfernt":
                auffaellig.append(dict(row))

                def _mark(article_id=row["id"]):
                    conn = _connect()
                    conn.execute("UPDATE artikel SET vinted_notified = 1 WHERE id = ?", (article_id,))
                    conn.commit()
                    conn.close()

                await asyncio.to_thread(_mark)
        return auffaellig

    @commands.command(name="verkauft-check")
    async def verkauft_check(self, ctx: commands.Context):
        """!verkauft-check – prüft manuell alle verknüpften Artikel auf Vinted-Status"""
        async with ctx.typing():
            auffaellig = await self._run_sold_check(ctx)
        if not auffaellig:
            await ctx.send("✅ Alle verknüpften Lager-Artikel scheinen noch aktiv auf Vinted zu sein.")
            return
        await self._send_sold_alert(ctx.channel, auffaellig)

    async def _send_sold_alert(self, channel: discord.abc.Messageable, artikel_liste: list[dict]):
        embed = discord.Embed(
            title="👀 Vermutlich verkauft",
            description="Diese Artikel sind auf Vinted nicht mehr auffindbar (verkauft, reserviert "
                        "oder manuell entfernt). Bitte kurz prüfen und mit `!verkauf <id> <preis>` "
                        "abschließen, falls verkauft.",
            color=COLOR,
        )
        for a in artikel_liste[:15]:
            embed.add_field(
                name=f"#{a['id']} – {a['name']}",
                value=f"Einkauf: {_fmt_eur(a['einkaufspreis'])} • `!verkauf {a['id']} <preis>`",
                inline=False,
            )
        await channel.send(embed=embed)

    @tasks.loop(minutes=VERKAUFT_CHECK_MINUTES)
    async def check_sold_loop(self):
        try:
            auffaellig = await self._run_sold_check()
        except Exception:
            log.exception("Fehler im automatischen Verkauft-Check")
            return
        if not auffaellig:
            return
        channel = discord.utils.get(self.bot.get_all_channels(), name=VERKAUFT_CHECK_CHANNEL)
        if channel:
            await self._send_sold_alert(channel, auffaellig)
        else:
            log.warning(f"Verkauft-Check: Kanal '{VERKAUFT_CHECK_CHANNEL}' nicht gefunden für Alarm.")

    @check_sold_loop.before_loop
    async def before_check_sold_loop(self):
        await self.bot.wait_until_ready()

    # ── Hilfe ────────────────────────────────────────────────────────────────
    @commands.command(name="buchhaltung")
    async def buchhaltung_hilfe(self, ctx: commands.Context):
        """!buchhaltung – Übersicht aller Buchhaltungs-Commands"""
        embed = discord.Embed(title="🧾 Buchhaltung – Hilfe", color=COLOR)
        embed.add_field(name="Einkauf erfassen", value="`!kauf <name> <preis> [notiz]`", inline=False)
        embed.add_field(name="Verkauf erfassen", value="`!verkauf <id> <preis>`", inline=False)
        embed.add_field(name="Lagerbestand", value="`!lager`", inline=True)
        embed.add_field(name="Verkäufe", value="`!verkauft [tage]`", inline=True)
        embed.add_field(name="Artikel-Details", value="`!artikel <id>`", inline=True)
        embed.add_field(name="Löschen", value="`!loeschen <id>`", inline=True)
        embed.add_field(name="Bilanz", value="`!bilanz [tage]`", inline=True)
        embed.add_field(name="CSV-Export", value="`!export`", inline=True)
        embed.add_field(name="Mit Vinted verknüpfen", value="`!vinted-link <id> <url/id>`", inline=True)
        embed.add_field(name="Verkauft-Check manuell", value="`!verkauft-check`", inline=True)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Buchhaltung(bot))
