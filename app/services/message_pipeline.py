# savoury-spud-backend/app/services/message_pipeline.py
#
# Entry point called by the webhook router for every inbound WhatsApp
# message. Responsible for, in order:
#   1. Idempotency (same wa_message_id never processed twice)
#   2. Per-sender locking (two rapid messages from one person don't race)
#   3. Routing — owner's number(s) go to admin_commands, everyone else to
#      the ordering flow
#   4. For customers: availability gate -> LLM interpretation -> resolving
#      the LLM's product/modifier NAMES against the real catalog ->
#      deterministic cart mutation -> reply
#
# This is where the real-estate backend's admin_whatsapp_number /
# ignored_whatsapp_numbers pattern gets reused, just routed to a handler
# instead of being dropped.

from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.core.config import get_settings
from app.core.supabase import get_supabase
from app.services import (
    orders,
    catalog,
    availability,
    availability_store,
    ordering_llm,
    admin_commands,
    paystack,
    whatsapp,
    notifications,
)
from app.services.orders import normalize_phone
from app.utils.distributed_state import distributed_lock
from app.utils.logger import log

settings = get_settings()


async def _get_org_id() -> UUID:
    """Single-tenant lookup by slug — see migration notes on why org_id exists at all yet."""
    db = await get_supabase()
    result = await db.table("organizations").select("id, name, pickup_address").eq("slug", settings.org_slug).single().execute()
    return result.data


async def _claim_message(org_id: UUID, wa_message_id: Optional[str], sender: str, customer_id: Optional[UUID], content: str) -> bool:
    """
    Returns True if this is the first time this wa_message_id has been seen
    (caller should process it), False if it's a redelivery (caller should
    skip). No wa_message_id at all -> always processed, since there's
    nothing to dedupe against (rare with Evolution API, but the payload
    schema marks the field Optional for a reason).
    """
    if not wa_message_id:
        return True
    db = await get_supabase()
    try:
        await db.table("conversation_messages").insert({
            "org_id": str(org_id),
            "customer_id": str(customer_id) if customer_id else None,
            "sender": sender,
            "content": content,
            "wa_message_id": wa_message_id,
        }).execute()
        return True
    except Exception:
        # Unique constraint violation on (org_id, wa_message_id) — already processed.
        await log.info("DUPLICATE_WA_MESSAGE_IGNORED", ref_type="org", ref_id=org_id, metadata={"wa_message_id": wa_message_id})
        return False


async def _record_bot_message(org_id: UUID, customer_id: UUID, content: str) -> None:
    """
    The bot's half of the conversation was never being saved — only inbound
    customer messages were (via _claim_message). That's the real cause
    behind "it doesn't remember": the LLM was never shown its own previous
    replies, only the current cart state, so it had no way to recall a
    clarifying question it had just asked. No wa_message_id needed here —
    dedup only matters for inbound webhook redelivery, not outbound sends.
    """
    db = await get_supabase()
    await db.table("conversation_messages").insert({
        "org_id": str(org_id),
        "customer_id": str(customer_id),
        "sender": "BOT",
        "content": content,
    }).execute()


