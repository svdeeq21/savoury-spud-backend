# Savoury Spud — Ordering Backend

WhatsApp-only ordering + admin dashboard API. No customer website. Forked
from the patterns already proven in the real-estate backend (`pd-fixed`) —
async Supabase client, X-Admin-Key dashboard auth, Evolution API WhatsApp
integration, distributed lock/cache — rebuilt around orders instead of leads.

Isolated on purpose: separate repo, separate Supabase project, nothing here
imports from or writes to the real-estate codebase. Extract reusable pieces
into Hooze proper only once this has survived contact with real orders.

## Confirmed working: the payment webhook

A real transcript shows `handle_confirmed_payment` firing automatically —
"Payment received — thank you!" sent with no manual reconciliation needed.
The `/dashboard/orders/{id}/verify-payment` endpoint from the earlier fix
remains as a safety net for any future webhook delivery failure, but the
root cause (missing webhook URL in the Paystack dashboard) is fixed.

## Conversational UX improvements (from an external review + real transcripts)

An external spec on conversational completeness was mostly about prompt
quality, not architecture — validated against real transcripts before
implementing anything:

- **"cancel it and create the sam order again"** was a genuine gap: the
  payment-pending gate correctly cancelled the old checkout but then made
  the customer retype their entire order from scratch. Fixed narrowly and
  deterministically — `orders.duplicate_order_items()` copies the
  cancelled order's items, modifiers, and fulfillment details into a fresh
  cart, triggered only by the specific "cancel + same/again" phrasing
  (`_REPEAT_ORDER_PATTERN` in `message_pipeline.py`). This is deliberately
  **not** a general "modify order while payment is pending" feature — the
  payment-pending gate stays exactly as strict as before for anything
  else; only this one unambiguous, code-driven case was added.
