"""
Experimenteller Client für Vinted's internes (nicht-öffentliches) API, um
Artikel automatisiert einzustellen.

WICHTIG – bitte lesen:
  Vinted bietet keine offizielle Public API zum Erstellen von Inseraten an.
  Alles hier basiert auf reverse-engineerten Endpunkten, die sich JEDERZEIT
  ohne Vorwarnung ändern können. Dieser Code ist ein erster Entwurf und
  wurde NICHT gegen einen echten Vinted-Account getestet (kein Live-Zugriff
  in dieser Umgebung möglich). Vor dem Produktiveinsatz:

    1. VINTED_SESSION_COOKIE in .env setzen (siehe SETUP.md)
    2. Mit einem günstigen Testartikel ausprobieren
    3. Bei Fehlern: die geloggte HTTP-Antwort ansehen und Endpunkte/Feldnamen
       anhand des Network-Tabs im Browser (beim manuellen Inserieren)
       abgleichen – dafür sind wir hier am besten gemeinsam in einer
       Folge-Session, in der du mir die tatsächlichen Responses zeigen kannst.

  Automatisiertes Erstellen von Inseraten verstößt vermutlich gegen Vintds
  Nutzungsbedingungen (ähnlich wie der Snipe-Bot) – Account-Risiko liegt bei
  dir, genau wie beim bestehenden Sniper.
"""

import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger("vinted-client")

BASE_URL = "https://www.vinted.de"

# Empirisch beobachtete Zustands-IDs (können sich ändern / je Land variieren).
CONDITION_IDS = {
    "neu mit etikett": 1,
    "neu ohne etikett": 2,
    "sehr gut": 3,
    "gut": 4,
    "zufriedenstellend": 5,
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/items/new",
    "Content-Type": "application/json",
}


class VintedAPIError(Exception):
    pass


