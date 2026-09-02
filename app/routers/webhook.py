# savoury-spud-backend/app/routers/webhook.py
#
# Two endpoints:
#   POST /webhook/whatsapp — Evolution API, every inbound WA event
#   POST /webhook/paystack — charge.success and friends
#
# Both follow the same shape: verify fast, return 200 immediately, do the
# real work as a background task. Paystack retries for up to 72 hours if it
# doesn't get a 200 quickly — the worst possible failure mode here is us
# being slow, not us being wrong, so nothing expensive happens before the
# response is sent.

import json
import logging
from uuid import UUID
from fastapi import APIRouter, Request, BackgroundTasks, Depends, HTTPException

from app.core.security import read_whatsapp_webhook_body, verify_paystack_signature
from app.models.schemas import WAWebhookPayload
from app.services.message_pipeline import handle_incoming_whatsapp_message, handle_confirmed_payment
from app.services import orders, paystack as paystack_service
from app.utils.logger import log

router = APIRouter(prefix="/webhook", tags=["webhook"])
_l = logging.getLogger("savoury-spud")

HANDLED_EVENTS = {"messages.upsert", "MESSAGES_UPSERT", "messages_upsert"}


def _extract_phone_and_text(payload: WAWebhookPayload) -> tuple[str, str, bool]:
    """Returns (phone_number, message_text, is_group_or_unusable)."""
    wa_data = payload.data
    remote_jid = (wa_data.key or {}).get("remoteJid", "")

    if "@g.us" in remote_jid:
        return "", "", True  # group chat — not something this bot handles

    phone_number = remote_jid.replace("@lid", "").replace("@s.whatsapp.net", "").lstrip("+")
    message_text = _extract_message_text(wa_data.message) if wa_data.message else ""
    return phone_number, message_text, (not phone_number or not message_text)


def _extract_message_text(message: dict) -> str:
    """
    Plain text first, then interactive reply types. A button/list tap is
    deliberately reduced to its display text and fed through the exact
    same pipeline as if the customer had typed it — this is the safe first
    integration step: interactive messages become a nicer input method,
    not a second code path that has to be kept correct in parallel with
    the LLM-driven text flow that's already tested end to end. A row-ID-
    based fast path (skipping the LLM entirely for a tapped selection) is
    a worthwhile later optimization, but only once buttons/lists are
    confirmed to actually render reliably on this instance.
    """
    if "conversation" in message:
        return message["conversation"]
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text", "")
    if "buttonsResponseMessage" in message:
        r = message["buttonsResponseMessage"]
        return r.get("selectedDisplayText") or r.get("selectedButtonId", "")
    if "listResponseMessage" in message:
        r = message["listResponseMessage"]
        title = r.get("title") or (r.get("singleSelectReply") or {}).get("selectedRowId", "")
        return title
    if "templateButtonReplyMessage" in message:
        r = message["templateButtonReplyMessage"]
        return r.get("selectedDisplayText") or r.get("selectedId", "")
    return ""


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, background: BackgroundTasks, raw_body: bytes = Depends(read_whatsapp_webhook_body)):
    try:
        data = json.loads(raw_body)
        payload = WAWebhookPayload(**data)
    except Exception as e:
        await log.warn("WEBHOOK_PARSE_ERROR", metadata={"error": str(e)})
        return {"status": "parse_error"}  # 200 anyway — don't make Evolution retry a payload that will never parse

    if payload.event not in HANDLED_EVENTS:
        return {"status": "ignored", "event": payload.event}

    if not payload.data.key or not payload.data.messageType:
        return {"status": "ignored", "reason": "missing_key_or_type"}

    if payload.data.key.get("fromMe"):
        return {"status": "ignored", "reason": "own_message"}  # don't let the bot reply to itself

    phone, text, unusable = _extract_phone_and_text(payload)
    if unusable:
        return {"status": "ignored", "reason": "no_usable_text_or_group_chat"}

    wa_message_id = payload.data.key.get("id")
    background.add_task(handle_incoming_whatsapp_message, phone, text, wa_message_id, payload.data.pushName)

    return {"status": "queued"}


@router.post("/paystack")
async def paystack_webhook(request: Request, background: BackgroundTasks, raw_body: bytes = Depends(verify_paystack_signature)):
    try:
        event = json.loads(raw_body)
    except Exception as e:
        await log.warn("PAYSTACK_WEBHOOK_PARSE_ERROR", metadata={"error": str(e)})
        return {"status": "parse_error"}

    event_type = event.get("event")
    if event_type != "charge.success":
        # We only act on successes — failed/abandoned attempts just leave the order
        # in PAYMENT_PENDING, which expire_stale_pending_orders() cleans up later.
        return {"status": "ignored", "event": event_type}

    data = event.get("data", {})
    reference = data.get("reference")
    if not reference:
        await log.warn("PAYSTACK_WEBHOOK_MISSING_REFERENCE")
        return {"status": "ignored", "reason": "missing_reference"}

    background.add_task(_process_paystack_success, reference, data)
    return {"status": "queued"}


async def _process_paystack_success(reference: str, data: dict) -> None:
    """
    Runs as a background task, so raising here just gets logged — the
    caller already returned 200. Two independent safeguards against acting
    on a bad or duplicate event:
      1. Re-verify with GET /transaction/verify/:reference rather than
         trusting the webhook body alone (webhook signature proves it came
         from Paystack, not that the amount/status weren't tampered with
         upstream of signing, and it's cheap insurance either way).
      2. orders.mark_paid() is itself idempotent — a redelivered webhook
         for an already-PAID order is a no-op there too.
    """
    try:
        verified = await paystack_service.verify_transaction(reference)
    except Exception as e:
        await log.error("PAYSTACK_VERIFY_FAILED", metadata={"reference": reference, "error": str(e)[:200]})
        return

    if verified.get("status") != "success":
        await log.warn("PAYSTACK_VERIFY_NOT_SUCCESS", metadata={"reference": reference, "status": verified.get("status")})
        return

    order = await orders.get_order_by_payment_reference(reference)
    if order is None:
        await log.error("PAYSTACK_WEBHOOK_UNKNOWN_REFERENCE", metadata={"reference": reference})
        return

    # Compare against subtotal, not total — delivery fee (if any) is never part of the
    # Paystack charge, so total (subtotal + delivery_fee) would be the wrong figure to
    # verify against and could reject a perfectly legitimate payment.
    if not paystack_service.amount_matches(order["subtotal"], verified.get("amount", 0)):
        await log.error("PAYSTACK_AMOUNT_MISMATCH", ref_type="order", ref_id=order["id"], metadata={
            "expected_naira": order["subtotal"], "paystack_kobo": verified.get("amount"),
        })
        return

    await handle_confirmed_payment(UUID(order["id"]), reference)
