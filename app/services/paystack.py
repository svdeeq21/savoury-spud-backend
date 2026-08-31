# savoury-spud-backend/app/services/paystack.py
#
# Paystack integration. Two ways a payment gets confirmed, both required:
#   1. Webhook (charge.success) — fast, primary path
#   2. Verify Transaction API (GET /transaction/verify/:reference) — the
#      fallback for "customer pays, webhook is late/lost", and the sanity
#      check the webhook handler itself runs before trusting anything.
#
# Per Paystack's docs: verify the signature, THEN call verify — never
# grant value off the webhook body alone, and never off signature
# verification alone either. Both.

from __future__ import annotations
import httpx
from typing import Optional
from uuid import UUID

from app.core.config import get_settings
from app.services import pricing_engine
from app.utils.retry import with_retry
from app.utils.logger import log

settings = get_settings()
_BASE_URL = "https://api.paystack.co"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.paystack_secret_key}",
        "Content-Type": "application/json",
    }


@with_retry(max_attempts=3, base_delay=1.5)
async def initialize_transaction(
    order_id: UUID,
    amount_naira,
    customer_email: str,
    customer_phone: str,
) -> dict:
    """
    POST /transaction/initialize. Amount must be in kobo (smallest subunit),
    hence the one and only to_kobo() call in this whole flow. Returns
    {authorization_url, access_code, reference} — authorization_url is what
    gets sent to the customer as their payment link.

    Paystack requires an email even for a WhatsApp-native flow with no
    customer account — if the customer hasn't given one, pass a
    deterministic placeholder built from their phone number rather than a
    fake-looking address, so support/reconciliation can still trace it back.
    """
    reference = f"spud_{order_id}"
    payload = {
        "email":    customer_email or f"{customer_phone}@whatsapp.savouryspud.local",
        "amount":   pricing_engine.to_kobo(amount_naira),
        "currency": settings.currency,
        "reference": reference,
        "metadata": {"order_id": str(order_id), "phone": customer_phone},
    }
    if settings.paystack_callback_url:
        payload["callback_url"] = settings.paystack_callback_url

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(f"{_BASE_URL}/transaction/initialize", json=payload, headers=_headers())

    if response.status_code != 200:
        raise RuntimeError(f"Paystack initialize failed ({response.status_code}): {response.text[:300]}")

    data = response.json()["data"]
    await log.info("PAYSTACK_TRANSACTION_INITIALIZED", ref_type="order", ref_id=order_id, metadata={"reference": reference})
    return {
        "authorization_url": data["authorization_url"],
        "access_code":       data["access_code"],
        "reference":          data["reference"],
    }


@with_retry(max_attempts=3, base_delay=1.5)
async def verify_transaction(reference: str) -> dict:
    """
    GET /transaction/verify/:reference. Returns Paystack's transaction
    object. Caller must check data['status'] == 'success' AND that
    data['amount'] (kobo) matches what the order expected — verifying only
    the status without checking the amount is the classic underpayment bug.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{_BASE_URL}/transaction/verify/{reference}", headers=_headers())

    if response.status_code != 200:
        raise RuntimeError(f"Paystack verify failed ({response.status_code}): {response.text[:300]}")

    return response.json()["data"]


def amount_matches(expected_naira, paystack_kobo: int) -> bool:
    return pricing_engine.to_kobo(expected_naira) == paystack_kobo
