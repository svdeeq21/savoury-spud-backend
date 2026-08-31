-- 0001_ordering_schema.sql
--
-- Savoury Spud ordering system — initial schema.
--
-- Design notes:
--   * Multi-tenant shaped (org_id everywhere) even though there's one
--     organization today, matching the "real Hooze organization configured
--     for food ordering" structure from the original brief. Costs nothing
--     now, saves a painful migration later if a second food business signs on.
--   * A "cart" is not a separate table. It's an order row with
--     status = 'CART'. Adding/removing items just edits that row's
--     order_items until checkout starts. One order model, one source of
--     truth, no separate ephemeral cart state to keep in sync with the
--     real thing.
--   * Money is stored as numeric(10,2) in Naira throughout. The only place
--     that ever converts to kobo (Paystack's subunit) is the Paystack
--     service, right before the API call — never in the schema or the
--     pricing engine.
--   * Every table that represents a decision (availability, payments,
--     admin actions) keeps enough history to answer "what happened and
--     when", not just "what's true right now".

create extension if not exists "pgcrypto";

-- ── Organizations ────────────────────────────────────────────────
create table organizations (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    slug       text not null unique,
    created_at timestamptz not null default now()
);

-- ── Catalog ───────────────────────────────────────────────────────
create table categories (
    id         uuid primary key default gen_random_uuid(),
    org_id     uuid not null references organizations(id) on delete cascade,
    name       text not null,
    sort_order int  not null default 0
);

create table products (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organizations(id) on delete cascade,
    category_id uuid references categories(id) on delete set null,
    name        text not null,
    description text,
    base_price  numeric(10,2) not null default 0 check (base_price >= 0),
    available   boolean not null default true,
    sort_order  int not null default 0,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index idx_products_org on products(org_id);

-- A modifier group is a question the customer answers: "pick a protein",
-- "add toppings", "add a drink". selection_type controls whether it's a
-- radio button (single) or checkboxes (multiple) in the WhatsApp flow.
create table modifier_groups (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references organizations(id) on delete cascade,
    name           text not null,
    selection_type text not null check (selection_type in ('single', 'multiple')),
    required       boolean not null default false,
    sort_order     int not null default 0
);

create table modifiers (
    id         uuid primary key default gen_random_uuid(),
    group_id   uuid not null references modifier_groups(id) on delete cascade,
    name       text not null,
    price      numeric(10,2) not null default 0 check (price >= 0),
    available  boolean not null default true,
    sort_order int not null default 0
);
create index idx_modifiers_group on modifiers(group_id);

-- Which modifier groups apply to which products (e.g. "Loaded Fries" gets
-- Protein + Toppings + Extras; "Pepsi" gets nothing).
create table product_modifier_groups (
    product_id        uuid not null references products(id) on delete cascade,
    modifier_group_id uuid not null references modifier_groups(id) on delete cascade,
    primary key (product_id, modifier_group_id)
);

-- ── Availability ──────────────────────────────────────────────────
-- Business-level status. One row per org. This is the emergency-override
-- layer ("PAUSE ORDERS") that sits above the operating-hours schedule.
create table availability_settings (
    org_id        uuid primary key references organizations(id) on delete cascade,
    status        text not null default 'OPEN' check (status in ('OPEN', 'CLOSED', 'PAUSED')),
    pause_reason  text,
    pause_message text,
    updated_at    timestamptz not null default now()
);

-- day_of_week: 0 = Monday ... 6 = Sunday (ISO convention). is_closed lets a
-- day be fully closed without needing a null open/close time.
create table operating_hours (
    id          uuid primary key default gen_random_uuid(),
    org_id      uuid not null references organizations(id) on delete cascade,
    day_of_week int  not null check (day_of_week between 0 and 6),
    open_time   time,
    close_time  time,
    is_closed   boolean not null default false,
    unique (org_id, day_of_week)
);

-- ── Customers ─────────────────────────────────────────────────────
create table customers (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references organizations(id) on delete cascade,
    phone_number   text not null,          -- normalized digits only, e.g. 2348012345678
    whatsapp_number text,
    name           text,
    created_at     timestamptz not null default now(),
    last_order_at  timestamptz,
    total_orders   int not null default 0,
    unique (org_id, phone_number)
);

-- ── Orders ────────────────────────────────────────────────────────
-- CART is a live, editable order the customer hasn't checked out yet.
-- PAYMENT_PENDING means a Paystack transaction was initialized but not yet
-- confirmed. Only a verified webhook (or a verify-transaction call) moves
-- an order to PAID — nothing else is allowed to.
create table orders (
    id               uuid primary key default gen_random_uuid(),
    org_id           uuid not null references organizations(id) on delete cascade,
    customer_id      uuid not null references customers(id) on delete cascade,
    status           text not null default 'CART' check (
        status in ('CART', 'PAYMENT_PENDING', 'PAID', 'PREPARING', 'READY', 'COMPLETED', 'CANCELLED', 'EXPIRED')
    ),
    subtotal         numeric(10,2) not null default 0,
    delivery_fee     numeric(10,2) not null default 0,
    total            numeric(10,2) not null default 0,
    delivery_address text,
    customer_notes   text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    paid_at          timestamptz
);
create index idx_orders_org_status on orders(org_id, status);
create index idx_orders_org_created on orders(org_id, created_at);
create index idx_orders_customer_open_cart on orders(customer_id) where status = 'CART';

-- Line items snapshot product name + price at time of order, so a later
-- price change or product deletion never rewrites a historical order.
create table order_items (
    id          uuid primary key default gen_random_uuid(),
    order_id    uuid not null references orders(id) on delete cascade,
    product_id  uuid references products(id) on delete set null,
    product_name text not null,
    base_price   numeric(10,2) not null,
    quantity     int not null default 1 check (quantity > 0),
    line_total   numeric(10,2) not null
);
create index idx_order_items_order on order_items(order_id);

create table order_item_modifiers (
    id            uuid primary key default gen_random_uuid(),
    order_item_id uuid not null references order_items(id) on delete cascade,
    modifier_id   uuid references modifiers(id) on delete set null,
    modifier_name text not null,
    price         numeric(10,2) not null
);
create index idx_order_item_modifiers_item on order_item_modifiers(order_item_id);

-- ── Payments ──────────────────────────────────────────────────────
create table payments (
    id                  uuid primary key default gen_random_uuid(),
    order_id            uuid not null references orders(id) on delete cascade,
    provider            text not null default 'paystack',
    reference           text not null unique,
    status              text not null default 'pending' check (status in ('pending', 'success', 'failed')),
    amount              numeric(10,2) not null,
    currency            text not null default 'NGN',
    raw_webhook_payload jsonb,
    created_at          timestamptz not null default now(),
    verified_at         timestamptz
);
create index idx_payments_order on payments(order_id);

-- Idempotency guard for inbound provider webhooks. Paystack (and Evolution
-- API) can and will redeliver the same event — this table is the thing that
-- makes "duplicate webhook must not create two orders" actually true,
-- rather than just a test scenario we hope passes.
create table webhook_events (
    id           uuid primary key default gen_random_uuid(),
    provider     text not null,
    event_id     text not null,
    payload      jsonb,
    processed_at timestamptz not null default now(),
    unique (provider, event_id)
);

-- ── Admin + audit ─────────────────────────────────────────────────
-- Every "pause orders" / "chicken sold out" command sent from the owner's
-- WhatsApp number gets logged here, so there's a record of who changed
-- what and when — independent of the general audit_logs firehose below.
create table admin_actions_log (
    id         uuid primary key default gen_random_uuid(),
    org_id     uuid not null references organizations(id) on delete cascade,
    actor_phone text not null,
    action      text not null,
    payload     jsonb,
    created_at  timestamptz not null default now()
);

-- Generic structured event log (mirrors the real-estate app's audit_logs,
-- but keyed by ref_type/ref_id instead of a hardcoded lead_id column so it
-- can point at an order, a customer, or nothing at all).
create table audit_logs (
    id         uuid primary key default gen_random_uuid(),
    event      text not null,
    severity   text not null,
    ref_type   text,
    ref_id     uuid,
    metadata   jsonb,
    created_at timestamptz not null default now()
);
create index idx_audit_logs_created on audit_logs(created_at);

-- Inbound/outbound WhatsApp transcript. wa_message_id gets a unique
-- constraint so a redelivered Evolution API webhook can't be processed
-- twice — the second line-of-defense the real-estate app's own comments
-- call out, on top of the distributed lock.
create table conversation_messages (
    id           uuid primary key default gen_random_uuid(),
    org_id       uuid not null references organizations(id) on delete cascade,
    customer_id  uuid references customers(id) on delete cascade,
    sender       text not null check (sender in ('CUSTOMER', 'BOT', 'ADMIN')),
    content      text,
    message_type text default 'text',
    wa_message_id text,
    created_at   timestamptz not null default now()
);
create unique index idx_conversation_messages_wa_id
    on conversation_messages(org_id, wa_message_id)
    where wa_message_id is not null;
create index idx_conversation_messages_customer on conversation_messages(customer_id, created_at);

-- ── Seed: one organization row ───────────────────────────────────
-- Real menu seeding (products/modifiers from the Google Form / her price
-- list) happens in 0002_seed_catalog.sql once the actual menu is finalized
-- — deliberately not guessed at here.
insert into organizations (name, slug) values ('Savoury Spud', 'savoury-spud');

insert into availability_settings (org_id, status)
select id, 'OPEN' from organizations where slug = 'savoury-spud';

insert into operating_hours (org_id, day_of_week, open_time, close_time, is_closed)
select o.id, d, '12:00', '22:00', false
from organizations o, generate_series(0, 6) as d
where o.slug = 'savoury-spud';
