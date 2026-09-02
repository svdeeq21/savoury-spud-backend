# savoury-spud-backend/app/services/whatsapp.py
#
# Adapted from the real-estate backend's whatsapp.py. Same retry-safety
# boundary (audit fix H3 there): only the network call itself is retried,
# never anything after a confirmed-successful send — otherwise a retry
# after a late exception re-sends a message that already went through.
#
# ── A note on send_list / send_buttons before you wire these up live ──
#
# These call Evolution API's POST /message/sendList/{instance} and
# POST /message/sendButtons/{instance}, which is what turns a reply into
# WhatsApp's native tappable list/button UI (the "View Plans" screen this
# was built to match). The payload shapes below are correct as of Evolution
# API's current documented API (doc.evolution-api.com) — but two things are
# worth knowing before this goes near a real customer:
#
#   1. Field names DO drift between Evolution API versions/forks (there's
#      an open history of breaking changes to these two endpoints
#      specifically — sendButtons/sendList regressed between 2.3.6 and
#      2.3.7, for example). If a call starts failing after an Evolution
#      upgrade, checking that instance's own `/docs` (Swagger) page for the
#      current request shape is the first thing to do, before assuming the
#      code here is wrong.
#   2. More fundamentally: interactive buttons/lists are a WhatsApp Business
#      Platform (Cloud API) feature that Baileys (the WhatsApp Web protocol
#      Evolution uses for a normal, non-Business-API-registered number) is
#      only ever unofficially reproducing — WhatsApp doesn't support them
#      on that protocol, Baileys support for them has been described as
#      "likely to be discontinued", and there are live reports of
#      sendButtons returning 201 while the message never actually renders
#      on the recipient's phone. In short: this works well on genuine Cloud
#      API instances, and is a coin flip on a Baileys one — test it against
#      your real instance before trusting it for anything customer-facing.
#
# Because of (2), every send in this module always carries a plain-text
# fallback of the same content: if the interactive call raises, it's
# caught and a normal text message goes out instead, so a rendering
# failure degrades to "less pretty" rather than "customer gets nothing".

import asyncio
import random
from typing import Optional
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


# ── Interactive messages (lists + buttons) ──────────────────────
#
# Both take a `body` (what shows above the tappable UI — always visible
# even on a client that can't render the interactive part at all) and
# both fall back to plain text carrying that same `body` if the
# interactive send itself throws. Callers should write `body` so it
# reads fine as an ordinary chat message on its own (e.g. spell out the
# options in the text too), since on a flaky Baileys instance that's
# sometimes literally all the customer will see.

@with_retry(max_attempts=2, base_delay=2.0)
async def _send_list_wa_call(phone_number: str, title: str, body: str, button_text: str,
                              sections: list[dict], footer: Optional[str], delay: float) -> None:
    url = f"{settings.evolution_api_url}/message/sendList/{settings.evolution_instance_name}"
    payload = {
        "number": _format_phone(phone_number),
        "options": {"delay": int(delay * 1000), "presence": "composing"},
        "listMessage": {
            "title": title,
            "description": body,
            "buttonText": button_text,
            "footerText": footer or "",
            "sections": sections,
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=_evolution_headers())

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Evolution API sendList error {response.status_code}: {response.text[:200]}")


async def send_list(
    phone_number: str,
    title: str,
    body: str,
    button_text: str,
    sections: list[dict],
    footer: Optional[str] = None,
) -> bool:
    """
    sections: [{"title": "Loaded Fries", "rows": [{"title": "Regular", "description": "₦4,500", "rowId": "Regular"}, ...]}, ...]

    `rowId` is deliberately just the plain display name (product/modifier
    name), not a synthetic ID — a tap then arrives back through the
    webhook indistinguishable from the customer having typed that name,
    so the existing LLM ordering pipeline resolves it exactly as it
    already resolves typed text. No changes needed anywhere downstream.

    Returns True if the interactive message went out, False if it fell
    back to plain text (the customer still got `body`, either way).
    """
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    await asyncio.sleep(delay)
    try:
        await _send_list_wa_call(phone_number, title, body, button_text, sections, footer, delay)
        await log.info("WA_LIST_SENT", metadata={"phone": phone_number[:6] + "****"})
        return True
    except Exception as e:
        await log.warn("WA_LIST_SEND_FAILED_FALLBACK_TEXT", metadata={"error": str(e)[:200]})
        await _send_text_wa_call(phone_number, body, 0.0)
        return False


@with_retry(max_attempts=2, base_delay=2.0)
async def _send_buttons_wa_call(phone_number: str, title: str, body: str,
                                 buttons: list[dict], footer: Optional[str], delay: float) -> None:
    url = f"{settings.evolution_api_url}/message/sendButtons/{settings.evolution_instance_name}"
    payload = {
        "number": _format_phone(phone_number),
        "title": title,
        "description": body,
        "footer": footer or "",
        "buttons": buttons,
        "delay": int(delay * 1000),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=_evolution_headers())

    if response.status_code not in (200, 201):
        raise RuntimeError(f"Evolution API sendButtons error {response.status_code}: {response.text[:200]}")


async def send_buttons(
    phone_number: str,
    title: str,
    body: str,
    buttons: list[dict],
    footer: Optional[str] = None,
) -> bool:
    """
    buttons: up to 3, e.g. [{"type": "reply", "displayText": "Large", "id": "Large"}, ...]
    (WhatsApp's own hard cap is 3 reply buttons per message — send_list for anything bigger.)

    Same `id` == display text convention and same true/False return
    contract as send_list — see there for why.
    """
    if len(buttons) > 3:
        raise ValueError("WhatsApp reply-button messages support at most 3 buttons — use send_list instead")

    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    await asyncio.sleep(delay)
    try:
        await _send_buttons_wa_call(phone_number, title, body, buttons, footer, delay)
        await log.info("WA_BUTTONS_SENT", metadata={"phone": phone_number[:6] + "****"})
        return True
    except Exception as e:
        await log.warn("WA_BUTTONS_SEND_FAILED_FALLBACK_TEXT", metadata={"error": str(e)[:200]})
        await _send_text_wa_call(phone_number, body, 0.0)
        return False
