from datetime import datetime, timezone
from app.services import availability


def _dt(hour, minute=0):
    return datetime(2026, 8, 24, hour, minute, tzinfo=timezone.utc)  # a Monday


def test_open_status_within_hours_is_accepting_orders():
    settings_row = {"status": "OPEN"}
    hours_row = {"open_time": "12:00", "close_time": "22:00", "is_closed": False}
    is_open, reason = availability.resolve_business_open(settings_row, hours_row, _dt(15))
    assert is_open is True
    assert reason is None


def test_open_status_outside_hours_is_not_accepting_orders():
    settings_row = {"status": "OPEN"}
    hours_row = {"open_time": "12:00", "close_time": "22:00", "is_closed": False}
    is_open, reason = availability.resolve_business_open(settings_row, hours_row, _dt(23))
    assert is_open is False
    assert reason is not None


def test_manual_pause_overrides_being_within_hours():
    # "Business closed" test scenario, plus "manual pause": even mid-day, mid-hours, PAUSED wins.
    settings_row = {"status": "PAUSED", "pause_message": "Busy today, back soon."}
    hours_row = {"open_time": "12:00", "close_time": "22:00", "is_closed": False}
    is_open, reason = availability.resolve_business_open(settings_row, hours_row, _dt(15))
    assert is_open is False
    assert reason == "Busy today, back soon."


def test_paused_without_custom_message_uses_default():
    settings_row = {"status": "PAUSED", "pause_message": None}
    is_open, reason = availability.resolve_business_open(settings_row, None, _dt(15))
    assert is_open is False
    assert "aren't accepting orders" in reason


def test_closed_status_blocks_regardless_of_hours():
    settings_row = {"status": "CLOSED"}
    hours_row = {"open_time": "00:00", "close_time": "23:59", "is_closed": False}
    is_open, _ = availability.resolve_business_open(settings_row, hours_row, _dt(15))
    assert is_open is False


def test_missing_hours_row_defaults_closed():
    settings_row = {"status": "OPEN"}
    is_open, _ = availability.resolve_business_open(settings_row, None, _dt(15))
    assert is_open is False


def test_day_marked_is_closed_true_blocks_even_with_times_set():
    settings_row = {"status": "OPEN"}
    hours_row = {"open_time": "12:00", "close_time": "22:00", "is_closed": True}
    is_open, _ = availability.resolve_business_open(settings_row, hours_row, _dt(15))
    assert is_open is False


def test_overnight_hours_wrap_past_midnight():
    hours_row = {"open_time": "18:00", "close_time": "02:00", "is_closed": False}
    assert availability.is_within_operating_hours(hours_row, _dt(23)) is True
    assert availability.is_within_operating_hours(hours_row, _dt(1)) is True
    assert availability.is_within_operating_hours(hours_row, _dt(10)) is False


def test_filter_available_drops_sold_out_items():
    # "Sold-out item" test scenario: an unavailable item must never reach the customer-facing list.
    items = [
        {"name": "Chicken", "available": True},
        {"name": "Shrimp", "available": False},
        {"name": "Beef", "available": True},
    ]
    result = availability.filter_available(items)
    assert [i["name"] for i in result] == ["Chicken", "Beef"]
