import pytest
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
    c1 = await orders_module.get_or_create_customer(org_id, "+234 801-234-5678", name="Ada")
    c2 = await orders_module.get_or_create_customer(org_id, "2348012345678", name="Someone Else")

    assert c1["id"] == c2["id"]
    assert c2["name"] == "Ada"  # first name wins, not overwritten by a later push_name


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
