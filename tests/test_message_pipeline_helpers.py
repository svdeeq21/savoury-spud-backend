from app.services.message_pipeline import _is_duplicate_bot_message, _PENDING_PAYMENT_CANCEL_PATTERN


def test_duplicate_bot_message_detected():
    recent = [
        {"sender": "CUSTOMER", "content": "Hey"},
        {"sender": "BOT", "content": "We're outside our operating hours right now. Please check back later."},
    ]
    assert _is_duplicate_bot_message(recent, "We're outside our operating hours right now. Please check back later.") is True


def test_different_bot_message_is_not_a_duplicate():
    recent = [{"sender": "BOT", "content": "Something else entirely."}]
    assert _is_duplicate_bot_message(recent, "We're outside our operating hours right now. Please check back later.") is False


def test_last_message_from_customer_is_never_a_duplicate():
    """Only suppress a repeat if WE said it last — a customer message in between means it's worth saying again."""
    recent = [
        {"sender": "BOT", "content": "We're outside our operating hours right now. Please check back later."},
        {"sender": "CUSTOMER", "content": "still there?"},
    ]
    assert _is_duplicate_bot_message(recent, "We're outside our operating hours right now. Please check back later.") is False


def test_empty_history_is_never_a_duplicate():
    assert _is_duplicate_bot_message([], "anything") is False


def test_cancel_pattern_matches_common_phrasings():
    for phrase in ["cancel", "Cancel please", "let's start over", "never mind", "nevermind then", "forget it", "undo that"]:
        assert _PENDING_PAYMENT_CANCEL_PATTERN.search(phrase), f"expected to match: {phrase!r}"


def test_cancel_pattern_does_not_match_unrelated_text():
    for phrase in ["has my order been placed?", "when will it arrive", "okay", "thanks"]:
        assert not _PENDING_PAYMENT_CANCEL_PATTERN.search(phrase), f"expected NOT to match: {phrase!r}"
