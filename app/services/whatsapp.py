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


# ── Interactive messages (buttons / lists) ─────────────────────────
#
# ⚠️ Verify before relying on these for anything real. Evolution API's
# button/list support is well-documented as unstable specifically on the
# Baileys (WhatsApp Web) connection — which is what a self-hosted instance
# like this one almost certainly uses, not Meta's official Cloud API.
# Multiple independent sources (Evolution's own GitHub issues, third-party
# client libraries, other unofficial WhatsApp providers) all say the same
# thing: rendering can silently break on a WhatsApp app update, entirely
# outside anyone's control, and isn't "fully supported" until you're on
# the official Cloud API. Test with the "test buttons" admin command
# before wiring this into the real ordering flow.
#
# The exact request field names below match Evolution API's documented
# shape as of this writing — if your instance rejects the payload, check
# its own API docs (usually at {EVOLUTION_API_URL}/docs or similar) for
# what your specific version actually expects, since this has changed
# across Evolution releases before (2.3.6 → 2.3.7 broke it entirely).

async def send_buttons(phone_number: str, body_text: str, buttons: list[dict], footer: str = "") -> None:
    """
    buttons: list of {"id": "...", "title": "..."}. WhatsApp hard limits,
    not Evolution's: max 3 buttons, title max 20 characters.
    """
    if len(buttons) > 3:
        raise ValueError(f"WhatsApp allows a maximum of 3 reply buttons, got {len(buttons)}")
    await _send_buttons_call(phone_number, body_text, buttons, footer)


@with_retry(max_attempts=2, base_delay=2.0)
async def _send_buttons_call(phone_number: str, body_text: str, buttons: list[dict], footer: str) -> None:
    url = f"{settings.evolution_api_url}/message/sendButtons/{settings.evolution_instance_name}"
    payload = {
        "number": _format_phone(phone_number),
        "title": body_text,
        "description": "",
        "footer": footer,
        "buttons": [{"buttonId": b["id"], "buttonText": b["title"]} for b in buttons],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=_evolution_headers())

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Evolution API error {response.status_code}: {response.text[:300]}")
    await log.info("WA_BUTTONS_SENT", metadata={"phone": phone_number[:6] + "****", "count": len(buttons)})


async def send_list(
    phone_number: str,
    title: str,
    body_text: str,
    button_text: str,
    sections: list[dict],
    footer: str = "",
) -> None:
    """
    sections: list of {"title": "...", "rows": [{"id": "...", "title": "...", "description": "..."}]}.
    WhatsApp hard limits: max 10 rows total across ALL sections combined,
    row title max 24 characters, row description max 72 characters.
    """
    total_rows = sum(len(s.get("rows", [])) for s in sections)
    if total_rows > 10:
        raise ValueError(f"WhatsApp allows a maximum of 10 list rows total, got {total_rows}")
    await _send_list_call(phone_number, title, body_text, button_text, sections, footer)


@with_retry(max_attempts=2, base_delay=2.0)
async def _send_list_call(
    phone_number: str, title: str, body_text: str, button_text: str, sections: list[dict], footer: str
) -> None:
    url = f"{settings.evolution_api_url}/message/sendList/{settings.evolution_instance_name}"
    payload = {
        "number": _format_phone(phone_number),
        "title": title,
        "description": body_text,
        "buttonText": button_text,
        "footerText": footer,
        "sections": sections,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=_evolution_headers())

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Evolution API error {response.status_code}: {response.text[:300]}")
    await log.info("WA_LIST_SENT", metadata={
        "phone": phone_number[:6] + "****",
        "rows": sum(len(s.get("rows", [])) for s in sections),
    })
