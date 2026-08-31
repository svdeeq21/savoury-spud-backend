# savoury-spud-backend/app/services/whatsapp.py
#
# Adapted from the real-estate backend's whatsapp.py. Same retry-safety
# boundary (audit fix H3 there): only the network call itself is retried,
# never anything after a confirmed-successful send — otherwise a retry
# after a late exception re-sends a message that already went through.

import asyncio
import random
import httpx

from app.core.config import get_settings
from app.utils.logger import log
from app.utils.retry import with_retry

settings = get_settings()

_MIN_DELAY = 1.0
_MAX_DELAY = 2.5


def _evolution_headers() -> dict:
    return {
        "apikey": settings.evolution_api_key,
        "Content-Type": "application/json",
    }


def _format_phone(phone: str) -> str:
    return phone.lstrip("+").replace(" ", "").replace("-", "")


@with_retry(max_attempts=3, base_delay=2.0)
async def _send_text_wa_call(phone_number: str, text: str, delay: float) -> None:
    url = f"{settings.evolution_api_url}/message/sendText/{settings.evolution_instance_name}"
    payload = {
        "number": _format_phone(phone_number),
        "text": text,
        "options": {"delay": int(delay * 1000), "presence": "composing"},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=_evolution_headers())

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Evolution API error {response.status_code}: {response.text[:200]}")


async def send_message(phone_number: str, text: str) -> bool:
    """Customer-facing send — includes a small human-typing delay."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    await asyncio.sleep(delay)
    await _send_text_wa_call(phone_number, text, delay)
    await log.info("WA_MESSAGE_SENT", metadata={"phone": phone_number[:6] + "****"})
    return True


async def send_admin_alert(phone_number: str, text: str) -> bool:
    """Internal notification to the owner (new order, low-confidence parse, etc) — no artificial typing delay."""
    await _send_text_wa_call(phone_number, text, 0.0)
    await log.info("WA_ADMIN_ALERT_SENT", metadata={"phone": phone_number[:6] + "****"})
    return True
