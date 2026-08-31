# Savoury Spud — Ordering Backend

WhatsApp-only ordering + admin dashboard API. No customer website. Forked
from the patterns already proven in the real-estate backend (`pd-fixed`) —
async Supabase client, X-Admin-Key dashboard auth, Evolution API WhatsApp
integration, distributed lock/cache — rebuilt around orders instead of leads.

Isolated on purpose: separate repo, separate Supabase project, nothing here
imports from or writes to the real-estate codebase. Extract reusable pieces
into Hooze proper only once this has survived contact with real orders.

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
pytest -q              # 60 passed, no external services required
uvicorn main:app --reload
```

Run the migrations in order against a fresh Supabase project:
`0001_ordering_schema.sql` → `0002_fulfillment_and_selection_limits.sql` →
`0003_seed_savoury_spud_catalog.sql`.

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
