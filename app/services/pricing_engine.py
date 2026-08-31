# savoury-spud-backend/app/services/pricing_engine.py
#
# "The AI doesn't calculate this. The AI interprets what the customer said
# and calls your ordering functions." — this module IS that calculation.
#
# Every function here is pure: given the same inputs, always the same
# output, no DB call, no network call, no clock. That's deliberate — this
# is the one part of the system that is not allowed to be fuzzy, and pure
# functions are the easiest thing in the whole codebase to unit test and to
# trust. If a number is ever wrong, the bug is here, not in the LLM prompt.
#
# Money is Decimal throughout, never float — floats introduce rounding
# error that compounds across modifiers, quantities and totals in ways that
# eventually show up as "the app charged me 5 more Naira than the receipt
# said", which is a genuinely bad thing for an ordering system to do.

from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP


def _money(value) -> Decimal:
    """Normalize any numeric input to a 2dp Decimal."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def line_item_total(base_price, modifier_prices: list, quantity: int) -> Decimal:
    """
    (base product price + sum of selected modifier prices) × quantity.

    e.g. Loaded Fries (2500) + Chicken (500) + Extra Cheese (300), qty 2
      -> (2500 + 500 + 300) * 2 = 6600
    """
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    unit_price = _money(base_price)
    for mp in modifier_prices:
        unit_price += _money(mp)

    return _money(unit_price * quantity)


def cart_subtotal(line_totals: list) -> Decimal:
    """Sum of every line item's total. Empty cart -> 0.00."""
    total = Decimal("0.00")
    for lt in line_totals:
        total += _money(lt)
    return total


def cart_total(subtotal, delivery_fee) -> Decimal:
    """subtotal + delivery_fee. Delivery is added once per order, not per item."""
    return _money(_money(subtotal) + _money(delivery_fee))


def to_kobo(naira_amount) -> int:
    """
    Paystack's API takes amounts in the currency's smallest subunit (kobo
    for NGN, cents for USD/GHS, etc) as an integer. This is the ONLY place
    in the codebase that should ever do this conversion — everywhere else
    (schema, pricing engine, dashboard) works in Naira.
    """
    return int(_money(naira_amount) * 100)


def from_kobo(kobo_amount: int) -> Decimal:
    """Inverse of to_kobo — used when reading amounts back off a Paystack response/webhook."""
    return _money(Decimal(kobo_amount) / 100)
