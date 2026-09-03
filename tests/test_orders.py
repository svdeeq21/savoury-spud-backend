import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from app.services import orders as orders_module


@pytest.fixture
def patched_db(monkeypatch, fake_db):
    async def _fake_get_supabase():
        return fake_db
    monkeypatch.setattr(orders_module, "get_supabase", _fake_get_supabase)
    return fake_db


async def test_get_or_create_customer_is_idempotent_on_phone(patched_db):
    org_id = uuid4()
    c1, is_new1 = await orders_module.get_or_create_customer(org_id, "+234 801-234-5678", name="Ada")
    c2, is_new2 = await orders_module.get_or_create_customer(org_id, "2348012345678", name="Someone Else")

    assert c1["id"] == c2["id"]
    assert c2["name"] == "Ada"  # first name wins, not overwritten by a later push_name
    assert is_new1 is True   # first contact ever from this number
    assert is_new2 is False  # same number, second contact


async def test_get_or_create_open_cart_reuses_existing_cart(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    # seed a customer row so foreign-key-shaped lookups have something to find, even though
    # the fake DB doesn't enforce real FKs
    patched_db.tables.setdefault("customers", []).append({"id": str(customer_id), "org_id": str(org_id)})

    cart1 = await orders_module.get_or_create_open_cart(org_id, customer_id)
    cart2 = await orders_module.get_or_create_open_cart(org_id, customer_id)

    assert cart1["id"] == cart2["id"]
    assert cart1["status"] == "CART"


async def test_add_product_recalculates_cart_total(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)

    product = {"id": "prod-1", "name": "Loaded Fries", "base_price": 2500}
    modifiers = [{"id": "mod-1", "name": "Chicken", "price": 500}, {"id": "mod-2", "name": "Extra Cheese", "price": 300}]

    await orders_module.add_product(cart["id"], product, quantity=2, modifiers=modifiers)

    updated_cart = [o for o in patched_db.tables["orders"] if o["id"] == cart["id"]][0]
    assert float(updated_cart["subtotal"]) == 6600.0  # (2500+500+300) * 2
    assert float(updated_cart["total"]) == 6600.0      # no delivery fee set yet


async def test_remove_modifier_reprices_the_line_item(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = {"id": "prod-1", "name": "Loaded Fries", "base_price": 2500}
    modifiers = [{"id": "mod-1", "name": "Chicken", "price": 500}, {"id": "mod-2", "name": "Extra Cheese", "price": 300}]
    item = await orders_module.add_product(cart["id"], product, quantity=1, modifiers=modifiers)

    # "Actually remove the cheese" — test scenario from the original brief
    await orders_module.remove_modifier(item["id"], "mod-2", cart["id"])

    updated_item = [i for i in patched_db.tables["order_items"] if i["id"] == item["id"]][0]
    assert float(updated_item["line_total"]) == 3000.0  # 2500 + 500, cheese gone

    updated_cart = [o for o in patched_db.tables["orders"] if o["id"] == cart["id"]][0]
    assert float(updated_cart["subtotal"]) == 3000.0


async def test_set_quantity_updates_line_total_and_cart(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = {"id": "prod-1", "name": "Pepsi", "base_price": 500}
    item = await orders_module.add_product(cart["id"], product, quantity=1, modifiers=[])

    await orders_module.set_quantity(item["id"], cart["id"], 3)

    updated_item = [i for i in patched_db.tables["order_items"] if i["id"] == item["id"]][0]
    assert float(updated_item["line_total"]) == 1500.0


async def test_set_quantity_rejects_zero():
    with pytest.raises(ValueError):
        await orders_module.set_quantity(uuid4(), uuid4(), 0)


async def test_start_checkout_rejects_empty_cart(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    with pytest.raises(ValueError):
        await orders_module.start_checkout(cart["id"])


async def test_mark_paid_is_idempotent_against_duplicate_webhook(patched_db):
    """The exact 'duplicate webhook must not create two orders' scenario from the brief."""
    org_id, customer_id = uuid4(), uuid4()
    patched_db.tables.setdefault("customers", []).append({"id": str(customer_id), "org_id": str(org_id), "total_orders": 0})
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = {"id": "prod-1", "name": "Loaded Fries", "base_price": 2500}
    await orders_module.add_product(cart["id"], product, quantity=1, modifiers=[])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    order = await orders_module.start_checkout(cart["id"])
    assert order["status"] == "PAYMENT_PENDING"

    first = await orders_module.mark_paid(order["id"], "ref_abc123")
    second = await orders_module.mark_paid(order["id"], "ref_abc123")  # redelivered webhook

    assert first["status"] == "PAID"
    assert second["status"] == "PAID"

    # Only ever one order row for this cart — mark_paid never inserts, only updates.
    matching_orders = [o for o in patched_db.tables["orders"] if o["id"] == order["id"]]
    assert len(matching_orders) == 1

    # Customer's total_orders incremented exactly once, not twice.
    customer_row = [c for c in patched_db.tables["customers"] if c["id"] == str(customer_id)][0]
    assert customer_row["total_orders"] == 1


async def test_update_status_enforces_valid_transitions(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    patched_db.tables.setdefault("customers", []).append({"id": str(customer_id), "org_id": str(org_id), "total_orders": 0})
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Fries", "base_price": 1000}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    order = await orders_module.start_checkout(cart["id"])
    await orders_module.mark_paid(order["id"], "ref_xyz")

    updated = await orders_module.update_status(order["id"], "PREPARING")
    assert updated["status"] == "PREPARING"

    with pytest.raises(ValueError):
        # Can't skip straight from PREPARING to COMPLETED
        await orders_module.update_status(order["id"], "COMPLETED")


# ── Modifier selection validation (the "N included, extra costs more" rules) ──

def _build_your_box_product():
    """Mirrors the real seeded product/groups closely enough to test the validation rules against."""
    return {
        "id": "box-1", "name": "Build Your Box", "base_price": 0,
        "modifier_groups": [
            {"id": "g-size", "name": "Size", "selection_type": "single", "required": True, "max_selections": 1,
             "modifiers": [{"id": "m-regular", "name": "Regular", "price": 9000},
                           {"id": "m-large", "name": "Large", "price": 11000}]},
            {"id": "g-protein", "name": "Protein", "selection_type": "single", "required": True, "max_selections": 1,
             "modifiers": [{"id": "m-chicken", "name": "Crispy Chicken", "price": 0},
                           {"id": "m-beef", "name": "Shawarma Beef", "price": 0}]},
            {"id": "g-toppings", "name": "Toppings", "selection_type": "multiple", "required": True, "max_selections": 2,
             "modifiers": [{"id": "m-cheese", "name": "Cheese Sauce", "price": 0},
                           {"id": "m-corn", "name": "Corn Salad", "price": 0},
                           {"id": "m-salsa", "name": "Mexican Salsa", "price": 0}]},
            {"id": "g-sauces", "name": "Sauces", "selection_type": "multiple", "required": True, "max_selections": 1,
             "modifiers": [{"id": "m-garlic", "name": "Garlic Sauce", "price": 0},
                           {"id": "m-burger", "name": "Burger Sauce", "price": 0},
                           {"id": "m-bbq", "name": "BBQ Sauce", "price": 0},
                           {"id": "m-honeymustard", "name": "Hot Honey Mustard", "price": 0},
                           {"id": "m-yaji", "name": "Yaji Sauce", "price": 0}]},
            {"id": "g-extras", "name": "Extras", "selection_type": "multiple", "required": False, "max_selections": None,
             "modifiers": [{"id": "m-extra-topping", "name": "Extra Toppings", "price": 500}]},
        ],
    }


async def test_add_product_rejects_missing_required_group(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    # Size (required) never selected
    modifiers = [{"id": "m-chicken", "name": "Crispy Chicken", "price": 0}]
    with pytest.raises(ValueError, match="Size"):
        await orders_module.add_product(cart["id"], product, 1, modifiers)


async def test_add_product_enforces_max_selections_on_toppings(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    # 3 toppings when only 2 are included free — the "give me chicken for free" adjacent case
    modifiers = [
        {"id": "m-regular", "name": "Regular", "price": 9000},
        {"id": "m-chicken", "name": "Crispy Chicken", "price": 0},
        {"id": "m-cheese", "name": "Cheese Sauce", "price": 0},
        {"id": "m-corn", "name": "Corn Salad", "price": 0},
        {"id": "m-salsa", "name": "Mexican Salsa", "price": 0},
    ]
    with pytest.raises(ValueError, match="Toppings"):
        await orders_module.add_product(cart["id"], product, 1, modifiers)


async def test_add_product_accepts_a_full_valid_selection(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    modifiers = [
        {"id": "m-regular", "name": "Regular", "price": 9000},
        {"id": "m-chicken", "name": "Crispy Chicken", "price": 0},
        {"id": "m-cheese", "name": "Cheese Sauce", "price": 0},
        {"id": "m-corn", "name": "Corn Salad", "price": 0},
        {"id": "m-garlic", "name": "Garlic Sauce", "price": 0},
    ]
    item = await orders_module.add_product(cart["id"], product, 1, modifiers)
    assert item is not None
    updated_cart = [o for o in patched_db.tables["orders"] if o["id"] == cart["id"]][0]
    assert float(updated_cart["subtotal"]) == 9000.0


async def test_add_product_extras_group_is_repeatable(patched_db):
    """Selecting the same uncapped Extras modifier twice (two extra toppings) should sum, not dedupe."""
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    modifiers = [
        {"id": "m-regular", "name": "Regular", "price": 9000},
        {"id": "m-chicken", "name": "Crispy Chicken", "price": 0},
        {"id": "m-cheese", "name": "Cheese Sauce", "price": 0},
        {"id": "m-garlic", "name": "Garlic Sauce", "price": 0},
        {"id": "m-extra-topping", "name": "Extra Toppings", "price": 500},
        {"id": "m-extra-topping", "name": "Extra Toppings", "price": 500},  # a 2nd extra topping
    ]
    item = await orders_module.add_product(cart["id"], product, 1, modifiers)
    assert float(item["line_total"]) == 10000.0  # 9000 + 0 + 0 + 500 + 500


# ── Fulfillment (pickup / delivery) ────────────────────────────

async def test_set_fulfillment_pickup_marks_fee_confirmed_immediately(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)

    updated = await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    assert updated["fulfillment_method"] == "PICKUP"
    assert updated["delivery_fee_confirmed"] is True


async def test_set_fulfillment_delivery_requires_address(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)

    with pytest.raises(ValueError):
        await orders_module.set_fulfillment_details(cart["id"], method="DELIVERY")


async def test_set_fulfillment_delivery_leaves_fee_unconfirmed(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)

    updated = await orders_module.set_fulfillment_details(
        cart["id"], method="DELIVERY", delivery_address="12 Example St", delivery_area="Wuse"
    )
    assert updated["fulfillment_method"] == "DELIVERY"
    assert updated["delivery_fee_confirmed"] is False
    assert float(updated["delivery_fee"]) == 0.0


# ── Checkout gates discovered from the real order flow ──────────

async def test_start_checkout_blocks_without_fulfillment_method(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])

    with pytest.raises(ValueError, match="pickup or delivery"):
        await orders_module.start_checkout(cart["id"])


async def test_start_checkout_allows_pickup_immediately(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")

    order = await orders_module.start_checkout(cart["id"])
    assert order["status"] == "PAYMENT_PENDING"


async def test_start_checkout_charges_food_subtotal_regardless_of_pickup_or_delivery(patched_db):
    """Checkout never waits on a delivery fee — it isn't quoted yet, and payment only ever covers the food."""
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="DELIVERY", delivery_address="12 Example St")

    order = await orders_module.start_checkout(cart["id"])
    assert order["status"] == "PAYMENT_PENDING"
    assert float(order["subtotal"]) == 2500.0
    assert float(order["delivery_fee"]) == 0.0  # nothing quoted yet — that's expected, not a blocker


async def test_delivery_fee_is_recorded_after_payment_not_before(patched_db):
    """Matches the real flow: pay for food first, she reaches out with the delivery fee afterward."""
    org_id, customer_id = uuid4(), uuid4()
    patched_db.tables.setdefault("customers", []).append({"id": str(customer_id), "org_id": str(org_id), "total_orders": 0})
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="DELIVERY", delivery_address="12 Example St")

    order = await orders_module.start_checkout(cart["id"])
    paid_order = await orders_module.mark_paid(order["id"], "ref_delivery_1")
    assert paid_order["status"] == "PAID"
    assert float(paid_order["subtotal"]) == 2500.0  # what was actually charged via Paystack

    # Only now, after payment, does she quote and record the delivery fee.
    updated = await orders_module.set_delivery_fee(order["id"], 1500)
    assert float(updated["delivery_fee"]) == 1500.0
    assert float(updated["total"]) == 4000.0  # informational grand total — not re-charged


async def test_metrics_separate_food_revenue_from_delivery_fees(patched_db):
    """food_revenue is what Paystack verified; delivery_fees_recorded is informational and kept apart."""
    org_id, customer_id = uuid4(), uuid4()
    patched_db.tables.setdefault("customers", []).append({"id": str(customer_id), "org_id": str(org_id), "total_orders": 0})
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="DELIVERY", delivery_address="12 Example St")
    order = await orders_module.start_checkout(cart["id"])
    await orders_module.mark_paid(order["id"], "ref_metrics_1")
    await orders_module.set_delivery_fee(order["id"], 1500)

    metrics = await orders_module.get_order_metrics(org_id, days=7)
    assert metrics["food_revenue"] == 2500.0
    assert metrics["delivery_fees_recorded"] == 1500.0


# ── Payment-pending gate (the fix for the "Enjoy your order!" bug) ──

async def test_set_fulfillment_details_bumps_updated_at(patched_db):
    """
    Confirmed bug from a real transcript: this used to leave updated_at untouched,
    so the cart-staleness clock kept counting from the last pricing mutation instead
    of the last real interaction, silently expiring carts mid-checkout.
    """
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    original_updated_at = cart.get("updated_at")

    updated = await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")

    assert updated.get("updated_at") is not None
    assert updated.get("updated_at") != original_updated_at


async def test_get_pending_payment_order_finds_the_right_order(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    order = await orders_module.start_checkout(cart["id"])

    pending = await orders_module.get_pending_payment_order(org_id, customer_id)
    assert pending is not None
    assert pending["id"] == order["id"]
    assert pending["status"] == "PAYMENT_PENDING"


async def test_get_pending_payment_order_returns_none_when_no_checkout_in_flight(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    await orders_module.get_or_create_open_cart(org_id, customer_id)  # just a cart, never checked out

    pending = await orders_module.get_pending_payment_order(org_id, customer_id)
    assert pending is None


async def test_cancel_pending_order_only_cancels_from_payment_pending(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    order = await orders_module.start_checkout(cart["id"])

    cancelled = await orders_module.cancel_pending_order(order["id"])
    assert cancelled["status"] == "CANCELLED"

    # And the customer can now start a fresh cart — the cancelled one is out of the way.
    new_cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    assert new_cart["id"] != order["id"]
    assert new_cart["status"] == "CART"


async def test_record_pending_payment_stores_authorization_url(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    await orders_module.add_product(cart["id"], {"id": "prod-1", "name": "Chapman", "base_price": 2500}, 1, [])
    await orders_module.set_fulfillment_details(cart["id"], method="PICKUP")
    order = await orders_module.start_checkout(cart["id"])

    await orders_module.record_pending_payment(order["id"], "ref_xyz", 2500, "https://checkout.paystack.com/xyz")

    link = await orders_module.get_pending_payment_link(order["id"])
    assert link == "https://checkout.paystack.com/xyz"


# ── Cart staleness (returning after a long gap starts fresh, not resuming an old cart) ──

async def test_get_or_create_open_cart_reuses_recent_cart(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart1 = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)
    cart2 = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)
    assert cart1["id"] == cart2["id"]


async def test_get_or_create_open_cart_starts_fresh_after_staleness_window(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    old_cart = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)

    # Simulate "hasn't touched this cart in 10 hours" by backdating it directly in the fake DB.
    ten_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    for row in patched_db.tables["orders"]:
        if row["id"] == old_cart["id"]:
            row["updated_at"] = ten_hours_ago
            row["created_at"] = ten_hours_ago

    new_cart = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)

    assert new_cart["id"] != old_cart["id"]
    expired_row = [o for o in patched_db.tables["orders"] if o["id"] == old_cart["id"]][0]
    assert expired_row["status"] == "EXPIRED"


async def test_get_or_create_open_cart_tolerates_a_short_pause(patched_db):
    """A customer who steps away for 20 minutes mid-order should NOT get a fresh cart."""
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)

    twenty_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    for row in patched_db.tables["orders"]:
        if row["id"] == cart["id"]:
            row["updated_at"] = twenty_min_ago

    same_cart = await orders_module.get_or_create_open_cart(org_id, customer_id, stale_after_hours=4.0)
    assert same_cart["id"] == cart["id"]


# ── Draft item accumulation (the fix for losing partial answers turn by turn) ──

async def test_update_draft_item_persists_partial_answer_instead_of_discarding_it(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    # Size + Protein given, nothing else yet — should NOT commit, but should be remembered.
    result = await orders_module.update_draft_item(
        cart["id"], product, 1,
        [{"id": "m-regular", "name": "Regular", "price": 9000}, {"id": "m-chicken", "name": "Crispy Chicken", "price": 0}],
    )
    assert result["committed"] is False
    assert {g["name"] for g in result["missing"]} == {"Toppings", "Sauces"}
    assert {m["name"] for m in result["selected_so_far"]} == {"Regular", "Crispy Chicken"}

    order_row = [o for o in patched_db.tables["orders"] if o["id"] == cart["id"]][0]
    assert set(order_row["draft_item"]["modifier_ids"]) == {"m-regular", "m-chicken"}


async def test_update_draft_item_reproduces_and_fixes_the_live_transcript_bug(patched_db):
    """
    The exact sequence from the failing WhatsApp transcript: Base+Protein
    given, then three more messages that each supply something, none of
    which should ever lose what came before. Ends fully committed with
    everything the customer actually asked for.
    """
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    # Turn: "regular" (Size) — via an earlier call, then "Plantain base with shawarma chicken"
    r1 = await orders_module.update_draft_item(
        cart["id"], product, 1,
        [{"id": "m-regular", "name": "Regular", "price": 9000}, {"id": "m-chicken", "name": "Crispy Chicken", "price": 0}],
    )
    assert r1["committed"] is False

    # Turn: "BBQ Sauce" (a Sauces answer, not Toppings — must be remembered even though Toppings is still missing)
    r2 = await orders_module.update_draft_item(
        cart["id"], product, 1,
        [{"id": "m-bbq", "name": "BBQ Sauce", "price": 0}],
    )
    assert r2["committed"] is False
    assert {g["name"] for g in r2["missing"]} == {"Toppings"}  # Sauces now satisfied, only Toppings left
    assert {m["name"] for m in r2["selected_so_far"]} == {"Regular", "Crispy Chicken", "BBQ Sauce"}

    # Turn: "Cheese Sauce and Mexican Salsa" (Toppings) — should now complete and commit everything at once
    r3 = await orders_module.update_draft_item(
        cart["id"], product, 1,
        [{"id": "m-cheese", "name": "Cheese Sauce", "price": 0}, {"id": "m-salsa", "name": "Mexican Salsa", "price": 0}],
    )
    assert r3["committed"] is True
    item_modifier_names = {
        m["modifier_name"] for m in patched_db.tables["order_item_modifiers"] if m["order_item_id"] == r3["item"]["id"]
    }
    assert item_modifier_names == {"Regular", "Crispy Chicken", "BBQ Sauce", "Cheese Sauce", "Mexican Salsa"}

    # Draft is cleared once committed — nothing left dangling for the next item.
    order_row = [o for o in patched_db.tables["orders"] if o["id"] == cart["id"]][0]
    assert order_row["draft_item"] is None


async def test_update_draft_item_single_select_group_is_replaced_not_accumulated(patched_db):
    """Changing your mind on Base (single-select) should replace it, not add a second base."""
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    await orders_module.update_draft_item(cart["id"], product, 1, [{"id": "m-regular", "name": "Regular", "price": 9000}])
    result = await orders_module.update_draft_item(cart["id"], product, 1, [{"id": "m-large", "name": "Large", "price": 11000}])

    selected_names = {m["name"] for m in result["selected_so_far"]}
    assert "Large" in selected_names
    assert "Regular" not in selected_names  # replaced, not both present


async def test_update_draft_item_multi_select_group_accumulates_across_turns(patched_db):
    """Toppings given across two separate messages should both end up selected, not overwrite each other."""
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    await orders_module.update_draft_item(cart["id"], product, 1, [{"id": "m-cheese", "name": "Cheese Sauce", "price": 0}])
    result = await orders_module.update_draft_item(cart["id"], product, 1, [{"id": "m-corn", "name": "Corn Salad", "price": 0}])

    selected_names = {m["name"] for m in result["selected_so_far"]}
    assert {"Cheese Sauce", "Corn Salad"} <= selected_names  # both present, neither lost


async def test_update_draft_item_with_zero_modifiers_lists_every_required_group(patched_db):
    """
    The 'I want to order a box' case with nothing else said yet — calling
    add_product immediately with an empty modifier list (as the LLM is now
    instructed to do) should surface every required group and its real
    options in one shot, not just the first one encountered.
    """
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    product = _build_your_box_product()

    result = await orders_module.update_draft_item(cart["id"], product, 1, [])

    assert result["committed"] is False
    assert result["selected_so_far"] == []
    assert {g["name"] for g in result["missing"]} == {"Size", "Protein", "Toppings", "Sauces"}
    # Extras is optional — never shows up as "missing", it's not required
    assert "Extras" not in {g["name"] for g in result["missing"]}


async def test_update_draft_item_starting_a_different_product_resets_the_draft(patched_db):
    org_id, customer_id = uuid4(), uuid4()
    cart = await orders_module.get_or_create_open_cart(org_id, customer_id)
    box = _build_your_box_product()
    drink = {"id": "prod-drink", "name": "Chapman", "base_price": 2500, "modifier_groups": []}

    await orders_module.update_draft_item(cart["id"], box, 1, [{"id": "m-regular", "name": "Regular", "price": 9000}])
    # Switching to an unrelated product mid-conversation shouldn't drag "Regular" into it.
    result = await orders_module.update_draft_item(cart["id"], drink, 1, [])
    assert result["committed"] is True  # Chapman has no required groups at all
    assert result["item"]["product_name"] == "Chapman"