async def _get_recent_history(customer_id: UUID, limit: int) -> list[dict]:
    """Last `limit` messages (both sides), returned oldest-first for the prompt."""
    db = await get_supabase()
    result = (
        await db.table("conversation_messages")
        .select("sender, content")
        .eq("customer_id", str(customer_id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(result.data or []))


def _is_duplicate_bot_message(recent_history: list[dict], candidate_text: str) -> bool:
    """True if the most recent message was this exact bot message already — used to stop
    the closed-hours notice (or anything else) from being repeated verbatim every single turn."""
    return bool(recent_history) and recent_history[-1].get("sender") == "BOT" and recent_history[-1].get("content") == candidate_text


def _is_admin_number(phone: str) -> bool:
    normalized = normalize_phone(phone)
    return any(normalize_phone(n) == normalized for n in settings.admin_number_list)


async def handle_incoming_whatsapp_message(phone: str, text: str, wa_message_id: Optional[str], push_name: Optional[str] = None) -> None:
    org = await _get_org_id()
    org_id, business_name, pickup_address = org["id"], org["name"], org.get("pickup_address")

    async with distributed_lock(f"wa:{normalize_phone(phone)}"):
        if _is_admin_number(phone):
            should_process = await _claim_message(org_id, wa_message_id, "ADMIN", None, text)
            if not should_process:
                return
            await _handle_admin_message(org_id, phone, text)
            return

        customer, is_new = await orders.get_or_create_customer(org_id, phone, name=push_name)
        should_process = await _claim_message(org_id, wa_message_id, "CUSTOMER", customer["id"], text)
        if not should_process:
            return
        await _handle_customer_message(org_id, business_name, pickup_address, customer, phone, text, is_new)


async def _send_and_record(org_id: UUID, customer_id: UUID, phone: str, text: str) -> None:
    """Every customer-facing send goes through here so conversation_messages actually has both sides of the chat."""
    await whatsapp.send_message(phone, text)
    await _record_bot_message(org_id, customer_id, text)


_WELCOME_MESSAGE_TEMPLATE = (
    "👋 Welcome to {business_name}! I'm here to help you build your box — pick a size, base, protein, "
    "toppings, sauce, and any extras you'd like, plus a drink if you're after one. Just tell me what you "
    "want, or ask to see the menu, and I'll take it from there."
)


async def _handle_admin_message(org_id: UUID, phone: str, text: str) -> None:
    command = admin_commands.parse_admin_command(text)
    reply = await admin_commands.apply_admin_command(org_id, phone, command)
    await whatsapp.send_message(phone, reply)


_PENDING_PAYMENT_CANCEL_PATTERN = re.compile(r"\b(cancel|start over|never\s*mind|nevermind|forget it|undo)\b", re.IGNORECASE)


async def _handle_pending_payment_message(org_id: UUID, customer_id: UUID, phone: str, pending_order: dict, text: str) -> None:
    """
    Deterministic, LLM-free handling for anything sent while a checkout is
    already in flight. This is the fix for the most dangerous bug found in
    a real transcript: once an order left CART status, the conversation had
    zero awareness a payment was pending, and the LLM — seeing a blank new
    cart — freely improvised a close ("Enjoy your order!") with no actual
    payment having happened. Nothing here is left to the LLM's judgment on
    purpose; money is deterministic territory.
    """
    if _PENDING_PAYMENT_CANCEL_PATTERN.search(text):
        await orders.cancel_pending_order(pending_order["id"])
        await _send_and_record(
            org_id, customer_id, phone,
            "No problem — I've cancelled that checkout. Message me anytime you're ready to start a new order.",
        )
        return

    message = f"You've got a payment in progress for ₦{pending_order['subtotal']:,.2f}."
    payment_row_url = pending_order.get("_authorization_url")  # attached by the caller, see below
    if payment_row_url:
        message += f" Here's the link again:\n{payment_row_url}"
    message += "\n\nI'll confirm automatically the moment it goes through — no need to tell me. Say \"cancel\" if you'd rather start over instead."
    await _send_and_record(org_id, customer_id, phone, message)


async def _handle_customer_message(
    org_id: UUID,
    business_name: str,
    pickup_address: Optional[str],
    customer: dict,
    phone: str,
    text: str,
    is_new: bool,
) -> None:
    customer_id = customer["id"]

    if is_new:
        await _send_and_record(org_id, customer_id, phone, _WELCOME_MESSAGE_TEMPLATE.format(business_name=business_name))

    # Hard gate, checked before anything else: a checkout already in flight is never
    # allowed to fall through to the ordering LLM with a blank cart.
    pending_order = await orders.get_pending_payment_order(org_id, customer_id)
    if pending_order:
        pending_order["_authorization_url"] = await orders.get_pending_payment_link(pending_order["id"])
        await _handle_pending_payment_message(org_id, customer_id, phone, pending_order, text)
        return

    now_local = availability.to_business_time(datetime.now(timezone.utc), settings.business_utc_offset_hours)
    availability_row = await availability_store.get_availability_settings(org_id)
    hours_row = await availability_store.get_operating_hours_for_day(org_id, now_local.weekday())
    is_open, closed_reason = availability.resolve_business_open(availability_row, hours_row, now_local)

    if not is_open:
        recent = await _get_recent_history(customer_id, 1)
        if not _is_duplicate_bot_message(recent, closed_reason):
            await _send_and_record(org_id, customer_id, phone, closed_reason)
        return

    cart = await orders.get_or_create_open_cart(org_id, customer_id, stale_after_hours=settings.cart_stale_after_hours)
    catalog_rows = await catalog.get_full_catalog(org_id)
    cart_detail = await orders.get_cart_detail(cart["id"])
    recent_history = await _get_recent_history(customer_id, settings.conversation_history_turns)

    llm_result = await ordering_llm.interpret_customer_message(
        business_name, catalog_rows, cart_detail, text, pickup_address, recent_history
    )

    checkout_requested = False
    override_message = None
    for action in llm_result.get("actions", []):
        if action.get("function") == "checkout":
            checkout_requested = True
            continue
        try:
            result = await _apply_action(cart["id"], catalog_rows, action)
            if result:
                override_message = result
        except ValueError as e:
            # A hard business-rule violation (no delivery address yet, invalid quantity, ...)
            # — tell the customer exactly why and stop this turn here rather than applying
            # the rest of a now-inconsistent set of actions.
            await _send_and_record(org_id, customer_id, phone, str(e))
            return

    if checkout_requested:
        await _start_checkout(org_id, cart["id"], customer, phone)
    elif override_message:
        # An add_product attempt was incomplete — the authoritative "here's what's
        # still missing" message takes priority over whatever the LLM's own reply said,
        # since the LLM's reply was written without knowing the commit would fail.
        await _send_and_record(org_id, customer_id, phone, override_message)
    else:
        await _send_and_record(org_id, customer_id, phone, llm_result.get("reply", "Got it."))


def _find_product_by_name(catalog_rows: list[dict], name: str) -> Optional[dict]:
    if not name:
        return None
    lowered = name.strip().lower()
    for p in catalog_rows:
        if p["name"].strip().lower() == lowered:
            return p
    for p in catalog_rows:  # fall back to partial match
        if lowered in p["name"].strip().lower():
            return p
    return None


def _resolve_modifiers_by_name(product: dict, modifier_names: list[str]) -> list[dict]:
    resolved = []
    all_modifiers = [m for group in product.get("modifier_groups", []) for m in group.get("modifiers", [])]
    for name in modifier_names:
        lowered = name.strip().lower()
        match = next((m for m in all_modifiers if m["name"].strip().lower() == lowered), None) or \
                next((m for m in all_modifiers if lowered in m["name"].strip().lower()), None)
        if match:
            resolved.append(match)
    return resolved


def _safe_uuid(value) -> Optional[UUID]:
    """Malformed UUID from the LLM is an internal formatting slip, not a business-rule
    violation — logged and skipped rather than surfaced to the customer as an error."""
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _format_incomplete_draft_message(product_name: str, result: dict) -> str:
    """
    Turns update_draft_item's structured result into the customer-facing
    message — always lists every still-missing group AND its actual
    options in one message, generated from the real catalog, never left to
    the LLM to remember or improvise (that's what was breaking before).

    Two distinct framings: starting a product from scratch reads like a
    menu walkthrough (this IS the onboarding moment — first thing shown
    once someone says "I want a box"), while an in-progress item reads as
    a status update on top of what's already chosen. Same underlying data,
    worded for where the customer actually is.
    """
    selected = result.get("selected_so_far", [])

    if "error" in result:
        selected_names = ", ".join(m["name"] for m in selected) if selected else "nothing yet"
        return f"Got it — {selected_names} so far for your {product_name}. {result['error']}"

    parts = []
    for group in result.get("missing", []):
        options = ", ".join(m["name"] for m in group.get("modifiers", []))
        parts.append(f"{group['name']} — choose at least 1: {options}")

    if not selected:
        return f"Let's build your {product_name}! Here's what you'll need to choose:\n" + "\n".join(f"- {p}" for p in parts)

    selected_names = ", ".join(m["name"] for m in selected)
    return f"Got it — {selected_names} so far for your {product_name}. Still need:\n" + "\n".join(f"- {p}" for p in parts)


async def _apply_action(cart_id: UUID, catalog_rows: list[dict], action: dict) -> Optional[str]:
    """
    Raises ValueError for hard failures the customer needs to hear about
    (no delivery address yet, invalid quantity, ...). Returns a non-None
    string when the action produced an authoritative message that should
    REPLACE the LLM's own reply for this turn — currently only used for an
    incomplete add_product, where accuracy matters more than letting the
    LLM improvise. Returns None otherwise, meaning "the LLM's reply stands
    as-is".
    """
    fn = action.get("function")

    if fn == "add_product":
        product = _find_product_by_name(catalog_rows, action.get("product_name", ""))
        if product is None:
            # Confirmed root cause of a real bug: this used to silently no-op here,
            # meaning the LLM's own optimistic reply ("Adding that to your cart now!")
            # would reach the customer even though NOTHING was actually added — exactly
            # the "bot claims success the backend never confirmed" failure mode. Now it
            # overrides with an honest message instead of staying silent.
            await log.warn("LLM_REFERENCED_UNKNOWN_PRODUCT", metadata={"name": action.get("product_name")})
            return f"Sorry, I couldn't match \"{action.get('product_name') or 'that'}\" to anything on our menu — could you tell me again what you'd like?"
        modifiers = _resolve_modifiers_by_name(product, action.get("modifier_names", []) or [])
        quantity = max(1, int(action.get("quantity", 1) or 1))
        result = await orders.update_draft_item(cart_id, product, quantity, modifiers)
        if result["committed"]:
            return None  # fully specified and added — let the LLM's own "added to cart" reply stand
        return _format_incomplete_draft_message(product["name"], result)

    elif fn == "remove_item":
        item_id = _safe_uuid(action.get("order_item_id"))
        if item_id:
            await orders.remove_item(cart_id, item_id)

    elif fn == "remove_modifier":
        item_id = _safe_uuid(action.get("order_item_id"))
        modifier_name = action.get("modifier_name")
        if item_id and modifier_name:
            # Resolving modifier_id by name here would need the specific order_item's product —
            # simplification for v0: look it up against the full catalog's modifiers by name.
            all_modifiers = [m for p in catalog_rows for g in p.get("modifier_groups", []) for m in g.get("modifiers", [])]
            match = next((m for m in all_modifiers if m["name"].strip().lower() == modifier_name.strip().lower()), None)
            if match:
                await orders.remove_modifier(item_id, match["id"], cart_id)

    elif fn == "set_quantity":
        item_id = _safe_uuid(action.get("order_item_id"))
        quantity = action.get("quantity")
        if item_id and quantity:
            await orders.set_quantity(item_id, cart_id, int(quantity))  # raises ValueError if quantity < 1

    elif fn == "set_fulfillment":
        method = (action.get("method") or "").upper()
        if method in ("PICKUP", "DELIVERY"):
            await orders.set_fulfillment_details(
                cart_id,
                method=method,
                delivery_address=action.get("delivery_address"),
                delivery_area=action.get("delivery_area"),
                delivery_landmark=action.get("delivery_landmark"),
            )  # raises ValueError if DELIVERY without an address

    return None


async def _start_checkout(org_id: UUID, cart_id: UUID, customer: dict, phone: str) -> None:
    customer_id = customer["id"]
    try:
        order = await orders.start_checkout(cart_id)
    except ValueError as e:
        await _send_and_record(org_id, customer_id, phone, str(e))
        return

    # Payment is always for the food subtotal only — delivery fee (if any) is confirmed
    # by the vendor directly with the customer after payment, never part of this charge.
    amount_to_charge = order["subtotal"]

    try:
        payment = await paystack.initialize_transaction(
            order_id=order["id"],
            amount_naira=amount_to_charge,
            customer_email="",
            customer_phone=phone,
        )
        await orders.record_pending_payment(order["id"], payment["reference"], amount_to_charge, payment["authorization_url"])
    except Exception as e:
        await log.error("CHECKOUT_INIT_FAILED", ref_type="order", ref_id=order["id"], metadata={"error": str(e)[:200]})
        await _send_and_record(org_id, customer_id, phone, "Sorry, I couldn't start checkout right now — please try again in a moment.")
        return

    message = f"Your total is ₦{amount_to_charge:,.2f}. Tap here to pay:\n{payment['authorization_url']}"
    if order.get("fulfillment_method") == "DELIVERY":
        message += "\n\n(This covers the food only — we'll confirm your delivery fee separately once payment goes through.)"
    await _send_and_record(org_id, customer_id, phone, message)


async def handle_confirmed_payment(order_id: UUID, reference: str) -> None:
    """Called by the Paystack webhook handler once signature + verify-transaction have both passed."""
    order = await orders.mark_paid(order_id, reference)
    if order is None:
        return
    await orders.mark_payment_verified(reference, "success")

    org = await _get_org_id()
    customer = (await (await get_supabase()).table("customers").select("*").eq("id", order["customer_id"]).single().execute()).data
    order_detail = await orders.get_cart_detail(order_id)

    await notifications.notify_new_paid_order(order_detail, customer)

    message = f"Payment received — thank you! Your order (₦{order['subtotal']:,.2f}) is confirmed."
    if order.get("fulfillment_method") == "DELIVERY":
        message += f" {org['name']} will reach out shortly to confirm your delivery fee and arrange delivery."
    elif order.get("fulfillment_method") == "PICKUP":
        message += f" You can pick it up at {org.get('pickup_address') or 'our pickup location'} — we'll let you know when it's ready."
    await _send_and_record(order["org_id"], order["customer_id"], customer["phone_number"], message)