class VintedClient:
    """Hält eine Session gegen Vinted offen und bündelt die Schritte, die zum
    Erstellen eines Inserats nötig sind. Best-effort / experimentell, siehe
    Modul-Docstring."""

    def __init__(self, session_cookie: Optional[str] = None):
        self.session_cookie = session_cookie or os.getenv("VINTED_SESSION_COOKIE")
        self._csrf_token: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.session_cookie)

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = dict(DEFAULT_HEADERS)
        if self.session_cookie:
            headers["Cookie"] = self.session_cookie
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        if extra:
            headers.update(extra)
        return headers

    async def _get_csrf_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Lädt die Startseite und liest den CSRF-Token aus dem <meta>-Tag.
        Vinted verlangt diesen Token als X-CSRF-Token Header bei POST-Requests."""
        if self._csrf_token:
            return self._csrf_token
        try:
            async with session.get(BASE_URL, headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                html = await r.text()
        except aiohttp.ClientError as e:
            log.error(f"CSRF-Token Abruf fehlgeschlagen: {e}")
            return None

        marker = 'name="csrf-token" content="'
        idx = html.find(marker)
        if idx == -1:
            log.error("CSRF-Token nicht in der Startseite gefunden – Vinted-Layout hat sich evtl. geändert.")
            return None
        start = idx + len(marker)
        end = html.find('"', start)
        self._csrf_token = html[start:end]
        return self._csrf_token

    async def verify_session(self, session: aiohttp.ClientSession) -> bool:
        """Prüft ob das Session-Cookie noch gültig ist (eingeloggt)."""
        if not self.enabled:
            return False
        await self._get_csrf_token(session)
        try:
            async with session.get(f"{BASE_URL}/api/v2/users/current",
                                    headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return True
                log.warning(f"Session-Check: HTTP {r.status} – Cookie evtl. abgelaufen.")
                return False
        except aiohttp.ClientError as e:
            log.error(f"Session-Check fehlgeschlagen: {e}")
            return False

    async def search_brand(self, session: aiohttp.ClientSession, query: str) -> Optional[int]:
        try:
            async with session.get(
                f"{BASE_URL}/api/v2/brands", params={"search_text": query},
                headers=self._headers(), timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    log.warning(f"Brand-Suche fehlgeschlagen (HTTP {r.status}) für '{query}'")
                    return None
                data = await r.json()
                brands = data.get("brands", [])
                if not brands:
                    return None
                # exakte Übereinstimmung bevorzugen, sonst erstes Ergebnis
                for b in brands:
                    if b.get("title", "").lower() == query.lower():
                        return b["id"]
                return brands[0]["id"]
        except aiohttp.ClientError as e:
            log.error(f"Brand-Suche Netzwerkfehler: {e}")
            return None

    async def search_catalog(self, session: aiohttp.ClientSession, query: str) -> Optional[int]:
        try:
            async with session.get(
                f"{BASE_URL}/api/v2/catalog/search_suggestions", params={"query": query},
                headers=self._headers(), timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    log.warning(f"Kategorie-Suche fehlgeschlagen (HTTP {r.status}) für '{query}'")
                    return None
                data = await r.json()
                suggestions = data.get("catalog_suggestions") or data.get("suggestions") or []
                if not suggestions:
                    return None
                return suggestions[0].get("id") or suggestions[0].get("catalog_id")
        except aiohttp.ClientError as e:
            log.error(f"Kategorie-Suche Netzwerkfehler: {e}")
            return None

    async def upload_photo(self, session: aiohttp.ClientSession, image_bytes: bytes,
                            filename: str) -> Optional[int]:
        form = aiohttp.FormData()
        form.add_field("photo[file]", image_bytes, filename=filename, content_type="image/jpeg")
        form.add_field("photo[type]", "item")
        try:
            headers = self._headers()
            headers.pop("Content-Type", None)  # aiohttp setzt den multipart-Header selbst
            async with session.post(f"{BASE_URL}/api/v2/photos", data=form, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status not in (200, 201):
                    body = await r.text()
                    log.error(f"Foto-Upload fehlgeschlagen (HTTP {r.status}): {body[:300]}")
                    return None
                data = await r.json()
                return data.get("photo", {}).get("id")
        except aiohttp.ClientError as e:
            log.error(f"Foto-Upload Netzwerkfehler: {e}")
            return None

    async def create_item(self, session: aiohttp.ClientSession, *, title: str, description: str,
                           price: float, brand_id: Optional[int], catalog_id: Optional[int],
                           condition: str, photo_ids: list[int]) -> Optional[dict]:
        status_id = CONDITION_IDS.get(condition.lower().strip())
        if status_id is None:
            log.warning(f"Unbekannter Zustand '{condition}', nutze 'Gut' als Fallback.")
            status_id = CONDITION_IDS["gut"]

        payload = {
            "item": {
                "title": title,
                "description": description,
                "price": f"{price:.2f}",
                "currency": "EUR",
                "status_id": status_id,
                "brand_id": brand_id,
                "catalog_id": catalog_id,
                "photo_ids": photo_ids,
                "assigned_photos": [{"id": pid, "orientation": 0} for pid in photo_ids],
            }
        }
        try:
            async with session.post(f"{BASE_URL}/api/v2/items", json=payload,
                                     headers=self._headers(),
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status not in (200, 201):
                    body = await r.text()
                    log.error(f"Item-Erstellung fehlgeschlagen (HTTP {r.status}): {body[:500]}")
                    raise VintedAPIError(f"HTTP {r.status}: {body[:300]}")
                data = await r.json()
                return data.get("item")
        except aiohttp.ClientError as e:
            log.error(f"Item-Erstellung Netzwerkfehler: {e}")
            raise VintedAPIError(str(e))

    async def create_listing(self, *, title: str, description: str, price: float,
                              brand: str, category: str, condition: str,
                              images: list[bytes]) -> dict:
        """High-level Flow: Session prüfen → Brand/Kategorie auflösen → Fotos hochladen
        → Item erstellen. Wirft VintedAPIError bei Fehlschlag, mit möglichst
        genauer Fehlermeldung für's Debugging."""
        if not self.enabled:
            raise VintedAPIError("Kein VINTED_SESSION_COOKIE gesetzt.")

        async with aiohttp.ClientSession() as session:
            if not await self.verify_session(session):
                raise VintedAPIError(
                    "Vinted-Session ungültig/abgelaufen. Cookie in .env erneuern "
                    "(im Browser eingeloggt, Cookie aus DevTools kopieren)."
                )

            brand_id = await self.search_brand(session, brand) if brand else None
            catalog_id = await self.search_catalog(session, category) if category else None

            photo_ids = []
            for i, img in enumerate(images):
                pid = await self.upload_photo(session, img, f"item_{i}.jpg")
                if pid:
                    photo_ids.append(pid)
            if not photo_ids:
                raise VintedAPIError("Keine Fotos konnten hochgeladen werden.")

            item = await self.create_item(
                session, title=title, description=description, price=price,
                brand_id=brand_id, catalog_id=catalog_id, condition=condition,
                photo_ids=photo_ids,
            )
            if not item:
                raise VintedAPIError("Vinted hat kein Item-Objekt zurückgegeben.")
            return item
