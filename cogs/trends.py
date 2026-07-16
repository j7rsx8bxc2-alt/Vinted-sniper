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
    'größte Bewegung'-Liste im täglichen
