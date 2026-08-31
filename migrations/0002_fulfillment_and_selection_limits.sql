-- 0002_fulfillment_and_selection_limits.sql
--
-- Two gaps the real menu/flow exposed that 0001 didn't account for:
--
--   1. "N included, extra costs more" (2 toppings, 1 protein, 1 sauce
--      included free; more than that is charged separately via Extras).
--      modifier_groups needs to know its own cap so the ordering flow can
--      enforce it instead of trusting the LLM to remember.
--
--   2. Pickup vs delivery, with genuinely different data needs (pickup
--      has a fixed location + ASAP/scheduled time; delivery has an
--      address/area/landmark + its own time preference), and delivery fee
--      that can't be computed automatically yet — someone has to quote it
--      per order, same as her current manual process.

alter table modifier_groups
    add column max_selections int;  -- null = unlimited (e.g. Extras); required + max=1 is effectively a "pick exactly one"

alter table organizations
    add column pickup_address text;

alter table orders
    add column fulfillment_method text check (fulfillment_method in ('PICKUP', 'DELIVERY')),
    add column delivery_area text,
    add column delivery_landmark text,
    add column fulfillment_time_preference text check (fulfillment_time_preference in ('ASAP', 'SCHEDULED')),
    add column scheduled_for timestamptz,
    -- Delivery fee is quoted manually, by her, directly to the customer — and confirmed
    -- AFTER payment, not before (payment only ever covers the food subtotal; see
    -- orders.start_checkout / message_pipeline._start_checkout). This flag is purely
    -- informational: it's what the dashboard uses to flag which delivery orders she still
    -- needs to follow up on, not a gate on anything.
    add column delivery_fee_confirmed boolean not null default false;

-- Pickup orders have nothing to confirm — 0 is correct and final for them, not a
-- placeholder — so backfill existing/seed rows accordingly.
update orders set delivery_fee_confirmed = true where fulfillment_method = 'PICKUP' or fulfillment_method is null;
