# savoury-spud-backend/app/services/orders.py
#
# Owns the cart -> order lifecycle. A "cart" is just an order row with
# status = 'CART' — see migration notes. These are the functions the
# ordering flow (WhatsApp AI layer) and the admin dashboard both call into;
# neither of them is allowed to touch order_items/orders directly.
#
# Function names deliberately match the brief:
#   add_product, add_modifier, remove_modifier, set_quantity,
#   calculate_cart, create_order, create_payment (in paystack.py)

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from app.core.supabase import get_supabase
from app.services import pricing_engine
from app.utils.logger import log


# ── Customers ───────────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    """Digits only, no leading +/spaces/dashes — the canonical form stored in DB."""
    return "".join(ch for ch in raw if ch.isdigit())


async def get_or_create_customer(org_id: UUID, phone_number: str, name: Optional[str] = None) -> tuple[dict, bool]:
    """Returns (customer, is_new) — is_new is True only on the very first message this phone number has ever sent."""
    db = await get_supabase()
    phone = normalize_phone(phone_number)

    existing = (
        await db.table("customers")
        .select("*")
        .eq("org_id", str(org_id))
        .eq("phone_number", phone)
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        if name and not row.get("name"):
            await db.table("customers").update({"name": name}).eq("id", row["id"]).execute()
            row["name"] = name
        return row, False

    inserted = (
        await db.table("customers")
        .insert({"org_id": str(org_id), "phone_number": phone, "name": name})
        .execute()
    )
    return inserted.data[0], True


# ── Cart ────────────────────────────────────────────────────────

def _is_stale(timestamp_str: Optional[str], stale_after_hours: float) -> bool:
    if not timestamp_str:
        return False
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) > timedelta(hours=stale_after_hours)


