from app.services.message_pipeline import _format_incomplete_draft_message


def test_starting_fresh_reads_as_a_menu_walkthrough():
    """The actual onboarding moment: first thing shown after 'I want a box', nothing chosen yet."""
    result = {
        "committed": False,
        "missing": [
            {"name": "Size", "modifiers": [{"name": "Regular"}, {"name": "Large"}]},
            {"name": "Protein", "modifiers": [{"name": "Crispy Chicken"}, {"name": "Shawarma Beef"}]},
        ],
        "selected_so_far": [],
    }
    message = _format_incomplete_draft_message("Build Your Box", result)
    assert message.startswith("Let's build your Build Your Box!")
    assert "Size — choose at least 1: Regular, Large" in message
    assert "Protein — choose at least 1: Crispy Chicken, Shawarma Beef" in message
    assert "Got it" not in message  # nothing to "got" yet — different framing entirely


def test_in_progress_shows_whats_already_chosen_plus_whats_left():
    result = {
        "committed": False,
        "missing": [{"name": "Sauces", "modifiers": [{"name": "BBQ Sauce"}, {"name": "Garlic Sauce"}]}],
        "selected_so_far": [{"name": "Regular"}, {"name": "Crispy Chicken"}, {"name": "Cheese Sauce"}],
    }
    message = _format_incomplete_draft_message("Build Your Box", result)
    assert message.startswith("Got it — Regular, Crispy Chicken, Cheese Sauce so far")
    assert "Sauces — choose at least 1: BBQ Sauce, Garlic Sauce" in message


def test_error_message_still_shows_whats_already_chosen():
    result = {"committed": False, "error": "Too many toppings.", "selected_so_far": [{"name": "Regular"}]}
    message = _format_incomplete_draft_message("Build Your Box", result)
    assert message == "Got it — Regular so far for your Build Your Box. Too many toppings."


def test_error_message_with_nothing_selected_yet():
    result = {"committed": False, "error": "Something went wrong.", "selected_so_far": []}
    message = _format_incomplete_draft_message("Build Your Box", result)
    assert message == "Got it — nothing yet so far for your Build Your Box. Something went wrong."
