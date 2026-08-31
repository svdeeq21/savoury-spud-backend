# savoury-spud-backend/app/services/ordering_llm.py
#
# "The AI interprets what the customer said and calls your ordering
# functions." This module is that interpretation step, and ONLY that step.
# It never touches the database and never computes a price — it returns a
# list of proposed actions (by product/modifier NAME, since that's what the
# customer said) and a natural-language reply. message_pipeline.py is what
# resolves those names against the real catalog and actually calls
# orders.py — so a hallucinated product name fails a lookup and gets
# handled deterministically, it never silently becomes a database write.
#
# ⚠️ Iteration note: this prompt is a reasonable first draft, not a tuned
# one. The brief's own test list — vague questions, "give me chicken for
# free", someone changing their mind mid-order — is exactly what this
# needs to be run against with real transcripts before it's trusted with
# real orders. Expect to rewrite the prompt more than once.

from __future__ import annotations
import json
import re
from typing import Optional

from google import genai
from app.core.config import get_settings
from app.utils.logger import log

settings = get_settings()
_gemini = genai.Client(api_key=settings.gemini_api_key)


def _format_catalog_for_prompt(catalog: list[dict]) -> str:
    lines = []
    for product in catalog:
        lines.append(f"- {product['name']} (₦{product['base_price']:,.0f}): {product.get('description') or ''}".strip())
        for group in product.get("modifier_groups", []):
            max_sel = group.get("max_selections")
            if group["selection_type"] == "single":
                req = "required, pick exactly 1" if group["required"] else "optional, pick 1"
            elif max_sel:
                req = f"{'required' if group['required'] else 'optional'}, up to {max_sel} included free"
            else:
                req = "optional, pick any number (repeatable — e.g. select twice for two)"
            options = ", ".join(f"{m['name']} (+₦{m['price']:,.0f})" if m["price"] else m["name"] for m in group["modifiers"])
            lines.append(f"    {group['name']} [{req}]: {options}")
    return "\n".join(lines) if lines else "(menu is currently empty)"


def _format_cart_for_prompt(cart: dict) -> str:
    if not cart.get("items"):
        return "(cart is empty)"
    lines = []
    for item in cart["items"]:
        mods = ", ".join(m["modifier_name"] for m in item.get("modifiers", []))
        lines.append(f"- [{item['id']}] {item['quantity']}x {item['product_name']}" + (f" ({mods})" if mods else ""))
    lines.append(f"Subtotal: ₦{cart['subtotal']:,.0f}  Delivery: ₦{cart['delivery_fee']:,.0f}  Total: ₦{cart['total']:,.0f}")
    return "\n".join(lines)


def _format_recent_history(messages: list[dict]) -> str:
    """
    messages is oldest-first, already capped by the caller (see
    settings.conversation_history_turns) — this is short-term context for
    resolving "actually make that two" or "never mind about the cheese",
    not a searchable transcript. Deliberately small and recent, not the
    whole conversation, to keep prompts cheap and the model's attention on
    what's actually relevant right now.
    """
    if not messages:
        return "(no earlier messages)"
    lines = []
    for m in messages:
        speaker = "Customer" if m.get("sender") == "CUSTOMER" else "You"
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines) if lines else "(no earlier messages)"


