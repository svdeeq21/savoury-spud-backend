from app.routers.webhook import _extract_message_text


def test_extracts_plain_conversation_text():
    assert _extract_message_text({"conversation": "Hello there"}) == "Hello there"


def test_extracts_extended_text_message():
    assert _extract_message_text({"extendedTextMessage": {"text": "Extended hi"}}) == "Extended hi"


def test_extracts_button_reply_display_text():
    message = {"buttonsResponseMessage": {"selectedButtonId": "m-regular", "selectedDisplayText": "Regular"}}
    assert _extract_message_text(message) == "Regular"


def test_button_reply_falls_back_to_id_if_no_display_text():
    message = {"buttonsResponseMessage": {"selectedButtonId": "m-regular", "selectedDisplayText": ""}}
    assert _extract_message_text(message) == "m-regular"


def test_extracts_list_reply_title():
    message = {"listResponseMessage": {"title": "Large", "singleSelectReply": {"selectedRowId": "m-large"}}}
    assert _extract_message_text(message) == "Large"


def test_list_reply_falls_back_to_row_id_if_no_title():
    message = {"listResponseMessage": {"singleSelectReply": {"selectedRowId": "m-large"}}}
    assert _extract_message_text(message) == "m-large"


def test_extracts_template_button_reply():
    message = {"templateButtonReplyMessage": {"selectedId": "opt_1", "selectedDisplayText": "Yes please"}}
    assert _extract_message_text(message) == "Yes please"


def test_unknown_message_type_returns_empty_string():
    assert _extract_message_text({"someWeirdMessageType": {"foo": "bar"}}) == ""


def test_empty_message_returns_empty_string():
    assert _extract_message_text({}) == ""
