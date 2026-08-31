# savoury-spud-backend/app/core/security.py
#
# Three security concerns:
#   1. Evolution API webhook — inbound WhatsApp events
#   2. Paystack webhook signature — HMAC-SHA512 of the raw body, x-paystack-signature header
#   3. Admin API key — protects /dashboard/* routes

import hmac
import hashlib
from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from app.core.config import get_settings

settings = get_settings()


# ── 1. Evolution API webhook ──────────────────────────────────────
# Same permissive-by-default posture as the real-estate app: return the raw
# body so the caller can parse it, and leave room to enforce a signature
# once Evolution's exact header format for this instance is confirmed.

async def read_whatsapp_webhook_body(request: Request) -> bytes:
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty request body")
    return raw_body


# ── 2. Paystack webhook signature ─────────────────────────────────
# Paystack signs every webhook delivery with HMAC-SHA512 of the *raw*
# request body, using your secret key, sent in the x-paystack-signature
# header. This MUST run against the raw bytes, before any JSON parsing —
# re-serializing the parsed body and hashing that will not match.

async def verify_paystack_signature(request: Request) -> bytes:
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(status_code=400, detail="Empty request body")

    signature = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(
        settings.paystack_secret_key.encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid Paystack signature")

    return raw_body


# ── 3. Admin API key ───────────────────────────────────────────────
# Passed in X-Admin-Key header by the dashboard frontend (server-side only,
# same as the real-estate CRM). Single shared secret, constant-time compare.

_api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_admin_key(api_key: str = Security(_api_key_header)) -> str:
    if not api_key or not settings.admin_api_key or not hmac.compare_digest(api_key, settings.admin_api_key):
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing admin API key",
        )
    return api_key
