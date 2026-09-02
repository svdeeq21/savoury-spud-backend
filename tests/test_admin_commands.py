from app.services.admin_commands import parse_admin_command


def test_parses_pause_orders():
    cmd = parse_admin_command("pause orders")
    assert cmd.type == "pause_orders"
    assert cmd.reason is None


def test_parses_pause_orders_with_reason():
    cmd = parse_admin_command("pause orders, busy today")
    assert cmd.type == "pause_orders"
    assert cmd.reason == "busy today"


def test_parses_bare_pause():
    assert parse_admin_command("pause").type == "pause_orders"


def test_parses_resume():
    assert parse_admin_command("resume").type == "resume_orders"
    assert parse_admin_command("resume orders").type == "resume_orders"
    assert parse_admin_command("open").type == "resume_orders"
    assert parse_admin_command("reopen").type == "resume_orders"


def test_parses_close_as_pause_with_reason():
    cmd = parse_admin_command("close")
    assert cmd.type == "pause_orders"
    assert cmd.reason == "closed"


def test_parses_status_report():
    assert parse_admin_command("status").type == "status_report"


def test_parses_sold_out():
    cmd = parse_admin_command("chicken sold out")
    assert cmd.type == "set_item_availability"
    assert cmd.item_name == "chicken"
    assert cmd.available is False


def test_parses_sold_out_alternate_phrasing():
    cmd = parse_admin_command("we're out of shrimp")
    assert cmd.type == "set_item_availability"
    assert cmd.item_name == "shrimp"
    assert cmd.available is False


def test_parses_item_available_again():
    cmd = parse_admin_command("chicken is available")
    assert cmd.type == "set_item_availability"
    assert cmd.item_name == "chicken"
    assert cmd.available is True


def test_parses_item_back_in_stock():
    cmd = parse_admin_command("shrimp back in stock")
    assert cmd.type == "set_item_availability"
    assert cmd.available is True


def test_parses_test_buttons():
    assert parse_admin_command("test buttons").type == "test_buttons"
    assert parse_admin_command("test button").type == "test_buttons"


def test_parses_test_list():
    assert parse_admin_command("test list").type == "test_list"
    assert parse_admin_command("test lists").type == "test_list"


def test_unrecognized_text_is_unknown():
    cmd = parse_admin_command("what's the weather like")
    assert cmd.type == "unknown"


def test_case_insensitive():
    assert parse_admin_command("PAUSE ORDERS").type == "pause_orders"
    assert parse_admin_command("Chicken SOLD OUT").item_name == "chicken"
