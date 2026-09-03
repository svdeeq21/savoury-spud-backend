from app.utils.whatsapp_format import to_whatsapp_markdown


def test_double_asterisk_bold_converts_to_single():
    assert to_whatsapp_markdown("**ORDER SUMMARY**") == "*ORDER SUMMARY*"


def test_dunder_bold_converts_to_single_asterisk():
    assert to_whatsapp_markdown("__Total__: 9000") == "*Total*: 9000"


def test_markdown_header_becomes_bold_line():
    assert to_whatsapp_markdown("### Build Your Box") == "*Build Your Box*"


def test_markdown_link_becomes_plain_text_and_url():
    result = to_whatsapp_markdown("[Pay now](https://checkout.paystack.com/abc123)")
    assert result == "Pay now: https://checkout.paystack.com/abc123"


def test_already_correct_single_asterisk_is_left_alone():
    assert to_whatsapp_markdown("*Total: ₦9,800*") == "*Total: ₦9,800*"


def test_plain_text_unaffected():
    text = "Your total is ₦9,800.00. Tap here to pay:\nhttps://checkout.paystack.com/abc"
    assert to_whatsapp_markdown(text) == text


def test_multiple_bold_sections_in_one_message():
    text = "**Build Your Box**\n\n**Drinks (₦2,500 each)**\n• Chapman"
    result = to_whatsapp_markdown(text)
    assert result == "*Build Your Box*\n\n*Drinks (₦2,500 each)*\n• Chapman"


def test_empty_string_returns_empty():
    assert to_whatsapp_markdown("") == ""


def test_none_returns_none():
    assert to_whatsapp_markdown(None) is None
