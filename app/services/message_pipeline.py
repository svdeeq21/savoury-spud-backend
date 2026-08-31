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

        customer = await orders.get_or_create_customer(org_id, phone, name=push_name)
        should_process = await _claim_message(org_id, wa_message_id, "CUSTOMER", customer["id"], text)
        if not should_process:
            return
        await _handle_customer_message(org_id, business_name, pickup_address, customer, phone, text)


async def _handle_admin_message(org_id: UUID, phone: str, text: str) -> None:
    command = admin_commands.parse_admin_command(text)
    reply = await admin_commands.apply_admin_command(org_id, phone, command)
    await whatsapp.send_message(phone, reply)


async def _handle_customer_message(org_id: UUID, business_name: str, pickup_address: Optional[str], customer: dict, phone: str, text: str) -> None:
    now_local = availability.to_business_time(datetime.now(timezone.utc), settings.business_utc_offset_hours)
    availability_row = await availability_store.get_availability_settings(org_id)
    hours_row = await availability_store.get_operating_hours_for_day(org_id, now_local.weekday())
    is_open, closed_reason = availability.resolve_business_open(availability_row, hours_row, now_local)

    if not is_open:
        await whatsapp.send_message(phone, closed_reason)
        return

    cart = await orders.get_or_create_open_cart(org_id, customer["id"])
    catalog_rows = await catalog.get_full_catalog(org_id)
    cart_detail = await orders.get_cart_detail(cart["id"])

    llm_result = await ordering_llm.interpret_customer_message(business_name, catalog_rows, cart_detail, text, pickup_address)

    checkout_requested = False
    for action in llm_result.get("actions", []):
        if action.get("function") == "checkout":
            checkout_requested = True
            continue
        try:
            await _apply_action(cart["id"], catalog_rows, action)
        except ValueError as e:
            # A business-rule violation (missing required modifier, too many free
            # toppings, no delivery address yet, ...) — tell the customer exactly
            # why and stop this turn here rather than applying the rest of a
            # now-inconsistent set of actions.
            await whatsapp.send_message(phone, str(e))
            return

    if checkout_requested:
        await _start_checkout(org_id, cart["id"], customer, phone)
    else:
        await whatsapp.send_message(phone, llm_result.get("reply", "Got it."))


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


async def _apply_action(cart_id: UUID, catalog_rows: list[dict], action: dict) -> None:
    """
    Raises ValueError for anything the customer needs to hear about (missing
    required modifier, too many free toppings, no delivery address yet — see
    orders._validate_modifier_selection / set_fulfillment_details). The
    caller is responsible for catching that and messaging the customer.
    """
    fn = action.get("function")

    if fn == "add_product":
        product = _find_product_by_name(catalog_rows, action.get("product_name", ""))
        if product is None:
            # The LLM was instructed never to invent items, but this is the deterministic
            # backstop if it does anyway — silently drop rather than write garbage to the cart.
            await log.warn("LLM_REFERENCED_UNKNOWN_PRODUCT", metadata={"name": action.get("product_name")})
            return
        modifiers = _resolve_modifiers_by_name(product, action.get("modifier_names", []) or [])
        quantity = max(1, int(action.get("quantity", 1) or 1))
        await orders.add_product(cart_id, product, quantity, modifiers)  # raises ValueError on bad selection

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


async def _start_checkout(org_id: UUID, cart_id: UUID, customer: dict, phone: str) -> None:
    try:
        order = await orders.start_checkout(cart_id)
    except ValueError as e:
        await whatsapp.send_message(phone, str(e))
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
        await orders.record_pending_payment(order["id"], payment["reference"], amount_to_charge)
    except Exception as e:
        await log.error("CHECKOUT_INIT_FAILED", ref_type="order", ref_id=order["id"], metadata={"error": str(e)[:200]})
        await whatsapp.send_message(phone, "Sorry, I couldn't start checkout right now — please try again in a moment.")
        return

    message = f"Your total is ₦{amount_to_charge:,.2f}. Tap here to pay:\n{payment['authorization_url']}"
    if order.get("fulfillment_method") == "DELIVERY":
        message += "\n\n(This covers the food only — we'll confirm your delivery fee separately once payment goes through.)"
    await whatsapp.send_message(phone, message)


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
    await whatsapp.send_message(customer["phone_number"], message)
