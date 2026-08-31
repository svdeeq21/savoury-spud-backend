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
from app.services import catalog, availability_store
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

    await log.warn("ADMIN_COMMAND_NOT_UNDERSTOOD", ref_type="org", ref_id=org_id, metadata={"text": command.raw_text})
    return (
        "I didn't understand that as a command. Try: \"pause orders\", \"resume\", "
        "\"chicken sold out\", \"chicken available\", or \"status\"."
    )