- **Prompt rules 9–11** (`ordering_llm.py`): every committed action must
  now include a concrete next step ("Want a drink with that, or ready to
  check out?"), not just an acknowledgment — a bare "Added the Chapman."
  no longer satisfies the rule. Ambiguous short confirmations ("yes") are
  only accepted when the preceding question had exactly one sensible
  reading. And the model is told explicitly to sound like a person who
  works there, not a formal system — no "your request has been
  processed" phrasing.
- Multi-intent extraction in one message, not repeating already-answered
  questions, and using the cart as context were **already working**
  correctly per the transcripts (verified, not assumed) — no changes
  needed there.

## Fixes from a real transcript review

An outside review of live conversation logs (mostly accurate, worth taking
seriously) surfaced real bugs. Two were traced to their actual root cause
rather than just patched at the symptom:

- **"Adding that to your cart" followed by "Cannot check out an empty
  cart" hours later.** Traced with the actual timestamps: `set_fulfillment_details`
  never bumped `orders.updated_at`, so the cart-staleness clock
  (`CART_STALE_AFTER_HOURS`) kept counting from the last *pricing*
  mutation instead of the last real interaction. A customer who took a
  couple of hours to answer "pickup or delivery?" could have their cart
  silently expire mid-conversation. Fixed — `set_fulfillment_details` now
  bumps `updated_at` like every other cart mutation.
- **"Okay" → "Enjoy your order!" with no payment having happened.** Worse
  than it looks: once checkout starts, the order leaves `CART` status and
  becomes invisible to `get_or_create_open_cart` — the next message
  silently spun up a brand-new *empty* cart, and the LLM, seeing nothing
  in it, freely improvised a close with zero actual awareness a payment
  was in flight. Fixed with a hard, LLM-free gate: `message_pipeline`
  checks for an in-flight `PAYMENT_PENDING` order *before* touching the
  ordering flow at all (`orders.get_pending_payment_order`). While one
  exists, the customer gets a deterministic reminder with the real payment
  link (now stored — `payments.authorization_url`, migration `0005`) or
  can cancel it explicitly; nothing about payment state is ever left to
  the LLM's judgment.
- **A silently-failed `add_product` could still say "Adding that to your
  cart now!"** If the LLM's product name didn't resolve, this used to log
  a warning and say nothing — leaving the LLM's optimistic reply
  unchallenged even though nothing was added. Now returns an honest
  override instead.
- **WhatsApp doesn't render standard Markdown** — `**bold**` just shows as
  literal asterisks. `app/utils/whatsapp_format.py` normalizes every
  outbound message at the universal send boundary in `whatsapp.py`
  (`**bold**` → `*bold*`, headers stripped to bold, markdown links
  flattened to plain URLs), and the prompt is told to use WhatsApp's own
  syntax directly rather than relying on cleanup alone.
- **The identical "outside operating hours" message got repeated
  verbatim on every single message** once triggered once. Now suppressed
  if the last thing sent was that exact line (`_is_duplicate_bot_message`).
- Two prompt rules added directly from the transcripts: never confirm
  with a bare "Got it!" — always restate what actually happened; never
  guess on ambiguity that could change what's charged or how something's
  fulfilled ("no delivery please" — ask, don't assume).

### What's genuinely good direction from that review, not built today

The reviewer's broader architecture — explicit conversation states, an
intent router (`ORDER_STATUS`, `COMPLAINT`, `HUMAN_SUPPORT`, etc. instead
of everything defaulting to "sell them a box"), a staff-driven order
lifecycle dashboard with automatic customer notifications, and a
regression suite built from real transcripts — is the right long-term
shape and worth building toward. It's not started here because each of
those is a genuine multi-day feature, not a bug fix, and the transaction-
correctness issues above were both more dangerous and more urgent. A
couple of the reviewer's specific diagnoses didn't quite match this
codebase once traced (e.g. "state isn't authoritative" was actually one
specific missing `updated_at` bump, and the restaurant-config-separated-
from-code point is already mostly true — organizations/products/modifiers
are schema-driven per org, not hardcoded) — worth knowing the difference
between "this exact thing is broken" and "this is good direction to grow
into" before committing real time to either.

## Interactive messages (buttons/lists) — added, not yet wired into ordering

Native WhatsApp buttons and list menus (`app/services/whatsapp.py`:
`send_buttons()`, `send_list()`) are a real UX upgrade for the single-select
fields (Size, Base, Protein) — tapping removes the entire class of typos
and invalid answers the draft-item fix above had to work around. **But
this is added as a standalone, testable capability, not wired into the
live ordering flow yet, on purpose.**

Why: Evolution API's button/list support is well-documented — by its own
GitHub issues, by third-party client libraries, and independently by other
unofficial WhatsApp providers — as unstable specifically on the Baileys
(WhatsApp Web) connection, which this instance almost certainly uses. One
client library's own docs: *"Interactive buttons and list messages are not
supported on the Baileys connector and are likely to be discontinued...
fully supported only on the Cloud API connector."* There's a closed
Evolution API bug where buttons/lists worked in v2.3.6 and broke entirely
in v2.3.7. Rendering can silently stop working on a WhatsApp app update,
entirely outside anyone's control.

**Test it first:** text `test buttons` or `test list` from an admin
WhatsApp number. If it renders as tappable UI, great — that's the signal
to move to phase 2 (wiring it into the real ordering conversation for
Size/Base/Protein, keeping free text as the fallback for Toppings/Sauces/
Extras, which don't map cleanly to WhatsApp's single-select-only native
UI anyway). If it renders as plain text instead, stay on the free-text
flow — which the draft-item fix already made considerably more robust —
until ready to move to the official Meta Cloud API.

On the receive side, `app/routers/webhook.py` already extracts a button/
list reply's display text and feeds it through the *exact same* pipeline
as typed text — so even without any further wiring, a tap already works
today exactly as well as typing the same words would.

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
pytest -q              # 112 passed, no external services required
uvicorn main:app --reload
```

Run the migrations in order against a fresh Supabase project:
`0001_ordering_schema.sql` → `0002_fulfillment_and_selection_limits.sql` →
`0003_seed_savoury_spud_catalog.sql` → `0004_draft_cart_item.sql` →
`0005_payment_link_and_cart_updated_at.sql`.

**Already live?** Run `0004_draft_cart_item.sql` and
`0005_payment_link_and_cart_updated_at.sql` against the existing
database — both are single additive columns, safe to run without
touching anything already there.

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
