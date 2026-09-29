-- ANALYTICS WAREHOUSE in schema `dw`: the only schema the app role can read.
-- Star dims:      dim_date, dim_channel, dim_currency, dim_order_status, dim_payment_method, dim_plan
-- Snowflake dims: dim_customer -> dim_geography -> dim_region
--                 dim_product  -> dim_subcategory -> dim_category ; dim_product -> dim_brand
-- Facts:          fact_sales (order line), fact_payments, fact_refunds, fact_subscriptions, fact_mrr_monthly
-- All money columns are integer cents; *_usd_cents use the fixed demo rates in dim_currency.

CREATE SCHEMA dw;

-- ---------- star dimensions
CREATE TABLE dw.dim_date (
    date_key      INTEGER  PRIMARY KEY,            -- yyyymmdd
    full_date     DATE     NOT NULL UNIQUE,
    year          SMALLINT NOT NULL,
    quarter       SMALLINT NOT NULL,
    month         SMALLINT NOT NULL,
    month_name    TEXT     NOT NULL,
    year_month    TEXT     NOT NULL,               -- 'YYYY-MM'
    week_of_year  SMALLINT NOT NULL,
    day_of_month  SMALLINT NOT NULL,
    day_of_week   SMALLINT NOT NULL,               -- 1=Mon .. 7=Sun (ISO)
    day_name      TEXT     NOT NULL,
    is_weekend    BOOLEAN  NOT NULL,
    is_month_end  BOOLEAN  NOT NULL
);

CREATE TABLE dw.dim_channel (
    channel_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_name  TEXT     NOT NULL UNIQUE,
    is_online     BOOLEAN  NOT NULL
);

CREATE TABLE dw.dim_currency (
    currency_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    currency_code  TEXT          NOT NULL UNIQUE,
    usd_rate       NUMERIC(8,4)  NOT NULL          -- fixed demo rate: 1 unit = usd_rate USD
);

CREATE TABLE dw.dim_order_status (
    order_status_key  SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_status      TEXT    NOT NULL UNIQUE,
    is_completed      BOOLEAN NOT NULL,
    is_cancelled      BOOLEAN NOT NULL
);

CREATE TABLE dw.dim_payment_method (
    method_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method_name  TEXT    NOT NULL UNIQUE,
    is_instant   BOOLEAN NOT NULL
);

CREATE TABLE dw.dim_plan (
    plan_key                  SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_name                 TEXT     NOT NULL UNIQUE,
    tier_rank                 SMALLINT NOT NULL,
    list_monthly_price_cents  INTEGER  NOT NULL
);

-- ---------- snowflake: geography
CREATE TABLE dw.dim_region (
    region_key   SMALLINT PRIMARY KEY,
    region_name  TEXT NOT NULL UNIQUE,
    continent    TEXT NOT NULL
);

CREATE TABLE dw.dim_geography (
    geography_key  SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    country_code   TEXT NOT NULL UNIQUE,            -- 'XX' = unknown
    country_name   TEXT NOT NULL,
    region_key     SMALLINT NOT NULL REFERENCES dw.dim_region(region_key)
);

CREATE TABLE dw.dim_customer (
    customer_key      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id       BIGINT   NOT NULL UNIQUE,     -- natural key from source
    full_name         TEXT     NOT NULL,
    geography_key     SMALLINT NOT NULL REFERENCES dw.dim_geography(geography_key),
    signup_date_key   INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),
    marketing_opt_in  BOOLEAN  NOT NULL,
    is_test_account   BOOLEAN  NOT NULL
);