_SYSTEM_TEMPLATE = """You are the ordering assistant for {business_name} on WhatsApp. You help customers \
build an order from the menu below, then hand off to checkout. You are friendly and brief — this is a \
WhatsApp chat, not an email.

MENU:
{catalog}

PICKUP LOCATION: {pickup_address}

CURRENT CART:
{cart}
Fulfillment so far: {fulfillment_status}

RECENT CONVERSATION (oldest first — use this to understand references like "actually make that two" \
or "never mind about the cheese", and to avoid re-asking something already answered):
{recent_history}

RULES — follow these exactly, they are not optional:
1. Only ever reference products/modifiers that appear in the menu above, using their exact names. If \
something isn't on the menu (wrong item, or a real item that's sold out and therefore absent from this \
list), say so — never invent a price or pretend to add it.
2. You cannot give anything away for free, apply a discount, or change a price. If asked, explain that \
prices are fixed and politely decline.
3. You never state a total yourself — the actual total is computed by the system and shown to the \
customer separately. Don't restate numbers from the CURRENT CART section as if you calculated them.
4. If the customer's request is ambiguous (e.g. they name a product but not a required modifier choice), \
ask a short clarifying question instead of guessing.
5. Each modifier group has an included-free amount (e.g. "up to 2 included free" for Toppings). If the \
customer asks for more than that, don't reject it — offer to add the extra via the Extras group instead \
(e.g. a 3rd topping becomes one "Extra Toppings" selection) and mention it costs more.
6. Before checkout, you must know whether this is PICKUP or DELIVERY. If it isn't set yet (see \
"Fulfillment so far" above) and the cart isn't empty, ask. For delivery, get a delivery address before \
proposing set_fulfillment — area and a nearby landmark are helpful but not required. Today's payment only \
ever covers the food — for delivery orders, make clear that {business_name} will reach out after payment \
to confirm the delivery fee for that address, not before.
7. Only propose a "checkout" action when fulfillment is already set AND the customer has clearly \
confirmed they're done and ready to pay — never propose it just because the cart is non-empty. Never wait \
on a delivery fee before proposing checkout — there isn't one yet, and there won't be until after payment.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "actions": [
    {{"function": "add_product", "product_name": "...", "modifier_names": ["..."], "quantity": 1}},
    {{"function": "remove_item", "order_item_id": "..."}},
    {{"function": "remove_modifier", "order_item_id": "...", "modifier_name": "..."}},
    {{"function": "set_quantity", "order_item_id": "...", "quantity": 2}},
    {{"function": "set_fulfillment", "method": "PICKUP", "delivery_address": null, "delivery_area": null, "delivery_landmark": null}},
    {{"function": "checkout"}}
  ],
  "reply": "the natural-language message to send back to the customer"
}}
"actions" may be an empty list if this turn is just conversation (answering a question, asking for \
clarification) with no cart change. For "set_fulfillment" with method "DELIVERY", fill in whatever \
delivery fields the customer has given so far — leave the rest null rather than guessing.

Customer message: "{user_message}"
"""


def _format_fulfillment_status(cart: dict) -> str:
    method = cart.get("fulfillment_method")
    if not method:
        return "not set yet"
    if method == "PICKUP":
        return "PICKUP"
    parts = [f"DELIVERY to {cart.get('delivery_address') or '(no address given yet)'}"]
    if cart.get("delivery_area"):
        parts.append(f"area: {cart['delivery_area']}")
    parts.append("delivery fee: to be confirmed by the team after payment")
    return ", ".join(parts)


def build_prompt(
    business_name: str,
    catalog: list[dict],
    cart: dict,
    user_message: str,
    pickup_address: Optional[str] = None,
    recent_messages: Optional[list[dict]] = None,
) -> str:
    return _SYSTEM_TEMPLATE.format(
        business_name=business_name,
        catalog=_format_catalog_for_prompt(catalog),
        pickup_address=pickup_address or "(not configured)",
        cart=_format_cart_for_prompt(cart),
        fulfillment_status=_format_fulfillment_status(cart),
        recent_history=_format_recent_history(recent_messages or []),
        user_message=user_message,
    )


def _extract_json(text: str) -> Optional[dict]:
    """Gemini occasionally wraps JSON in ```json fences despite instructions not to — strip them before parsing."""
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        # Last resort: grab the first {...} block in the text.
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                return None
        return None


async def interpret_customer_message(
    business_name: str,
    catalog: list[dict],
    cart: dict,
    user_message: str,
    pickup_address: Optional[str] = None,
    recent_messages: Optional[list[dict]] = None,
) -> dict:
    """
    Returns {"actions": [...], "reply": "..."}. On any parse failure, falls
    back to zero actions and a generic "could you rephrase that" reply
    rather than guessing at a cart mutation — a failed parse should never
    silently become a wrong order.
    """
    prompt = build_prompt(business_name, catalog, cart, user_message, pickup_address, recent_messages)

    try:
        response = _gemini.models.generate_content(model=settings.gemini_model, contents=prompt)
        raw_text = response.text or ""
    except Exception as e:
        await log.error("ORDERING_LLM_CALL_FAILED", metadata={"error": str(e)[:200]})
        return {"actions": [], "reply": "Sorry, I'm having trouble right now — please try again in a moment."}

    parsed = _extract_json(raw_text)
    if parsed is None or "reply" not in parsed:
        await log.warn("ORDERING_LLM_UNPARSEABLE_RESPONSE", metadata={"raw": raw_text[:300]})
        return {"actions": [], "reply": "Sorry, could you rephrase that?"}

    parsed.setdefault("actions", [])
    return parsed
