-- migrations/0005_order_feedback.sql
--
-- Post-completion feedback (the "★★★★★ / ★★★ / ★" prompt sent once an
-- order is marked COMPLETED from the dashboard — see
-- app/services/message_pipeline.py:send_feedback_prompt). A low rating
-- gets a deterministic follow-up asking what went wrong; that answer is
-- stored in issue_category and also forwarded to the admin numbers as a
-- normal WhatsApp alert (notifications.py), same as a new-order alert.
--
-- One row per order — additive, safe to run on a live database.

create table order_feedback (
    id             uuid primary key default gen_random_uuid(),
    org_id         uuid not null references organizations(id) on delete cascade,
    order_id       uuid not null references orders(id) on delete cascade,
    customer_id    uuid not null references customers(id) on delete cascade,
    rating         int  not null check (rating in (1, 3, 5)),
    issue_category text,
    created_at     timestamptz not null default now(),
    unique (order_id)  -- one rating per order — a second tap just gets acknowledged, not double-recorded
);

create index idx_order_feedback_org on order_feedback(org_id, created_at desc);
