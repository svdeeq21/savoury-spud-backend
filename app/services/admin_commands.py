# savoury-spud-backend/app/services/admin_commands.py
#
# Everything the owner can do by texting the bot instead of opening the
# dashboard. Deliberately NOT run through the LLM — "pause orders" flipping
# the wrong flag because a language model guessed wrong is a much worse
# failure mode here than on the customer side, where a misread just means
# asking a clarifying question. Plain keyword/regex matching, and if it
# doesn't match confidently, it reports back "unknown" and tells her to be
# more specific rather than guessing.

from __future__ import annotations
import re
from typing import Optional
from uuid import UUID

from app.models.schemas import AdminCommand
from app.services import catalog, availability_store, whatsapp
from app.utils.logger import log

_PAUSE_PATTERNS = [
    r"^pause( orders)?\b",
    r"^stop( taking)? orders\b",
]
_RESUME_PATTERNS = [
    r"^(resume|unpause)( orders)?\b",
    r"^(re)?open\b",
]
_CLOSE_PATTERNS = [
    r"^close\b",
    r"^closed\b",
]
_STATUS_PATTERNS = [
    r"^status\b",
    r"^how (are|is) (we|it|things)\b",
]
_TEST_BUTTONS_PATTERNS = [r"^test buttons?\b"]
_TEST_LIST_PATTERNS = [r"^test lists?\b"]
# "chicken sold out", "we're out of shrimp", "no more chicken", "shrimp is finished"
_SOLD_OUT_PATTERNS = [
    r"^(?P<item>.+?)\s+(is\s+)?sold\s*out$",
    r"^(?P<item>.+?)\s+(is\s+)?(finished|done|out)$",
    r"^(no more|out of|we'?re out of)\s+(?P<item>.+)$",
]
# "chicken available", "chicken is back", "shrimp back in stock"
_AVAILABLE_PATTERNS = [
    r"^(?P<item>.+?)\s+(is\s+)?(available|back|back in stock)$",
]


def _match_any(patterns: list[str], text: str) -> Optional[re.Match]:
    for p in patterns:
        m = re.match(p, text, re.IGNORECASE)
        if m:
            return m
    return None


def parse_admin_command(raw_text: str) -> AdminCommand:
    text = raw_text.strip()
    lowered = text.lower()

    if _match_any(_PAUSE_PATTERNS, lowered):
        # "pause orders, busy today" / "pause orders: busy today"
        reason = None
        for sep in (",", ":", "-"):
            if sep in text:
                reason = text.split(sep, 1)[1].strip()
                break
        return AdminCommand(type="pause_orders", reason=reason, raw_text=raw_text)

    if _match_any(_RESUME_PATTERNS, lowered):
        return AdminCommand(type="resume_orders", raw_text=raw_text)

    if _match_any(_CLOSE_PATTERNS, lowered):
        return AdminCommand(type="pause_orders", reason="closed", raw_text=raw_text)

    if _match_any(_STATUS_PATTERNS, lowered):
        return AdminCommand(type="status_report", raw_text=raw_text)

    if _match_any(_TEST_BUTTONS_PATTERNS, lowered):
        return AdminCommand(type="test_buttons", raw_text=raw_text)

    if _match_any(_TEST_LIST_PATTERNS, lowered):
        return AdminCommand(type="test_list", raw_text=raw_text)

    m = _match_any(_SOLD_OUT_PATTERNS, lowered)
    if m:
        return AdminCommand(type="set_item_availability", item_name=m.group("item").strip(), available=False, raw_text=raw_text)

    m = _match_any(_AVAILABLE_PATTERNS, lowered)
    if m:
        return AdminCommand(type="set_item_availability", item_name=m.group("item").strip(), available=True, raw_text=raw_text)

    return AdminCommand(type="unknown", raw_text=raw_text)


