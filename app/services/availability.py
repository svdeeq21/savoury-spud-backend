# savoury-spud-backend/app/services/availability.py
#
# "OPEN != ACCEPTING ORDERS" — the business rule from the original brief,
# as code. Three independent layers, checked in this order:
#
#   1. Manual override (PAUSED) — the emergency stop. Wins over everything.
#   2. Operating hours — the schedule. Wins over CLOSED/OPEN status if the
#      status itself is just left on OPEN and the schedule is what actually
#      governs it (a merchant who never touches the toggle should still be
#      closed outside her hours).
#   3. CLOSED — explicit manual closed, same effect as PAUSED for ordering
#      purposes but semantically distinct (CLOSED = "not operating today",
#      PAUSED = "temporarily can't fulfil right now").
#
# All pure functions — the caller is responsible for fetching the rows and
# passing in `now`.

from __future__ import annotations
from datetime import datetime, time as time_cls, timedelta
from typing import Optional


def to_business_time(now_utc: datetime, utc_offset_hours: float) -> datetime:
    """
    operating_hours.open_time/close_time have no timezone attached — they're
    plain local clock times ("12:00" means noon at the business, not noon
    UTC). This is the one conversion point between "now" (always fetched in
    UTC) and the local time those columns actually mean. Get this offset
    wrong and every open/closed decision is wrong by exactly that many hours.
    """
    return now_utc + timedelta(hours=utc_offset_hours)


def is_within_operating_hours(hours_row: Optional[dict], now: datetime) -> bool:
    """
    hours_row is the operating_hours row for `now`'s day of week (0=Monday),
    or None if no row exists for that day (treated as closed — a missing
    schedule is not an invitation to accept orders 24/7 by accident).
    """
    if hours_row is None:
        return False
    if hours_row.get("is_closed"):
        return False

    open_time = hours_row.get("open_time")
    close_time = hours_row.get("close_time")
    if open_time is None or close_time is None:
        return False

    if isinstance(open_time, str):
        open_time = time_cls.fromisoformat(open_time)
    if isinstance(close_time, str):
        close_time = time_cls.fromisoformat(close_time)

    current = now.time()

    # Handles the ordinary case (open 12:00, close 22:00) and the overnight
    # case (open 18:00, close 02:00) with the same comparison.
    if open_time <= close_time:
        return open_time <= current <= close_time
    return current >= open_time or current <= close_time


def resolve_business_open(
    availability_row: dict,
    hours_row: Optional[dict],
    now: datetime,
) -> tuple[bool, Optional[str]]:
    """
    Returns (is_accepting_orders, customer_facing_reason_if_not).

    This is the single function the ordering flow calls before it will let
    a customer add anything to a cart or proceed to payment.
    """
    status = availability_row.get("status", "OPEN")

    if status == "PAUSED":
        message = availability_row.get("pause_message") or (
            "We're currently unavailable and aren't accepting orders at the moment. "
            "Please check back later."
        )
        return False, message

    if status == "CLOSED":
        return False, "We're closed right now. Please check back during our operating hours."

    # status == OPEN — but OPEN only means "not manually stopped", the
    # schedule still has the final say.
    if not is_within_operating_hours(hours_row, now):
        return False, "We're outside our operating hours right now. Please check back later."

    return True, None


def is_item_available(item_row: dict) -> bool:
    """Product or modifier row -> whether it can still be offered/added."""
    return bool(item_row.get("available", True))


def filter_available(rows: list) -> list:
    """Drop unavailable products/modifiers before they're ever shown or offered to a customer."""
    return [r for r in rows if is_item_available(r)]
