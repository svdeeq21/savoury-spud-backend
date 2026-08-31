-- 0003_seed_savoury_spud_catalog.sql
--
-- The real menu, transcribed directly from the live "Build Your Box"
-- ordering form — not guessed at. Run after 0002.
--
-- Modeling notes:
--   * Size carries the full box price (Regular ₦9,000 / Large ₦11,000) as
--     a required modifier group, rather than duplicating the product per
--     size — Build Your Box itself has base_price = 0.
--   * Protein/Toppings/Sauces are all "N included free" — modeled at
--     price 0 with max_selections set to the included count (1/2/1).
--     Anything beyond that comes from Extras, which is uncapped and
--     repeatable (select "Extra Toppings" twice for two extra toppings —
--     the pricing engine sums every selected modifier, duplicates included).
--   * Drinks are standalone products, not modifiers — someone can order
--     more than one, which a single-select modifier group can't express.

do $$
declare
    v_org_id              uuid;
    v_box_category_id     uuid;
    v_drinks_category_id  uuid;
    v_box_product_id      uuid;
    v_size_group_id       uuid;
    v_base_group_id       uuid;
    v_protein_group_id    uuid;
    v_toppings_group_id   uuid;
    v_sauces_group_id     uuid;
    v_extras_group_id     uuid;
begin
    select id into v_org_id from organizations where slug = 'savoury-spud';

    update organizations set pickup_address = '5, Uruguay Crescent (Abuja)' where id = v_org_id;

    insert into categories (org_id, name, sort_order)
        values (v_org_id, 'Build Your Box', 0) returning id into v_box_category_id;
    insert into categories (org_id, name, sort_order)
        values (v_org_id, 'Drinks', 1) returning id into v_drinks_category_id;

    insert into products (org_id, category_id, name, description, base_price, sort_order)
        values (v_org_id, v_box_category_id, 'Build Your Box',
                'Choose your size, base, protein, toppings and sauce.', 0, 0)
        returning id into v_box_product_id;

    -- Size — required, exactly 1, carries the full box price
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Size', 'single', true, 1, 0) returning id into v_size_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_size_group_id, 'Regular', 9000, 0),
        (v_size_group_id, 'Large',   11000, 1);

    -- Base — required, exactly 1
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Base', 'single', true, 1, 1) returning id into v_base_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_base_group_id, 'Irish Potatoes', 0,    0),
        (v_base_group_id, 'Sweet Potato',   400,  1),
        (v_base_group_id, 'Plantain',       800,  2),
        (v_base_group_id, 'Nachos',         1000, 3);

    -- Protein — required, 1 included free (extra protein is bought via Extras)
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Protein', 'single', true, 1, 2) returning id into v_protein_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_protein_group_id, 'Crispy Chicken',    0, 0),
        (v_protein_group_id, 'Shawarma Beef',     0, 1),
        (v_protein_group_id, 'Shawarma Chicken',  0, 2);

    -- Toppings — required, up to 2 included free
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Toppings', 'multiple', true, 2, 3) returning id into v_toppings_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_toppings_group_id, 'Cheese Sauce',         0, 0),
        (v_toppings_group_id, 'Corn Salad',           0, 1),
        (v_toppings_group_id, 'Mexican Salsa',        0, 2),
        (v_toppings_group_id, 'Caramelised Onion',    0, 3),
        (v_toppings_group_id, 'Fried Crispy Onions',  0, 4);

    -- Sauces — required, up to 1 included free
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Sauces', 'multiple', true, 1, 4) returning id into v_sauces_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_sauces_group_id, 'Garlic Sauce',       0, 0),
        (v_sauces_group_id, 'Burger Sauce',       0, 1),
        (v_sauces_group_id, 'BBQ Sauce',          0, 2),
        (v_sauces_group_id, 'Hot Honey Mustard',  0, 3),
        (v_sauces_group_id, 'Yaji Sauce',         0, 4);

    -- Extras — optional, uncapped, repeatable (this is where anything beyond the
    -- included counts above actually gets charged)
    insert into modifier_groups (org_id, name, selection_type, required, max_selections, sort_order)
        values (v_org_id, 'Extras', 'multiple', false, null, 5) returning id into v_extras_group_id;
    insert into modifiers (group_id, name, price, sort_order) values
        (v_extras_group_id, 'Extra Base',      2000, 0),
        (v_extras_group_id, 'Extra Protein',   1500, 1),
        (v_extras_group_id, 'Extra Sauce',     300,  2),
        (v_extras_group_id, 'Extra Toppings',  500,  3);

    insert into product_modifier_groups (product_id, modifier_group_id) values
        (v_box_product_id, v_size_group_id),
        (v_box_product_id, v_base_group_id),
        (v_box_product_id, v_protein_group_id),
        (v_box_product_id, v_toppings_group_id),
        (v_box_product_id, v_sauces_group_id),
        (v_box_product_id, v_extras_group_id);

    -- Drinks — standalone products so more than one can be ordered per box
    insert into products (org_id, category_id, name, base_price, sort_order) values
        (v_org_id, v_drinks_category_id, 'Chapman',                 2500, 0),
        (v_org_id, v_drinks_category_id, 'Mojito',                  2500, 1),
        (v_org_id, v_drinks_category_id, 'Summer Sunset Refresher', 2500, 2);
end $$;
