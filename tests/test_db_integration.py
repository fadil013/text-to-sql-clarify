"""Integration tests: need `docker compose up -d` and `python -m db.seed`.

They verify (1) the security-critical read-only role and (2) that the star/snowflake warehouse
reconciles with the source system.
"""
from pathlib import Path

import psycopg
import pytest
import yaml

from app.config import get_settings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent

DW_TABLES = [
    "dim_date", "dim_channel", "dim_currency", "dim_order_status", "dim_payment_method", "dim_plan",
    "dim_region", "dim_geography", "dim_customer", "dim_category", "dim_subcategory", "dim_brand",
    "dim_product", "fact_sales", "fact_payments", "fact_refunds", "fact_subscriptions", "fact_mrr_monthly",
]


def _connect(dsn):
    try:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"database not reachable: {e}")


@pytest.fixture(scope="module")
def ro():
    conn = _connect(get_settings().readonly_dsn)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def admin():
    conn = _connect(get_settings().admin_dsn)
    yield conn
    conn.close()


def one(conn, sql):
    return conn.execute(sql).fetchone()[0]


# ------------------------------------------------------------------ read-only role (security)

@pytest.mark.parametrize("table", DW_TABLES)
def test_app_can_read_every_warehouse_table(ro, table):
    assert one(ro, f"SELECT count(*) FROM {table}") > 0


@pytest.mark.parametrize("sql", [
    "INSERT INTO dim_channel (channel_name, is_online) VALUES ('x', true)",
    "UPDATE fact_sales SET quantity = 0",
    "DELETE FROM fact_sales",
    "DROP TABLE fact_sales",
    "TRUNCATE fact_sales",
    "CREATE TABLE evil (id int)",
    "CREATE TABLE dw.evil (id int)",
    "CREATE TABLE public.evil (id int)",
    "CREATE SCHEMA evil",
    "ALTER TABLE dim_channel ADD COLUMN x int",
    "GRANT ALL ON dim_channel TO PUBLIC",
    "CREATE ROLE evil LOGIN",
])
def test_writes_and_ddl_rejected(ro, sql):
    with pytest.raises(psycopg.errors.Error):
        ro.execute(sql)


def test_cannot_disable_readonly_and_write(ro):
    ro.execute("SET default_transaction_read_only = off")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute("INSERT INTO dim_channel (channel_name, is_online) VALUES ('x', true)")


@pytest.mark.parametrize("table", ["customers", "payments", "orders", "order_items",
                                   "products", "refunds", "subscriptions"])
def test_source_system_unreachable(ro, table):
    """The source (OLTP) tables hold PII; the app role must not touch them at all."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(f"SELECT * FROM public.{table} LIMIT 1")


def test_warehouse_has_no_pii_columns(admin):
    n = one(admin, "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'dw' "
                   "AND column_name IN ('email','phone','card_last4')")
    assert n == 0


def test_statement_timeout_enforced(ro):
    with pytest.raises(psycopg.errors.QueryCanceled):
        ro.execute("SELECT pg_sleep(30)")


def test_role_is_not_superuser(ro):
    assert one(ro, "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles "
                   "WHERE rolname = current_user") is False


# ------------------------------------------------------------------ docs

def test_schema_docs_cover_every_column(admin):
    docs = yaml.safe_load((ROOT / "config" / "schema_docs.yaml").read_text(encoding="utf-8"))["tables"]
    rows = admin.execute("SELECT table_name, column_name FROM information_schema.columns "
                         "WHERE table_schema = 'dw'").fetchall()
    actual = {}
    for t, c in rows:
        actual.setdefault(t, set()).add(c)
    assert set(docs) == set(actual) == set(DW_TABLES)
    for table, cols in actual.items():
        assert set(docs[table]["columns"]) == cols, table


# ------------------------------------------------------------------ warehouse reconciliation

def test_row_counts_reconcile(admin):
    pairs = [("dw.fact_sales", "public.order_items"), ("dw.fact_payments", "public.payments"),
             ("dw.fact_refunds", "public.refunds"), ("dw.fact_subscriptions", "public.subscriptions"),
             ("dw.dim_customer", "public.customers"), ("dw.dim_product", "public.products")]
    for dw, src in pairs:
        assert one(admin, f"SELECT count(*) FROM {dw}") == one(admin, f"SELECT count(*) FROM {src}"), dw


def test_sales_totals_reconcile(admin):
    src = one(admin, "SELECT sum(quantity::bigint * unit_price_cents - discount_cents) FROM public.order_items")
    assert one(admin, "SELECT sum(net_cents) FROM dw.fact_sales") == src
    assert one(admin, "SELECT sum(gross_cents - discount_cents - net_cents) FROM dw.fact_sales") == 0


def test_usd_conversion_consistent(admin):
    assert one(admin, "SELECT count(*) FROM dw.fact_sales f JOIN dw.dim_currency c USING (currency_key) "
                      "WHERE c.currency_code = 'USD' AND f.net_usd_cents <> f.net_cents") == 0
    assert one(admin, "SELECT count(DISTINCT currency_key) FROM dw.fact_sales") == 3


def test_order_totals_match_fact_sales(admin):
    bad = one(admin, "SELECT count(*) FROM (SELECT o.id FROM public.orders o "
                     "JOIN dw.fact_sales f ON f.order_id = o.id WHERE o.total_cents IS NOT NULL "
                     "GROUP BY o.id, o.total_cents HAVING sum(f.net_cents) <> o.total_cents) x")
    assert bad == 0


def test_payment_and_refund_sums_reconcile(admin):
    assert one(admin, "SELECT sum(amount_cents) FROM dw.fact_payments") == \
        one(admin, "SELECT sum(amount_cents) FROM public.payments")
    assert one(admin, "SELECT sum(amount_cents) FROM dw.fact_refunds") == \
        one(admin, "SELECT sum(amount_cents) FROM public.refunds")


def test_mrr_never_active_after_cancellation(admin):
    bad = one(admin, "SELECT count(*) FROM dw.fact_mrr_monthly m JOIN dw.fact_subscriptions s "
                     "USING (subscription_id) JOIN dw.dim_date d ON d.date_key = m.month_date_key "
                     "JOIN dw.dim_date c ON c.date_key = s.cancel_date_key WHERE d.full_date >= c.full_date")
    assert bad == 0


def test_snowflake_chains_join(ro):
    geo = ro.execute("SELECT r.region_name, count(*) FROM fact_sales f "
                     "JOIN dim_customer c USING (customer_key) JOIN dim_geography g USING (geography_key) "
                     "JOIN dim_region r USING (region_key) GROUP BY 1").fetchall()
    assert len(geo) >= 4 and sum(n for _, n in geo) == one(ro, "SELECT count(*) FROM fact_sales")
    prod = ro.execute("SELECT cat.category_name, count(*) FROM fact_sales f "
                      "JOIN dim_product p USING (product_key) JOIN dim_subcategory s USING (subcategory_key) "
                      "JOIN dim_category cat USING (category_key) GROUP BY 1").fetchall()
    assert len(prod) == 5 and sum(n for _, n in prod) == one(ro, "SELECT count(*) FROM fact_sales")


def test_messy_data_survives_into_warehouse(ro):
    assert one(ro, "SELECT count(*) FROM fact_sales f JOIN dim_order_status s USING (order_status_key) "
                   "WHERE s.is_cancelled") > 0
    assert one(ro, "SELECT count(*) FROM dim_customer WHERE is_test_account") > 0
    assert one(ro, "SELECT count(*) FROM dim_customer c JOIN dim_geography g USING (geography_key) "
                   "WHERE g.country_code = 'XX'") > 0
