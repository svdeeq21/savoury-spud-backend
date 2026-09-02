# Savoury Spud — Ordering Backend

WhatsApp-only ordering + admin dashboard API. No customer website. Forked
from the patterns already proven in the real-estate backend (`pd-fixed`) —
async Supabase client, X-Admin-Key dashboard auth, Evolution API WhatsApp
integration, distributed lock/cache — rebuilt around orders instead of leads.

Isolated on purpose: separate repo, separate Supabase project, nothing here
imports from or writes to the real-estate codebase. Extract reusable pieces
into Hooze proper only once this has survived contact with real orders.

## Native WhatsApp interactive UI (buttons + lists)

Added on top of the existing text-only flow, modelled on a competitor's
"View Plans" list-message UX: customers now get tappable menus instead of
having to type everything, without changing anything about how the LLM or
the cart/order engine work underneath.

- **`app/services/whatsapp.py`** — `send_list()` and `send_buttons()`, on
  top of Evolution API's `POST /message/sendList` and `POST
  /message/sendButtons`. Both take a plain-text `body` that's shown
  alongside the interactive part, and both **fall back to sending that
  body as an ordinary text message** if the interactive call throws —
  see the big comment at the top of that file for why this fallback
  isn't optional. In short: interactive buttons/lists are an official
  WhatsApp Business Platform (Cloud API) feature that a Baileys-based
  Evolution instance (a normal linked-device connection, not a
  Business-API-registered number) is only ever unofficially
  reproducing — it works, but it's had real regressions between
  Evolution versions and there are open reports of `sendButtons`
  returning success while nothing renders on the recipient's phone.
  **Test this against your actual instance before trusting it for
  paying customers** — if it turns out to be flaky, everything still
  works exactly as it did before, just as plain text.
- **`app/routers/webhook.py`** — parses a tap on a list row or reply
  button (`listResponseMessage` / `buttonsResponseMessage` /
  `templateButtonReplyMessage`) and feeds it into the same pipeline a
  typed message goes through. For ordinary menu/modifier taps, the
  button's `id` is just the option's own name — a tap arrives
  indistinguishable from the customer having typed "Loaded Fries" or
  "Large", so `ordering_llm.py` and `orders.py` needed **zero changes**
  and all 52 original tests still pass untouched.
- **`app/services/message_pipeline.py`**:
  - "menu" (typed or tapped, from a "View Menu" button on the welcome
    message) sends a native WhatsApp list built from the real catalog,
    grouped into sections by category.
  - When a draft item is still missing a **single-select** required
    group (Size, Base, Protein) — not a multi-select one (Toppings,
    Sauces, Extras, which can't be represented as one tappable choice) —
    the next message is buttons (≤3 options) or a list (4–10 options)
    for that group, instead of plain text. The full "everything still
    missing" text is preserved as the message body either way, so
    nothing is lost if only the buttons render.
- **Post-order feedback** (`migrations/0005_order_feedback.sql`,
  `orders.save_feedback_rating/save_feedback_issue`,
  `notifications.notify_poor_feedback`) — the moment a merchant marks an
  order COMPLETED (`dashboard.py`'s status endpoint), the customer gets a
  ★★★★★ / ★★★ / ★ buttons prompt. 5 stars invites a Google review
  (`GOOGLE_REVIEW_URL` in `.env`, optional); anything lower asks what went
  wrong (a 6-option list: food quality, late delivery, missing item, wrong
  order, customer service, other) instead of pushing a public review, and
  alerts the admin number(s) — once as soon as the low rating comes in,
  once more with the reason if the customer answers.

**Not yet done, and worth knowing about before this is "finished":**
- No tests were written for any of the above — the existing 70 tests
  exercise pure logic against a fake Supabase client; the interactive
  send/parse code is thin I/O glue that's more useful to test against a
  real Evolution instance than to fake convincingly.
- Rows/sections in the menu list are capped at WhatsApp's 10-row limit —
  fine for the current ~8-item menu, but flagged in code for whenever it
  grows.
- The "View Menu" button on the welcome message and the interactive
  prompts don't yet have a way to be turned off per-organization if a
  future multi-tenant customer's Evolution instance turns out not to
  support them — right now it's all-or-nothing across the deployment.

## Fixes from a follow-up review

The draft-item fix above guarantees the *content* is correct whenever the
system tells a customer what's missing — but it only guaranteed that
message was accurate, not that it appeared immediately. A customer saying
"I want a box" with zero other details could still get a hand-written LLM
reply that skipped straight to asking about size without laying out the
whole picture, since nothing forced the LLM to trigger the deterministic
path right away.

Closed by making it a hard rule rather than a judgment call: the LLM is
now instructed to call `add_product` the moment a product is named — even
with an empty modifier list — rather than trying to describe the choices
itself. `update_draft_item()` called with nothing selected yet now returns
every required group at once, and `_format_incomplete_draft_message()`
frames that first response as a menu walkthrough ("Let's build your Build
Your Box! Here's what you'll need to choose: ...") rather than the
slightly odd "Got it — nothing yet..." wording a generic template would
produce. The LLM's own hand-written reply is explicitly told it won't be
the one shown to the customer in this case, so it no longer needs to (and
shouldn't try to) enumerate the menu on its own.

## Fixes from the second real test run

A live transcript showed the real failure mode clearly: a customer
answering one question at a time ("regular" → "plantain, shawarma
chicken" → "bbq sauce" → "cheese sauce and mexican salsa") kept hitting
the same generic `"Toppings" is required` error and, at one point, was
asked to re-supply size/base/protein it had already given several turns
earlier.

The root cause: `add_product` was all-or-nothing. If any required
modifier group was missing, the *entire* attempt was discarded — nothing
was saved — and the LLM had to reconstruct every prior answer from raw
chat history on the next turn, which it did not reliably do. This only
worked once the customer gave up and restated the whole order in one
message.

**Fixed with a persisted draft item** (`orders.update_draft_item()`,
`migrations/0004_draft_cart_item.sql`): every partial answer is merged
into `orders.draft_item` immediately — single-select groups (Size, Base,
Protein) replace the prior answer, multi-select groups (Toppings, Sauces,
Extras) accumulate across turns. Nothing is ever thrown away. The item
only becomes a real `order_item` once every required group is satisfied.
When something's still missing, the customer gets a deterministic message
listing **every** remaining group and its **actual options** in one shot
— generated from the real catalog, not left to the LLM to recall or
improvise, which fixes the "it never tells me what my options are"
complaint at the same time. The draft is also now shown directly in the
LLM's prompt (`IN PROGRESS: ...`), so it doesn't have to infer
already-chosen options purely from re-reading the conversation transcript.

## Fixes from the first real test run

Live testing on Render surfaced three real issues, all fixed here:

- **Checkout was failing on every order.** Paystack rejected the
  placeholder email built from `.local` — a reserved special-use TLD
  (RFC 6762, for mDNS), not a normal domain, so their validator refused it
  every time. Fixed in `app/services/paystack.py`.
- **The bot's own replies were never being recorded**, only the
  customer's — so the ordering LLM had no memory of what it had just said
  or asked, only the current cart state. Every bot reply now goes through
  `_send_and_record()` in `message_pipeline.py` and gets written to
  `conversation_messages`, and the last `CONVERSATION_HISTORY_TURNS`
  messages (default 8) are now included in the prompt.
- **An abandoned cart never expired.** A customer returning days later to
  order something different would have silently resumed whatever was left
  in their old cart. `orders.get_or_create_open_cart()` now expires a cart
  that hasn't been touched in `CART_STALE_AFTER_HOURS` (default 4) and
  starts fresh — long enough to tolerate someone pausing mid-order, short
  enough to not resurrect a genuinely old one.
- Also added: a deterministic welcome message on a customer's very first
  contact ever (not left to the LLM to improvise), rather than silence.

## What's actually built (phases 1–5 of the plan, now with the real menu)

- **Schema** (`migrations/0001_ordering_schema.sql`) — organizations,
  catalog (products/modifiers with availability flags), availability +
  operating hours, orders/order_items (cart = an order row with
  `status='CART'`), payments, webhook idempotency, admin action log.
- **`migrations/0002_fulfillment_and_selection_limits.sql`** — added after
  seeing the real order form: `modifier_groups.max_selections` (the "2
  toppings included, extra costs more" rule), and pickup/delivery fields on
  `orders` (`fulfillment_method`, delivery address/area/landmark, time
  preference, and `delivery_fee_confirmed`).
- **`migrations/0003_seed_savoury_spud_catalog.sql`** — the real menu,
  transcribed from the live "Build Your Box" form: Build Your Box (Size,
  Base, Protein, Toppings, Sauces, Extras) plus three standalone drinks.
  Modeling notes are in the file itself.
- **Transaction spine** — WhatsApp message → LLM interprets it into
  cart actions (by name) → deterministic resolution against the real
  catalog → `orders.py` mutates the cart → Paystack checkout link →
  webhook (signature-verified + re-verified via the Verify Transaction
  API) → order flips to PAID exactly once, even if the webhook is
  redelivered.
- **Payment covers food only, always.** Checkout charges `subtotal` —
  never `subtotal + delivery_fee` — regardless of pickup or delivery.
  `orders.set_fulfillment_details()` still records pickup vs delivery (and
  the address, for delivery) before checkout, since that's needed to
  fulfil the order — but it's never a payment gate. For delivery orders,
  she confirms the fee with the customer herself *after* payment, and
  records it via `PATCH /dashboard/orders/{id}/delivery-fee` purely for
  her own tracking — it never triggers a second charge.
- **Selection validation** — every `add_product` call is checked against
  each modifier group's `required`/`max_selections` before it's allowed to
  write anything. Asking for 3 free toppings when only 2 are included
  raises a clear, customer-facing message steering them to Extras instead
  of silently overcharging or undercharging.
- **Availability rules** — OPEN/CLOSED/PAUSED + operating hours + per-item
  sold-out, gating the ordering flow *before* checkout starts. Controllable
  two ways: the dashboard, or texting the bot from an admin number
  (`"pause orders"`, `"chicken sold out"`, `"status"` — see
  `app/services/admin_commands.py`).
- **Dashboard API** (`app/routers/dashboard.py`) — orders (list with
  date-range filter, detail, status transitions, delivery-fee recording),
  availability, catalog CRUD, customers, metrics (`food_revenue` — what
  Paystack actually verified — reported separately from
  `delivery_fees_recorded`, which is informational since it's collected
  off-platform). All behind `X-Admin-Key`.
- **Tests** — 52 tests, all passing, no network/DB needed
  (`pytest -q`). Pure pricing math, availability logic, admin command
  parsing, modifier-selection validation (required groups, max_selections,
  repeatable Extras), the fulfillment/payment split (food charged
  immediately, delivery fee recorded only after payment), and the order
  lifecycle (including the duplicate-webhook and invalid-status-transition
  cases) against an in-memory fake Supabase client.

## What's deliberately NOT built yet

- **No dashboard frontend.** This is the API only. If the real-estate
  CRM's Next.js frontend exists as a separate repo, fork its shell rather
  than starting from zero — the endpoint shapes here mirror that backend's
  conventions closely enough that it should translate directly.
- **No automatic delivery pricing.** Delivery fee is confirmed manually per
  order (dashboard endpoint), matching her current process exactly. A
  zone → fee lookup table is a natural next step once there's enough
  delivery-area data to build one from, but guessing at it now would just
  be wrong.
- **Payment channel:** built on Paystack, not raw bank transfer, despite
  the form's current copy. Paystack supports bank transfer as a payment
  channel (dedicated virtual account) if keeping that exact customer
  experience matters — worth a quick conversation with her, but not a
  reason to rebuild the (tested, working) checkout flow around manual
  transfers.
- **The ordering conversation prompt is a first draft, not a tuned one**
  (`app/services/ordering_llm.py`). It's fully wired to Gemini, now knows
  the real menu's included-vs-extra rules and collects pickup/delivery
  before checkout, and will work — but the brief's own test list — vague
  questions, "give me chicken for free", someone changing their mind
  mid-order — is exactly what needs to be run against it with real
  transcripts before it's trusted. Budget real iteration time here; this is
  the fuzziest part of the system on purpose (everything downstream of it
  is deterministic).
- **No background scheduler wired up** for
  `orders.expire_stale_pending_orders()` (the "customer abandons checkout"
  cleanup) — the function exists and is tested, it just isn't called on a
  cron yet. Same for the real-estate app's pattern of an
  `internal_cron_secret`-protected `/internal/*` endpoint a scheduler can hit.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in real values — see below
pytest -q              # 70 passed, no external services required
uvicorn main:app --reload
```

Run the migrations in order against a fresh Supabase project:
`0001_ordering_schema.sql` → `0002_fulfillment_and_selection_limits.sql` →
`0003_seed_savoury_spud_catalog.sql` → `0004_draft_cart_item.sql`.

**Already live?** Only `0004_draft_cart_item.sql` needs to be run against
the existing database — it's a single additive column
(`alter table orders add column draft_item jsonb`), safe to run without
touching anything already in there.

## What you need to supply before this can take a real order

| Value | Where to get it |
|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | A **new** Supabase project — do not reuse the real-estate one. Run all three `migrations/*.sql` files against it, in order. |
| `GEMINI_API_KEY` | Same as the real-estate app, or a fresh key. |
| `EVOLUTION_API_URL` / `EVOLUTION_API_KEY` | A WhatsApp instance with its webhook pointed at `POST /webhook/whatsapp` on wherever this gets deployed. |
| `PAYSTACK_SECRET_KEY` | Use `sk_test_...` until real orders are actually being taken. Set the webhook URL in the Paystack dashboard to `POST /webhook/paystack`. |
| `ADMIN_API_KEY` | Any random secret — `python -c "import secrets; print(secrets.token_urlsafe(32))"`. This is what the dashboard sends as `X-Admin-Key`. |
| `ADMIN_WHATSAPP_NUMBERS` | Her number (digits only), comma-separated if more than one person should get admin commands + new-order alerts. |
| `BUSINESS_UTC_OFFSET_HOURS` | Defaults to `1.0` (WAT/Lagos). Only change if the business isn't in that timezone. |

## Deploying

Same shape as the real-estate app — Render/Koyeb for the API,
`UPSTASH_REDIS_REST_URL`/`TOKEN` once this ever runs on more than one
worker (optional before that; see `app/utils/distributed_state.py`).

## Testing against the brief's scenario list

Every scenario from the original brainstorm maps to something here:

| Scenario | Covered by |
|---|---|
| Normal order, various combinations | `ordering_llm.py` + `orders.add_product` |
| Customer changes their mind ("remove the cheese") | `orders.remove_modifier` (tested) |
| Vague question ("what comes with loaded fries?") | `ordering_llm` returns 0 actions + a reply |
| Invalid request ("give me chicken for free") | Rule #2 in the LLM prompt; deterministic backend never applies discounts regardless |
| Sold-out item | `availability.filter_available` — never shown to the LLM in the first place |
| Business closed / manual pause | `availability.resolve_business_open` (tested), blocks before any cart action |
| Payment failure | Order stays `PAYMENT_PENDING`; no webhook, no state change |
| Payment success | `handle_confirmed_payment` → `mark_paid` |
| Duplicate webhook | `mark_paid` idempotency (tested) |
| Customer abandons checkout | Stays `PAYMENT_PENDING` until `expire_stale_pending_orders` flips it to `EXPIRED` |
| Merchant changes order status | `orders.update_status`, enforces valid transitions only (tested) |
| "Show me everything ordered Aug 20–30" | `GET /dashboard/orders?date_from=...&date_to=...` |
| Customer asks for 3 free toppings (only 2 included) | `orders._validate_modifier_selection` (tested) — rejects, suggests Extras |
| Delivery order pays, then vendor confirms fee | `message_pipeline._start_checkout` charges `subtotal` only (tested); `PATCH /dashboard/orders/{id}/delivery-fee` records it after `mark_paid` |

What isn't covered by an automated test: actually running any of this
against live Evolution API / Paystack / Gemini calls — that needs real
credentials and is the next real milestone, not something fakeable in a
unit test.
