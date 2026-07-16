"""
Gemeinsamer Low-Level-Client für die kostenlose OpenRouter Chat-API.
Wird sowohl von der Bild-Analyse (ai_vision.py) als auch vom Reselling-Coach
(coach.py) genutzt.
"""

import json
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger("openrouter-client")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(Exception):
    pass


def is_enabled() -> bool:
    return bool(OPENROUTER_API_KEY)


async def chat(messages: list[dict], *, temperature: float = 0.4,
                model: Optional[str] = None) -> str:
    """Schickt eine Chat-Completion-Anfrage an OpenRouter und gibt den
    Antworttext zurück. Wirft OpenRouterError bei Netzwerk-/API-Fehlern."""
    if not OPENROUTER_API_KEY:
        raise OpenRouterError("Kein OPENROUTER_API_KEY gesetzt.")

    payload = {
        "model": model or OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": "Vinted Bot",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as r:
                body = await r.text()
                if r.status != 200:
                    log.error(f"OpenRouter API Fehler (HTTP {r.status}): {body[:400]}")
                    raise OpenRouterError(f"HTTP {r.status} von OpenRouter: {body[:200]}")
                data = json.loads(body)
    except OpenRouterError:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:
        kind = "Zeitüberschreitung" if isinstance(e, TimeoutError) else "Netzwerkfehler"
        log.error(f"OpenRouter {kind}: {e!r}")
        raise OpenRouterError(
            f"{kind} bei der KI-Anfrage — das freie Modell war diesmal zu langsam. Nochmal probieren."
        )

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        log.error(f"Konnte OpenRouter-Antwort nicht lesen: {e} — Rohdaten: {str(data)[:400]}")
        raise OpenRouterError("Antwort der KI konnte nicht gelesen werden.")
  
