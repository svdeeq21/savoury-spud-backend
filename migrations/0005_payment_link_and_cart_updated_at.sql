-- 0005_payment_link_and_cart_updated_at.sql
--
-- Two small additive fixes from a real transcript review:
--
--   1. payments.authorization_url — wasn't stored anywhere, so a customer
--      who went quiet mid-payment and came back couldn't be reminded with
--      the actual working link, only "check your messages above".
--
--   2. (Code-only fix, no schema change needed) orders.updated_at now
--      gets bumped by set_fulfillment_details — it previously didn't,
--      which let the cart-staleness clock run out from the last pricing
--      mutation instead of the last real interaction. See orders.py.

alter table payments add column authorization_url text;