async def get_or_create_open_cart(org_id: UUID, customer_id: UUID, stale_after_hours: float = 4.0) -> dict:
    """
    Every customer has at most one CART-status order at a time. Returns it,
    creating one if none exists — UNLESS the existing one hasn't been
    touched in `stale_after_hours`, in which case it's marked EXPIRED and a
    fresh cart is started instead.

    This is deliberately not a short session timeout (a hard "forget after
    30 minutes" would punish someone who's just slow mid-order, or steps
    away and comes back) — it's specifically aimed at the case where
    someone abandoned an order hours or days ago and comes back later to
    order something new, and shouldn't have stale, possibly outdated items
    silently resumed into their new order.
    """
    db = await get_supabase()

    existing = (
        await db.table("orders")
        .select("*")
        .eq("customer_id", str(customer_id))
        .eq("status", "CART")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if existing.data:
        cart = existing.data[0]
        last_touched = cart.get("updated_at") or cart.get("created_at")
        if not _is_stale(last_touched, stale_after_hours):
            return cart
        await db.table("orders").update({"status": "EXPIRED"}).eq("id", cart["id"]).execute()
        await log.info("STALE_CART_EXPIRED", ref_type="order", ref_id=cart["id"], metadata={"last_touched": last_touched})

    inserted = (
        await db.table("orders")
        .insert({"org_id": str(org_id), "customer_id": str(customer_id), "status": "CART"})
        .execute()
    )
    return inserted.data[0]


def _merge_modifier_selection(product: dict, existing_ids: set, new_modifiers: list[dict]) -> set:
    """
    Merges a newly-given batch of modifiers into whatever was already
    chosen. The rule that actually matches how people order piecemeal:
    single-select groups (Size, Base, Protein) — a new answer REPLACES the
    old one, since re-specifying it means changing your mind. Multi-select
    groups (Toppings, Sauces, Extras) — a new answer ADDS to what's already
    there, since "Cheese Sauce and Mexican Salsa" said two messages later
    is completing the topping choice, not replacing an unrelated one.
    """
    result = set(existing_ids)
    groups_by_modifier_id = {
        m["id"]: group
        for group in product.get("modifier_groups", [])
        for m in group.get("modifiers", [])
    }

    replaced_groups = set()
    for nm in new_modifiers:
        group = groups_by_modifier_id.get(nm["id"])
        if group is None:
            result.add(nm["id"])
            continue
        if group["selection_type"] == "single" and group["id"] not in replaced_groups:
            group_modifier_ids = {m["id"] for m in group.get("modifiers", [])}
            result -= group_modifier_ids
            replaced_groups.add(group["id"])
        result.add(nm["id"])
    return result


def _missing_required_groups(product: dict, selected_ids: set) -> list[dict]:
    """Every required group with zero selections so far — used to tell the customer
    exactly what's still needed, in one message, instead of one field at a time."""
    missing = []
    for group in product.get("modifier_groups", []):
        group_modifier_ids = {m["id"] for m in group.get("modifiers", [])}
        if group.get("required") and not (selected_ids & group_modifier_ids):
            missing.append(group)
    return missing


def _validate_modifier_selection(product: dict, modifiers: list[dict]) -> None:
    """
    Enforces each modifier group's required/max_selections against what was
    actually selected. Raises ValueError with a customer-facing message on
    violation — the caller (message_pipeline) is expected to send that
    message back rather than silently dropping or guessing at a fix.

    Only runs if the product carries modifier_groups (i.e. was fetched via
    catalog.get_full_catalog, which nests them in) — a bare product dict
    from catalog.get_product() has none, so this is a no-op for callers
    that don't have group structure to check against.
    """
    groups = product.get("modifier_groups") or []
    if not groups:
        return

    selected_ids = [m["id"] for m in modifiers]

    for group in groups:
        group_modifier_ids = {m["id"] for m in group.get("modifiers", [])}
        count_in_group = sum(1 for mid in selected_ids if mid in group_modifier_ids)

        if group.get("required") and count_in_group == 0:
            raise ValueError(f"\"{group['name']}\" is required — please choose at least one option.")

        max_sel = group.get("max_selections")
        if max_sel is not None and count_in_group > max_sel:
            raise ValueError(
                f"\"{group['name']}\" includes up to {max_sel} free, and you picked {count_in_group}. "
                f"Extra {group['name'].lower()} can be added from Extras for a small fee — want me to add it that way instead?"
            )


async def update_draft_item(order_id: UUID, product: dict, quantity: int, new_modifiers: list[dict]) -> dict:
    """
    The fix for losing partial answers: merges new_modifiers into whatever
    draft already exists for this cart, persists it immediately (survives
    even if this exact call ends up incomplete), and only creates a real
    order_item once every required group is satisfied.

    Returns one of:
      {"committed": True, "item": {...}}
      {"committed": False, "missing": [group, ...], "selected_so_far": [modifier, ...]}
      {"committed": False, "error": "...", "selected_so_far": [modifier, ...]}   (e.g. too many toppings)

    A different product starting mid-conversation resets the draft rather
    than mixing modifiers from two different items together — only one
    item is "in progress" at a time, matching how the flow actually works.
    """
    db = await get_supabase()
    order = (await db.table("orders").select("draft_item").eq("id", str(order_id)).single().execute()).data
    existing_draft = (order or {}).get("draft_item") or {}

    existing_ids = set(existing_draft.get("modifier_ids", [])) if existing_draft.get("product_id") == product["id"] else set()
    merged_ids = _merge_modifier_selection(product, existing_ids, new_modifiers)

    all_modifiers_by_id = {m["id"]: m for g in product.get("modifier_groups", []) for m in g.get("modifiers", [])}
    merged_modifiers = [all_modifiers_by_id[mid] for mid in merged_ids if mid in all_modifiers_by_id]

    async def _persist_draft():
        await db.table("orders").update({"draft_item": {
            "product_id": product["id"],
            "product_name": product["name"],
            "quantity": quantity,
            "modifier_ids": list(merged_ids),
        }}).eq("id", str(order_id)).execute()

    missing = _missing_required_groups(product, merged_ids)
    if missing:
        await _persist_draft()
        return {"committed": False, "missing": missing, "selected_so_far": merged_modifiers}

    try:
        _validate_modifier_selection(product, merged_modifiers)
    except ValueError as e:
        await _persist_draft()
        return {"committed": False, "error": str(e), "selected_so_far": merged_modifiers}

    item = await add_product(order_id, product, quantity, merged_modifiers)
    await db.table("orders").update({"draft_item": None}).eq("id", str(order_id)).execute()
    return {"committed": True, "item": item}


async def add_product(
    order_id: UUID,
    product: dict,
    quantity: int,
    modifiers: list[dict],
) -> dict:
    """
    product and modifiers are already-fetched, already-availability-checked
    rows (the caller — the ordering flow — is responsible for calling
    availability.py first; this function trusts what it's given and just
    persists it, snapshotting name/price at time of order).
    """
    _validate_modifier_selection(product, modifiers)

    db = await get_supabase()

    modifier_prices = [m["price"] for m in modifiers]
    line_total = pricing_engine.line_item_total(product["base_price"], modifier_prices, quantity)

    item_result = (
        await db.table("order_items")
        .insert({
            "order_id":     str(order_id),
            "product_id":   product["id"],
            "product_name": product["name"],
            "base_price":   float(product["base_price"]),
            "quantity":     quantity,
            "line_total":   float(line_total),
        })
        .execute()
    )
    item = item_result.data[0]

    for m in modifiers:
        await db.table("order_item_modifiers").insert({
            "order_item_id": item["id"],
            "modifier_id":   m["id"],
            "modifier_name": m["name"],
            "price":         float(m["price"]),
        }).execute()

    await recalculate_cart(order_id)
    await log.info("CART_ITEM_ADDED", ref_type="order", ref_id=order_id, metadata={"product": product["name"], "qty": quantity})
    return item


async def remove_item(order_id: UUID, order_item_id: UUID) -> None:
    db = await get_supabase()
    await db.table("order_item_modifiers").delete().eq("order_item_id", str(order_item_id)).execute()
    await db.table("order_items").delete().eq("id", str(order_item_id)).eq("order_id", str(order_id)).execute()
    await recalculate_cart(order_id)
    await log.info("CART_ITEM_REMOVED", ref_type="order", ref_id=order_id, metadata={"order_item_id": str(order_item_id)})


async def remove_modifier(order_item_id: UUID, modifier_id: UUID, order_id: UUID) -> None:
    """'Actually remove the cheese' — drop one modifier from an existing line item and re-price it."""
    db = await get_supabase()
    await db.table("order_item_modifiers").delete().eq("order_item_id", str(order_item_id)).eq("modifier_id", str(modifier_id)).execute()

    item = (await db.table("order_items").select("*").eq("id", str(order_item_id)).single().execute()).data
    remaining_mods = (
        await db.table("order_item_modifiers").select("price").eq("order_item_id", str(order_item_id)).execute()
    ).data or []

    new_line_total = pricing_engine.line_item_total(
        item["base_price"], [m["price"] for m in remaining_mods], item["quantity"]
    )
    await db.table("order_items").update({"line_total": float(new_line_total)}).eq("id", str(order_item_id)).execute()
    await recalculate_cart(order_id)


async def set_quantity(order_item_id: UUID, order_id: UUID, quantity: int) -> None:
    if quantity < 1:
        raise ValueError("quantity must be at least 1 — use remove_item to take something out entirely")

    db = await get_supabase()
    item = (await db.table("order_items").select("*").eq("id", str(order_item_id)).single().execute()).data
    mods = (await db.table("order_item_modifiers").select("price").eq("order_item_id", str(order_item_id)).execute()).data or []

    new_line_total = pricing_engine.line_item_total(item["base_price"], [m["price"] for m in mods], quantity)
    await db.table("order_items").update({
        "quantity": quantity,
        "line_total": float(new_line_total),
    }).eq("id", str(order_item_id)).execute()
    await recalculate_cart(order_id)


async def recalculate_cart(order_id: UUID) -> dict:
    """
    Re-reads every line item's line_total and rewrites orders.subtotal/total.
    Called after every cart mutation — nothing computes a running total any
    other way, which is what makes calculate_cart() safe to call as often as
    the conversation needs to quote a price.
    """
    db = await get_supabase()
    items = (await db.table("order_items").select("line_total").eq("order_id", str(order_id)).execute()).data or []
    subtotal = pricing_engine.cart_subtotal([i["line_total"] for i in items])

    order = (await db.table("orders").select("delivery_fee").eq("id", str(order_id)).single().execute()).data
    delivery_fee = (order or {}).get("delivery_fee") or 0
    total = pricing_engine.cart_total(subtotal, delivery_fee)

    updated = (
        await db.table("orders")
        .update({"subtotal": float(subtotal), "total": float(total), "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", str(order_id))
        .execute()
    )
    return updated.data[0]


async def set_delivery_fee(order_id: UUID, delivery_fee) -> dict:
    """
    Records the fee she quoted the customer directly — normally called
    AFTER the order is already PAID, once she's confirmed the address and
    told them the cost. This never triggers a second charge: the fee is
    collected however she and the customer arrange it (transfer, cash on
    delivery, etc), outside Paystack. `total` here is informational — the
    actual amount verified via Paystack was always `subtotal` alone (see
    start_checkout / message_pipeline._start_checkout).
    """
    db = await get_supabase()
    await db.table("orders").update({
        "delivery_fee": float(delivery_fee),
        "delivery_fee_confirmed": True,
    }).eq("id", str(order_id)).execute()
    return await recalculate_cart(order_id)


async def set_fulfillment_details(
    order_id: UUID,
    method: str,
    delivery_address: Optional[str] = None,
    delivery_area: Optional[str] = None,
    delivery_landmark: Optional[str] = None,
    time_preference: str = "ASAP",
    scheduled_for: Optional[datetime] = None,
) -> dict:
    """
    Records pickup vs delivery + whatever details go with it. Called by the
    ordering flow once the customer has answered the "pickup or delivery?"
    question. Payment (start_checkout) never waits on this — it's the food
    subtotal only, always. delivery_fee_confirmed here is purely
    informational: it tells the dashboard which delivery orders she still
    needs to follow up with a fee on, not something checkout checks.
    """
    if method not in ("PICKUP", "DELIVERY"):
        raise ValueError(f"Unknown fulfillment method {method!r} — must be PICKUP or DELIVERY")
    if method == "DELIVERY" and not delivery_address:
        raise ValueError("A delivery address is required for delivery orders")
    if time_preference == "SCHEDULED" and scheduled_for is None:
        raise ValueError("A scheduled time is required when time_preference is SCHEDULED")

    db = await get_supabase()
    payload = {
        "fulfillment_method": method,
        "fulfillment_time_preference": time_preference,
        "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        # Pickup has no fee to confirm — 0 is correct and final, so mark it confirmed
        # immediately rather than leaving it in the "waiting on a human" state.
        "delivery_fee_confirmed": method == "PICKUP",
        # Confirmed bug from a real transcript: this used to omit updated_at, so the
        # cart-staleness clock (get_or_create_open_cart) kept counting from whenever an
        # item was last added/priced, NOT from this — the actual last real interaction.
        # A customer who took a couple of hours to answer "pickup or delivery?" could
        # have their cart silently expire mid-checkout with no warning.
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if method == "DELIVERY":
        payload.update({
            "delivery_address": delivery_address,
            "delivery_area": delivery_area,
            "delivery_landmark": delivery_landmark,
            "delivery_fee": 0.0,
        })

    updated = await db.table("orders").update(payload).eq("id", str(order_id)).execute()
    await log.info("FULFILLMENT_DETAILS_SET", ref_type="order", ref_id=order_id, metadata={"method": method})
    return updated.data[0] if updated.data else await recalculate_cart(order_id)


async def get_cart_detail(order_id: UUID) -> dict:
    """Full cart/order with items and their modifiers nested in — what gets quoted back to the customer."""
    db = await get_supabase()
    order = (await db.table("orders").select("*").eq("id", str(order_id)).single().execute()).data
    items = (await db.table("order_items").select("*").eq("order_id", str(order_id)).execute()).data or []

    for item in items:
        mods = (
            await db.table("order_item_modifiers")
            .select("modifier_id, modifier_name, price")
            .eq("order_item_id", item["id"])
            .execute()
        ).data or []
        item["modifiers"] = mods

    order["items"] = items
    return order


# ── Checkout / payment lifecycle ───────────────────────────────

async def start_checkout(order_id: UUID) -> dict:
    """
    CART -> PAYMENT_PENDING. Does not touch Paystack — that's paystack.py's
    job, called right after this.

    Payment only ever covers the food subtotal, never delivery — she
    confirms the delivery fee with the customer herself, after payment,
    once she knows the address. So the only gate here is knowing pickup vs
    delivery (and, for delivery, having an address — enforced earlier by
    set_fulfillment_details, not re-checked here). There is deliberately no
    "wait for a delivery fee" gate: that would block payment on a manual
    step that, per how this business actually runs, happens afterward.
    """
    db = await get_supabase()
    order = (await db.table("orders").select("*").eq("id", str(order_id)).single().execute()).data
    if order["status"] != "CART":
        raise ValueError(f"Cannot start checkout on an order in status {order['status']!r}")
    if not order.get("subtotal") or float(order["subtotal"]) <= 0:
        raise ValueError("Your cart's empty right now — what would you like to order?")
    if not order.get("fulfillment_method"):
        raise ValueError("Would you like pickup or delivery for this order?")

    updated = (
        await db.table("orders")
        .update({"status": "PAYMENT_PENDING", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", str(order_id))
        .execute()
    )
    return updated.data[0]


async def mark_paid(order_id: UUID, payment_reference: str) -> Optional[dict]:
    """
    PAYMENT_PENDING -> PAID. Idempotent: if the order is already PAID (a
    duplicate webhook got here first), this is a no-op that returns the
    existing row rather than re-processing — "your system must not create
    two orders" from a duplicate webhook, satisfied at this layer.
    """
    db = await get_supabase()
    order = (await db.table("orders").select("*").eq("id", str(order_id)).single().execute()).data
    if order is None:
        return None
    if order["status"] == "PAID":
        await log.info("DUPLICATE_PAID_WEBHOOK_IGNORED", ref_type="order", ref_id=order_id, metadata={"reference": payment_reference})
        return order

    now = datetime.now(timezone.utc).isoformat()
    updated = (
        await db.table("orders")
        .update({"status": "PAID", "paid_at": now, "updated_at": now})
        .eq("id", str(order_id))
        .eq("status", "PAYMENT_PENDING")  # only transitions from the expected prior state
        .execute()
    )
    rows = updated.data or []
    if not rows:
        # Someone else already moved it (race with another webhook delivery) — treat as success, not error.
        return (await db.table("orders").select("*").eq("id", str(order_id)).single().execute()).data

    customer_id = order["customer_id"]
    customer = (await db.table("customers").select("total_orders").eq("id", customer_id).single().execute()).data
    await db.table("customers").update({
        "last_order_at": now,
        "total_orders": (customer.get("total_orders", 0) if customer else 0) + 1,
    }).eq("id", customer_id).execute()

    await log.info("ORDER_PAID", ref_type="order", ref_id=order_id, metadata={"reference": payment_reference, "total": order["total"]})
    return rows[0]


async def update_status(order_id: UUID, new_status: str) -> dict:
    """PAID -> PREPARING -> READY -> COMPLETED (merchant-driven, from the dashboard)."""
    valid_transitions = {
        "PAID":       {"PREPARING", "CANCELLED"},
        "PREPARING":  {"READY", "CANCELLED"},
        "READY":      {"COMPLETED", "CANCELLED"},
    }
    db = await get_supabase()
    order = (await db.table("orders").select("status").eq("id", str(order_id)).single().execute()).data
    current = order["status"]

    if new_status not in valid_transitions.get(current, set()):
        raise ValueError(f"Cannot move order from {current!r} to {new_status!r}")

    updated = (
        await db.table("orders")
        .update({"status": new_status, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", str(order_id))
        .execute()
    )
    await log.info("ORDER_STATUS_CHANGED", ref_type="order", ref_id=order_id, metadata={"from": current, "to": new_status})
    return updated.data[0]


async def expire_stale_pending_orders(org_id: UUID, older_than_minutes: int = 30) -> int:
    """
    'Customer abandons checkout' -> order remains PAYMENT_PENDING, not PAID,
    per the original test scenario — but a PAYMENT_PENDING order sitting
    forever also isn't useful. This flips genuinely abandoned ones to
    EXPIRED after a grace period. Intended to run on a schedule (cron /
    background task), not inline in the request path.
    """
    db = await get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    stale = (
        await db.table("orders")
        .select("id")
        .eq("org_id", str(org_id))
        .eq("status", "PAYMENT_PENDING")
        .lt("updated_at", cutoff)
        .execute()
    ).data or []

    for row in stale:
        await db.table("orders").update({"status": "EXPIRED"}).eq("id", row["id"]).eq("status", "PAYMENT_PENDING").execute()

    return len(stale)


async def get_pending_payment_link(order_id: UUID) -> Optional[str]:
    """Looks up the authorization_url stored when checkout started — lets a reminder resend the actual working link."""
    db = await get_supabase()
    result = (
        await db.table("payments")
        .select("authorization_url")
        .eq("order_id", str(order_id))
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0].get("authorization_url") if rows else None


async def duplicate_order_items(source_order_id: UUID, dest_order_id: UUID) -> dict:
    """
    Copies every line item (with its modifiers) and the fulfillment details
    from source_order_id into dest_order_id, then recalculates dest's
    totals. Built specifically for "cancel it and create the same order
    again" — rebuilding by hand would mean the customer retyping their
    entire order, which defeats the point of asking for it in the first
    place. Deliberately code-driven, not LLM-improvised: money and cart
    contents stay deterministic even for this convenience feature.
    """
    db = await get_supabase()

    source_order = (
        await db.table("orders")
        .select("fulfillment_method, delivery_address, delivery_area, delivery_landmark, fulfillment_time_preference")
        .eq("id", str(source_order_id))
        .single()
        .execute()
    ).data or {}

    source_items = (
        await db.table("order_items").select("*").eq("order_id", str(source_order_id)).execute()
    ).data or []

    for item in source_items:
        new_item = (
            await db.table("order_items")
            .insert({
                "order_id": str(dest_order_id),
                "product_id": item.get("product_id"),
                "product_name": item["product_name"],
                "base_price": item["base_price"],
                "quantity": item["quantity"],
                "line_total": item["line_total"],
            })
            .execute()
        ).data[0]

        source_mods = (
            await db.table("order_item_modifiers")
            .select("modifier_id, modifier_name, price")
            .eq("order_item_id", item["id"])
            .execute()
        ).data or []
        for m in source_mods:
            await db.table("order_item_modifiers").insert({
                "order_item_id": new_item["id"],
                "modifier_id": m.get("modifier_id"),
                "modifier_name": m["modifier_name"],
                "price": m["price"],
            }).execute()

    if source_order.get("fulfillment_method"):
        await set_fulfillment_details(
            dest_order_id,
            method=source_order["fulfillment_method"],
            delivery_address=source_order.get("delivery_address"),
            delivery_area=source_order.get("delivery_area"),
            delivery_landmark=source_order.get("delivery_landmark"),
            time_preference=source_order.get("fulfillment_time_preference") or "ASAP",
        )

    return await recalculate_cart(dest_order_id)


async def get_pending_payment_order(org_id: UUID, customer_id: UUID) -> Optional[dict]:
    """
    The fix for a real, dangerous bug: once checkout starts, the order
    leaves CART status and get_or_create_open_cart can no longer see it —
    the next message would silently spin up a brand-new EMPTY cart, and
    the ordering LLM, seeing nothing in it, would freely improvise a close
    ("Enjoy your order!") with zero actual awareness a payment was ever in
    flight. message_pipeline checks this BEFORE touching the ordering flow
    at all, so an in-flight checkout is never invisible to the conversation.
    """
    db = await get_supabase()
    result = (
        await db.table("orders")
        .select("*")
        .eq("customer_id", str(customer_id))
        .eq("status", "PAYMENT_PENDING")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


async def cancel_pending_order(order_id: UUID) -> Optional[dict]:
    """Customer-initiated cancellation of their own in-flight checkout — separate from
    update_status's staff-driven transitions, since PAYMENT_PENDING -> CANCELLED here
    means 'I changed my mind before paying', not a kitchen/fulfillment decision."""
    db = await get_supabase()
    updated = (
        await db.table("orders")
        .update({"status": "CANCELLED", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", str(order_id))
        .eq("status", "PAYMENT_PENDING")
        .execute()
    )
    return updated.data[0] if updated.data else None


async def record_pending_payment(order_id: UUID, reference: str, amount, authorization_url: str = "") -> dict:
    """Called right after paystack.initialize_transaction succeeds — creates the payments row the webhook will later look up by reference."""
    db = await get_supabase()
    result = (
        await db.table("payments")
        .insert({
            "order_id": str(order_id), "reference": reference,
            "amount": float(amount), "status": "pending",
            "authorization_url": authorization_url,
        })
        .execute()
    )
    return result.data[0]


async def get_order_by_payment_reference(reference: str) -> Optional[dict]:
    db = await get_supabase()
    payment = (await db.table("payments").select("order_id").eq("reference", reference).execute()).data
    if not payment:
        return None
    return (await db.table("orders").select("*").eq("id", payment[0]["order_id"]).single().execute()).data


async def mark_payment_verified(reference: str, status: str) -> None:
    db = await get_supabase()
    from datetime import datetime, timezone
    await db.table("payments").update({
        "status": status,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }).eq("reference", reference).execute()


# ── Dashboard queries ───────────────────────────────────────────

async def list_orders(
    org_id: UUID,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """
    The 'show me everything ordered between Aug 20 and Aug 30' query. Filters
    on created_at, paginated, most recent first.
    """
    db = await get_supabase()
    offset = (page - 1) * limit

    query = (
        db.table("orders")
        .select(
            "id, status, subtotal, delivery_fee, total, created_at, updated_at, paid_at, "
            "customer_id, delivery_address, fulfillment_method, delivery_area, delivery_fee_confirmed"
        )
        .eq("org_id", str(org_id))
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if status:
        query = query.eq("status", status)
    if date_from:
        query = query.gte("created_at", date_from.isoformat())
    if date_to:
        query = query.lte("created_at", date_to.isoformat())

    result = await query.execute()
    orders = result.data or []

    count_query = db.table("orders").select("id", count="exact").eq("org_id", str(org_id))
    if status:
        count_query = count_query.eq("status", status)
    if date_from:
        count_query = count_query.gte("created_at", date_from.isoformat())
    if date_to:
        count_query = count_query.lte("created_at", date_to.isoformat())
    count_result = await count_query.execute()
    total = count_result.count or len(orders)

    customer_ids = list({o["customer_id"] for o in orders if o.get("customer_id")})
    customers_by_id = {}
    if customer_ids:
        customers = (
            await db.table("customers").select("id, name, phone_number").in_("id", customer_ids).execute()
        ).data or []
        customers_by_id = {c["id"]: c for c in customers}

    for o in orders:
        c = customers_by_id.get(o.get("customer_id"))
        o["customer_name"] = c.get("name") if c else None
        o["customer_phone"] = c.get("phone_number") if c else None

    return orders, total


async def get_order_metrics(org_id: UUID, days: int = 7) -> dict:
    db = await get_supabase()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    orders = (
        await db.table("orders")
        .select("id, status, subtotal, delivery_fee, total, created_at")
        .eq("org_id", str(org_id))
        .gte("created_at", since)
        .execute()
    ).data or []

    paid_orders = [o for o in orders if o["status"] not in ("CART", "PAYMENT_PENDING", "EXPIRED", "CANCELLED")]
    # food_revenue is what Paystack actually verified (subtotal only — see start_checkout).
    # delivery_fees_recorded is what she's told the dashboard she quoted customers, tracked
    # for visibility but collected off-platform, so it's kept separate rather than folded
    # into "revenue" where it would misrepresent what actually came through Paystack.
    food_revenue = sum(float(o["subtotal"]) for o in paid_orders)
    delivery_fees_recorded = sum(float(o.get("delivery_fee") or 0) for o in paid_orders)
    abandoned = sum(1 for o in orders if o["status"] == "EXPIRED")

    return {
        "period_days":     days,
        "total_orders":    len(paid_orders),
        "food_revenue":    round(food_revenue, 2),
        "delivery_fees_recorded": round(delivery_fees_recorded, 2),
        "abandoned_carts": abandoned,
        "by_status": {
            s: sum(1 for o in orders if o["status"] == s)
            for s in ("PAID", "PREPARING", "READY", "COMPLETED", "CANCELLED", "EXPIRED")
        },
    }
