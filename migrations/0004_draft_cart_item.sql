-- 0004_draft_cart_item.sql
--
-- Fixes a real bug found in live testing: building a box across several
-- messages ("regular" → "plantain, shawarma chicken" → "bbq sauce" → ...)
-- lost everything whenever a required group (Toppings, Sauces) hadn't been
-- answered yet. add_product was all-or-nothing — a failed validation threw
-- the whole attempt away, and the LLM had to reconstruct every prior
-- answer from raw chat history on the next turn, which it did not
-- reliably do.
--
-- draft_item holds whatever's been chosen so far for the item currently
-- being built, persisted immediately on every partial answer — not
-- something the LLM has to remember, something the backend remembers for
-- it. Cleared the moment the item is complete and committed as a real
-- order_item.

alter table orders add column draft_item jsonb;
