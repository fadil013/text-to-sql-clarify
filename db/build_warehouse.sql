-- ETL: rebuild the whole `dw` schema from the source tables in `public`. Idempotent.
-- Run by db/seed.py inside the same transaction as the source load.

TRUNCATE dw.fact_mrr_monthly, dw.fact_subscriptions, dw.fact_refunds, dw.fact_payments, dw.fact_sales,
         dw.dim_product, dw.dim_brand, dw.dim_subcategory, dw.dim_category,
         dw.dim_customer, dw.dim_geography, dw.dim_region,
         dw.dim_channel, dw.dim_currency, dw.dim_order_status, dw.dim_payment_method, dw.dim_plan,
         dw.dim_date
    RESTART IDENTITY CASCADE;

-- ---------- static / star dimensions
INSERT INTO dw.dim_date
SELECT to_char(d, 'YYYYMMDD')::int, d, extract(year FROM d), extract(quarter FROM d), extract(month FROM d),
       trim(to_char(d, 'Month')), to_char(d, 'YYYY-MM'), extract(week FROM d), extract(day FROM d),
       extract(isodow FROM d), trim(to_char(d, 'Day')), extract(isodow FROM d) IN (6, 7),
       extract(day FROM d + 1) = 1
FROM (SELECT DATE '2024-01-01' + i AS d FROM generate_series(0, 1460) i) g;

INSERT INTO dw.dim_channel (channel_name, is_online) VALUES
    ('web', TRUE), ('mobile_app', TRUE), ('marketplace', TRUE), ('phone', FALSE);
INSERT INTO dw.dim_currency (currency_code, usd_rate) VALUES ('USD', 1.0), ('EUR', 1.08), ('GBP', 1.27);
INSERT INTO dw.dim_order_status (order_status, is_completed, is_cancelled) VALUES
    ('pending', FALSE, FALSE), ('paid', TRUE, FALSE), ('shipped', TRUE, FALSE),
    ('delivered', TRUE, FALSE), ('cancelled', FALSE, TRUE);
INSERT INTO dw.dim_payment_method (method_name, is_instant) VALUES
    ('card', TRUE), ('paypal', TRUE), ('bank_transfer', FALSE);
INSERT INTO dw.dim_plan (plan_name, tier_rank, list_monthly_price_cents) VALUES
    ('basic', 1, 900), ('pro', 2, 2900), ('enterprise', 3, 9900);

-- ---------- geography snowflake
INSERT INTO dw.dim_region (region_key, region_name, continent) VALUES
    (1, 'North America', 'Americas'), (2, 'Latin America', 'Americas'),
    (3, 'Western Europe', 'Europe'), (4, 'Asia Pacific', 'Asia-Pacific'), (5, 'Unknown', 'Unknown');
INSERT INTO dw.dim_geography (country_code, country_name, region_key) VALUES
    ('US', 'United States', 1), ('CA', 'Canada', 1), ('BR', 'Brazil', 2),
    ('GB', 'United Kingdom', 3), ('DE', 'Germany', 3), ('FR', 'France', 3), ('NL', 'Netherlands', 3),
    ('AU', 'Australia', 4), ('IN', 'India', 4), ('JP', 'Japan', 4), ('XX', 'Unknown', 5);

INSERT INTO dw.dim_customer (customer_id, full_name, geography_key, signup_date_key, marketing_opt_in, is_test_account)
SELECT c.id, c.full_name, g.geography_key, to_char(c.created_at AT TIME ZONE 'UTC', 'YYYYMMDD')::int,
       c.marketing_opt_in, c.is_test_account
FROM public.customers c
JOIN dw.dim_geography g ON g.country_code = COALESCE(c.country, 'XX')
ORDER BY c.id;

-- ---------- product snowflake
INSERT INTO dw.dim_category (category_name) SELECT DISTINCT category FROM public.products ORDER BY 1;
INSERT INTO dw.dim_subcategory (subcategory_name, category_key)
SELECT DISTINCT p.subcategory, c.category_key
FROM public.products p JOIN dw.dim_category c ON c.category_name = p.category
ORDER BY 2, 1;
INSERT INTO dw.dim_brand (brand_name) SELECT DISTINCT brand FROM public.products ORDER BY 1;
INSERT INTO dw.dim_product (product_id, product_name, subcategory_key, brand_key, list_price_usd_cents, is_active)
SELECT p.id, p.name, s.subcategory_key, b.brand_key, p.price_cents, p.is_active
FROM public.products p
JOIN dw.dim_category c    ON c.category_name = p.category
JOIN dw.dim_subcategory s ON s.subcategory_name = p.subcategory AND s.category_key = c.category_key
JOIN dw.dim_brand b       ON b.brand_name = p.brand
ORDER BY p.id;