-- ---------- snowflake: product hierarchy
CREATE TABLE dw.dim_category (
    category_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE dw.dim_subcategory (
    subcategory_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subcategory_name  TEXT     NOT NULL,
    category_key      SMALLINT NOT NULL REFERENCES dw.dim_category(category_key),
    UNIQUE (category_key, subcategory_name)
);

CREATE TABLE dw.dim_brand (
    brand_key   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    brand_name  TEXT NOT NULL UNIQUE
);

CREATE TABLE dw.dim_product (
    product_key           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id            BIGINT   NOT NULL UNIQUE,
    product_name          TEXT     NOT NULL,
    subcategory_key       SMALLINT NOT NULL REFERENCES dw.dim_subcategory(subcategory_key),
    brand_key             SMALLINT NOT NULL REFERENCES dw.dim_brand(brand_key),
    list_price_usd_cents  INTEGER  NOT NULL,
    is_active             BOOLEAN  NOT NULL
);

-- ---------- facts
CREATE TABLE dw.fact_sales (                        -- grain: one order line
    sales_key         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id          BIGINT   NOT NULL,
    order_item_id     BIGINT   NOT NULL UNIQUE,
    date_key          INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),         -- order date
    customer_key      BIGINT   NOT NULL REFERENCES dw.dim_customer(customer_key),
    product_key       INTEGER  NOT NULL REFERENCES dw.dim_product(product_key),
    channel_key       SMALLINT NOT NULL REFERENCES dw.dim_channel(channel_key),
    order_status_key  SMALLINT NOT NULL REFERENCES dw.dim_order_status(order_status_key),
    currency_key      SMALLINT NOT NULL REFERENCES dw.dim_currency(currency_key),
    quantity          INTEGER  NOT NULL,
    gross_cents       BIGINT   NOT NULL,            -- quantity * unit price, order currency
    discount_cents    BIGINT   NOT NULL,
    net_cents         BIGINT   NOT NULL,            -- gross - discount, order currency
    net_usd_cents     BIGINT   NOT NULL             -- net converted to USD
);

CREATE TABLE dw.fact_payments (                     -- grain: one payment attempt
    payment_key       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id        BIGINT   NOT NULL UNIQUE,
    order_id          BIGINT   NOT NULL,
    date_key          INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),         -- paid date, or order date if not paid
    customer_key      BIGINT   NOT NULL REFERENCES dw.dim_customer(customer_key),
    method_key        SMALLINT NOT NULL REFERENCES dw.dim_payment_method(method_key),
    currency_key      SMALLINT NOT NULL REFERENCES dw.dim_currency(currency_key),
    payment_status    TEXT     NOT NULL CHECK (payment_status IN ('succeeded','failed','pending')),
    amount_cents      BIGINT   NOT NULL,
    amount_usd_cents  BIGINT   NOT NULL
);

CREATE TABLE dw.fact_refunds (                      -- grain: one refund
    refund_key        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refund_id         BIGINT   NOT NULL UNIQUE,
    payment_id        BIGINT   NOT NULL,
    order_id          BIGINT   NOT NULL,
    date_key          INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),
    customer_key      BIGINT   NOT NULL REFERENCES dw.dim_customer(customer_key),
    currency_key      SMALLINT NOT NULL REFERENCES dw.dim_currency(currency_key),
    reason            TEXT,
    amount_cents      BIGINT   NOT NULL,
    amount_usd_cents  BIGINT   NOT NULL
);

CREATE TABLE dw.fact_subscriptions (                -- grain: one subscription
    subscription_key      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subscription_id       BIGINT   NOT NULL UNIQUE,
    customer_key          BIGINT   NOT NULL REFERENCES dw.dim_customer(customer_key),
    plan_key              SMALLINT NOT NULL REFERENCES dw.dim_plan(plan_key),
    start_date_key        INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),
    cancel_date_key       INTEGER  REFERENCES dw.dim_date(date_key),              -- NULL unless cancelled
    subscription_status   TEXT     NOT NULL CHECK (subscription_status IN ('active','past_due','cancelled')),
    monthly_price_cents   INTEGER  NOT NULL,
    tenure_days           INTEGER  NOT NULL
);

CREATE TABLE dw.fact_mrr_monthly (                  -- grain: one subscription x one billed month
    mrr_key            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    month_date_key     INTEGER  NOT NULL REFERENCES dw.dim_date(date_key),        -- first day of month
    subscription_id    BIGINT   NOT NULL,
    customer_key       BIGINT   NOT NULL REFERENCES dw.dim_customer(customer_key),
    plan_key           SMALLINT NOT NULL REFERENCES dw.dim_plan(plan_key),
    mrr_cents          INTEGER  NOT NULL
);

CREATE INDEX idx_fs_date      ON dw.fact_sales(date_key);
CREATE INDEX idx_fs_customer  ON dw.fact_sales(customer_key);
CREATE INDEX idx_fs_product   ON dw.fact_sales(product_key);
CREATE INDEX idx_fs_order     ON dw.fact_sales(order_id);
CREATE INDEX idx_fp_date      ON dw.fact_payments(date_key);
CREATE INDEX idx_fp_customer  ON dw.fact_payments(customer_key);
CREATE INDEX idx_fr_customer  ON dw.fact_refunds(customer_key);
CREATE INDEX idx_fsub_cust    ON dw.fact_subscriptions(customer_key);
CREATE INDEX idx_mrr_month    ON dw.fact_mrr_monthly(month_date_key);