async def apply_admin_command(org_id: UUID, actor_phone: str, command: AdminCommand) -> str:
    """
    Executes the parsed command and returns the confirmation text to send
    back to the owner over WhatsApp. Every successful action is written to
    admin_actions_log for an audit trail independent of the general
    audit_logs firehose.
    """
    from app.core.supabase import get_supabase
    db = await get_supabase()

    async def _record(action: str, payload: dict) -> None:
        await db.table("admin_actions_log").insert({
            "org_id": str(org_id),
            "actor_phone": actor_phone,
            "action": action,
            "payload": payload,
        }).execute()

    if command.type == "pause_orders":
        message = (
            f"We're currently unavailable and aren't accepting orders at the moment"
            + (f" ({command.reason})." if command.reason else ".")
        )
        await availability_store.set_status(org_id, "PAUSED", pause_reason=command.reason, pause_message=message)
        await _record("pause_orders", {"reason": command.reason})
        return f"Orders paused. Customers will see: \"{message}\"\nText \"resume\" when you're ready to take orders again."

    if command.type == "resume_orders":
        await availability_store.set_status(org_id, "OPEN")
        await _record("resume_orders", {})
        return "Orders resumed — you're back to your normal operating hours."

    if command.type == "status_report":
        settings_row = await availability_store.get_availability_settings(org_id)
        return f"Current status: {settings_row.get('status', 'OPEN')}" + (
            f" — {settings_row.get('pause_message')}" if settings_row.get("status") == "PAUSED" else ""
        )

    if command.type == "set_item_availability":
        item = await catalog.find_product_by_name(org_id, command.item_name)
        kind = "product"
        if item is None:
            item = await catalog.find_modifier_by_name(org_id, command.item_name)
            kind = "modifier"

        if item is None:
            return f"Couldn't find anything called \"{command.item_name}\" on the menu — check the spelling and try again."

        if kind == "product":
            await catalog.set_product_availability(item["id"], command.available, org_id)
        else:
            await catalog.set_modifier_availability(item["id"], command.available, org_id)

        await _record("set_item_availability", {"item": item["name"], "available": command.available})
        state = "available again" if command.available else "marked as sold out"
        return f"{item['name']} is now {state}."

    if command.type == "test_buttons":
        try:
            await whatsapp.send_buttons(
                actor_phone,
                body_text="This is a test button message — did it render as tappable buttons, or as plain text?",
                buttons=[
                    {"id": "test_a", "title": "Option A"},
                    {"id": "test_b", "title": "Option B"},
                    {"id": "test_c", "title": "Option C"},
                ],
                footer="Savoury Spud — interactive message test",
            )
            await _record("test_buttons_sent", {})
            return "Sent — look above ⬆️ for a button message. If it looks like plain text instead, buttons aren't rendering on this instance."
        except Exception as e:
            await log.error("TEST_BUTTONS_FAILED", ref_type="org", ref_id=org_id, metadata={"error": str(e)[:200]})
            return f"Sending failed outright: {str(e)[:200]}"

    if command.type == "test_list":
        try:
            products = await catalog.get_full_catalog(org_id)
            size_group = next(
                (g for p in products for g in p.get("modifier_groups", []) if g["name"] == "Size"),
                None,
            )
            rows = (
                [{"id": m["id"], "title": m["name"], "description": f"₦{m['price']:,.0f}"} for m in size_group["modifiers"]]
                if size_group else
                [{"id": "test_1", "title": "Test Option 1"}, {"id": "test_2", "title": "Test Option 2"}]
            )
            await whatsapp.send_list(
                actor_phone,
                title="Test List",
                body_text="This is a test list message — did it render as a tappable list, or as plain text?",
                button_text="View Options",
                sections=[{"title": "Choose Size", "rows": rows}],
                footer="Savoury Spud — interactive message test",
            )
            await _record("test_list_sent", {})
            return "Sent — look above ⬆️ for a list message with a 'View Options' button. If it looks like plain text instead, lists aren't rendering on this instance."
        except Exception as e:
            await log.error("TEST_LIST_FAILED", ref_type="org", ref_id=org_id, metadata={"error": str(e)[:200]})
            return f"Sending failed outright: {str(e)[:200]}"

    await log.warn("ADMIN_COMMAND_NOT_UNDERSTOOD", ref_type="org", ref_id=org_id, metadata={"text": command.raw_text})
    return (
        "I didn't understand that as a command. Try: \"pause orders\", \"resume\", "
        "\"chicken sold out\", \"chicken available\", \"status\", \"test buttons\", or \"test list\"."
    )
