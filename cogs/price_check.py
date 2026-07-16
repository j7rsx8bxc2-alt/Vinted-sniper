"""
Preis-Check – durchsucht aktuelle Vinted-Angebote zu einem Suchbegriff und
zeigt eine Preisspanne (Min/Median/Max) plus ein paar Beispiel-Inserate, damit
man beim Einstellen eines eigenen Artikels realistisch bepreisen kann.

Nutzt dieselbe Fetch-Logik (Proxy-Rotation, Session-Cookie) wie der
Snipe-Bot in vinted_bot.py – das sind GET-Requests auf die öffentliche
Katalog-Suche, funktionieren also genau wie beim Sniper zuverlässig
(im Gegensatz zum experimentellen Auto-Post, der von Vinteds Bot-Schutz
blockiert wird – Preis-Check liest nur, schreibt nichts).

Command:
  !preischeck <Suchbegriff>   z.B. !preischeck ralph lauren strickpullover
"""

import logging
import statistics
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands

from .access import is_vip_or_admin

log = logging.getLogger("price-check")

COLOR = 0x09B1BA
SEARCH_URL = "https://www.vinted.de/api/v2/catalog/items"
MAX_RESULTS_SHOWN = 5


class PriceCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return is_vip_or_admin(ctx.author)

    async def _fetch_items(self, query: str) -> list[dict]:
        # Import hier drin (nicht am Modulanfang), weil vinted_bot.py erst zur
        # Laufzeit vollständig geladen ist, wenn diese Cog per load_extension
        # eingebunden wird – so gibt's keine Probleme mit der Ladereihenfolge.
        from vinted_bot import HEADERS, get_random_proxy, get_session_cookie

        p = get_random_proxy()
        proxy_url = p["url"] if p else None
        proxy_auth = p["auth"] if p else None

        params = {
            "search_text": query,
            "per_page": "40",
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
                    log.error(f"Preis-Check Fehler (HTTP {r.status}): {body[:300]}")
                    raise RuntimeError(f"Vinted antwortete mit HTTP {r.status}.")
                data = await r.json()
                return data.get("items", [])

    @staticmethod
    def _price(item: dict) -> float | None:
        price_raw = item.get("price", {})
        try:
            if isinstance(price_raw, dict):
                return float(str(price_raw.get("amount", "")).replace(",", "."))
            return float(str(price_raw).replace(",", "."))
        except (ValueError, TypeError):
            return None

    @commands.command(name="preischeck")
    async def preischeck(self, ctx: commands.Context, *, suchbegriff: str = None):
        if not suchbegriff:
            await ctx.send("Gib einen Suchbegriff an, z.B. `!preischeck ralph lauren strickpullover`")
            return

        async with ctx.typing():
            try:
                items = await self._fetch_items(suchbegriff)
            except Exception as e:
                await ctx.send(
                    f"❌ Preis-Check fehlgeschlagen: `{e}`\n"
                    "Vinted blockt manchmal einzelne Anfragen — meist hilft es, es gleich nochmal zu probieren."
                )
                return

            prices = [p for p in (self._price(i) for i in items) if p is not None and p > 0]
            if not prices:
                await ctx.send(f"📭 Keine aktuellen Angebote zu **{suchbegriff}** gefunden.")
                return

            prices.sort()
            stats_embed = discord.Embed(
                title=f"💶 Preis-Check: {suchbegriff}",
                description=f"Basierend auf {len(prices)} aktuellen Vinted-Angeboten (aktuelle Angebotspreise, "
                            f"keine garantierten Verkaufspreise).",
                color=COLOR,
            )
            stats_embed.add_field(name="Min", value=f"{prices[0]:.2f} €", inline=True)
            stats_embed.add_field(name="Median", value=f"{statistics.median(prices):.2f} €", inline=True)
            stats_embed.add_field(name="Max", value=f"{prices[-1]:.2f} €", inline=True)
            stats_embed.add_field(name="Durchschnitt", value=f"{statistics.mean(prices):.2f} €", inline=True)

            examples = []
            for item in items[:MAX_RESULTS_SHOWN]:
                price = self._price(item)
                if price is None:
                    continue
                title = item.get("title", "?")[:60]
                item_id = item.get("id")
                url = f"https://www.vinted.de/items/{item_id}" if item_id else None
                line = f"{price:.2f} € — {title}"
                examples.append(f"[{line}]({url})" if url else line)
            if examples:
                stats_embed.add_field(name="Beispiele", value="\n".join(examples), inline=False)

            await ctx.send(embed=stats_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PriceCheck(bot))
