from app.services.ordering_llm import _format_recent_history, build_prompt


def test_format_recent_history_empty_list():
    assert _format_recent_history([]) == "(no earlier messages)"


def test_format_recent_history_labels_each_side():
    messages = [
        {"sender": "CUSTOMER", "content": "I want a large box"},
        {"sender": "BOT", "content": "Sure — what protein would you like?"},
        {"sender": "CUSTOMER", "content": "Crispy chicken"},
    ]
    result = _format_recent_history(messages)
    assert "Customer: I want a large box" in result
    assert "You: Sure — what protein would you like?" in result
    assert "Customer: Crispy chicken" in result
    # Order preserved (oldest first, as passed in)
    assert result.index("large box") < result.index("protein") < result.index("Crispy chicken")


def test_format_recent_history_skips_blank_content():
    messages = [{"sender": "CUSTOMER", "content": ""}, {"sender": "BOT", "content": "Hello!"}]
    result = _format_recent_history(messages)
    assert result == "You: Hello!"


def test_build_prompt_includes_recent_history_section():
    catalog = [{"name": "Chapman", "base_price": 2500, "description": None, "modifier_groups": []}]
    cart = {"items": [], "subtotal": 0, "delivery_fee": 0, "total": 0, "fulfillment_method": None}
    history = [{"sender": "CUSTOMER", "content": "Do you have Chapman?"}, {"sender": "BOT", "content": "Yes, ₦2500."}]

    prompt = build_prompt("Savoury Spud", catalog, cart, "I'll take one", recent_messages=history)

    assert "RECENT CONVERSATION" in prompt
    assert "Customer: Do you have Chapman?" in prompt
    assert "You: Yes, ₦2500." in prompt
    assert 'Customer message: "I\'ll take one"' in prompt


def test_build_prompt_handles_no_history_gracefully():
    catalog = [{"name": "Chapman", "base_price": 2500, "description": None, "modifier_groups": []}]
    cart = {"items": [], "subtotal": 0, "delivery_fee": 0, "total": 0, "fulfillment_method": None}

    prompt = build_prompt("Savoury Spud", catalog, cart, "Hi")
    assert "(no earlier messages)" in prompt
