from decimal import Decimal
import pytest
from app.services import pricing_engine


def test_line_item_total_single_item_no_modifiers():
    assert pricing_engine.line_item_total(2500, [], 1) == Decimal("2500.00")


def test_line_item_total_with_modifiers_and_quantity():
    # Loaded Fries (2500) + Chicken (500) + Extra Cheese (300), qty 2
    result = pricing_engine.line_item_total(2500, [500, 300], 2)
    assert result == Decimal("6600.00")


def test_line_item_total_rejects_zero_quantity():
    with pytest.raises(ValueError):
        pricing_engine.line_item_total(2500, [], 0)


def test_cart_subtotal_sums_all_lines():
    assert pricing_engine.cart_subtotal([6600, 1200, 500]) == Decimal("8300.00")


def test_cart_subtotal_empty_cart_is_zero():
    assert pricing_engine.cart_subtotal([]) == Decimal("0.00")


def test_cart_total_adds_delivery_once():
    assert pricing_engine.cart_total(8300, 1000) == Decimal("9300.00")


def test_to_kobo_conversion():
    assert pricing_engine.to_kobo(4500) == 450000
    assert pricing_engine.to_kobo("4500.50") == 450050


def test_from_kobo_conversion():
    assert pricing_engine.from_kobo(450000) == Decimal("4500.00")


def test_to_kobo_and_from_kobo_are_inverses():
    original = Decimal("1234.56")
    assert pricing_engine.from_kobo(pricing_engine.to_kobo(original)) == original


def test_no_float_rounding_drift_across_many_additions():
    # The classic float trap: 0.1 + 0.2 != 0.3 in binary floating point.
    # Decimal-based pricing must not exhibit this over repeated small additions.
    total = pricing_engine.cart_subtotal([Decimal("0.10")] * 10)
    assert total == Decimal("1.00")