-- ---------- facts
INSERT INTO dw.fact_sales (order_id, order_item_id, date_key, customer_key, product_key, channel_key,
                           order_status_key, currency_key, quantity, gross_cents, discount_cents,
                           net_cents, net_usd_cents)
SELECT o.id, oi.id, to_char(o.created_at AT TIME ZONE 'UTC', 'YYYYMMDD')::int, dc.customer_key,
       dp.product_key, ch.channel_key, st.order_status_key, cu.currency_key, oi.quantity,
       oi.quantity::bigint * oi.unit_price_cents, oi.discount_cents,
       oi.quantity::bigint * oi.unit_price_cents - oi.discount_cents,
       round((oi.quantity::bigint * oi.unit_price_cents - oi.discount_cents) * cu.usd_rate)::bigint
FROM public.order_items oi
JOIN public.orders o          ON o.id = oi.order_id
JOIN dw.dim_customer dc       ON dc.customer_id = o.customer_id
JOIN dw.dim_product dp        ON dp.product_id = oi.product_id
JOIN dw.dim_channel ch        ON ch.channel_name = o.channel
JOIN dw.dim_order_status st   ON st.order_status = o.status
JOIN dw.dim_currency cu       ON cu.currency_code = o.currency
ORDER BY oi.id;

INSERT INTO dw.fact_payments (payment_id, order_id, date_key, customer_key, method_key, currency_key,
                              payment_status, amount_cents, amount_usd_cents)
SELECT p.id, p.order_id, to_char(COALESCE(p.paid_at, o.created_at) AT TIME ZONE 'UTC', 'YYYYMMDD')::int,
       dc.customer_key, pm.method_key, cu.currency_key, p.status, p.amount_cents,
       round(p.amount_cents * cu.usd_rate)::bigint
FROM public.payments p
JOIN public.orders o            ON o.id = p.order_id
JOIN dw.dim_customer dc         ON dc.customer_id = o.customer_id
JOIN dw.dim_payment_method pm   ON pm.method_name = p.method
JOIN dw.dim_currency cu         ON cu.currency_code = o.currency
ORDER BY p.id;

INSERT INTO dw.fact_refunds (refund_id, payment_id, order_id, date_key, customer_key, currency_key,
                             reason, amount_cents, amount_usd_cents)
SELECT r.id, r.payment_id, p.order_id, to_char(r.created_at AT TIME ZONE 'UTC', 'YYYYMMDD')::int,
       dc.customer_key, cu.currency_key, r.reason, r.amount_cents, round(r.amount_cents * cu.usd_rate)::bigint
FROM public.refunds r
JOIN public.payments p    ON p.id = r.payment_id
JOIN public.orders o      ON o.id = p.order_id
JOIN dw.dim_customer dc   ON dc.customer_id = o.customer_id
JOIN dw.dim_currency cu   ON cu.currency_code = o.currency
ORDER BY r.id;

INSERT INTO dw.fact_subscriptions (subscription_id, customer_key, plan_key, start_date_key, cancel_date_key,
                                   subscription_status, monthly_price_cents, tenure_days)
SELECT s.id, dc.customer_key, pl.plan_key, to_char(s.started_at AT TIME ZONE 'UTC', 'YYYYMMDD')::int,
       to_char(s.cancelled_at AT TIME ZONE 'UTC', 'YYYYMMDD')::int, s.status, s.monthly_price_cents,
       (COALESCE(s.cancelled_at, now())::date - s.started_at::date)
FROM public.subscriptions s
JOIN dw.dim_customer dc ON dc.customer_id = s.customer_id
JOIN dw.dim_plan pl     ON pl.plan_name = s.plan
ORDER BY s.id;

-- MRR: a subscription bills every calendar month from its start month up to the month BEFORE
-- cancellation (or the current month if not cancelled).
INSERT INTO dw.fact_mrr_monthly (month_date_key, subscription_id, customer_key, plan_key, mrr_cents)
SELECT to_char(m, 'YYYYMMDD')::int, s.id, dc.customer_key, pl.plan_key, s.monthly_price_cents
FROM public.subscriptions s
JOIN dw.dim_customer dc ON dc.customer_id = s.customer_id
JOIN dw.dim_plan pl     ON pl.plan_name = s.plan
CROSS JOIN LATERAL generate_series(
    date_trunc('month', s.started_at AT TIME ZONE 'UTC'),
    CASE WHEN s.cancelled_at IS NULL THEN date_trunc('month', now() AT TIME ZONE 'UTC')
         ELSE date_trunc('month', s.cancelled_at AT TIME ZONE 'UTC') - interval '1 month' END,
    interval '1 month') AS m
ORDER BY s.id, m;

ANALYZE;
