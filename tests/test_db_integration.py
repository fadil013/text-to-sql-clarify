"""Integration tests: need `docker compose up -d` and `python -m db.seed`.

These verify the security-critical DB role behaves as designed.
"""
from pathlib import Path

import psycopg
import pytest
import yaml

from app.config import get_settings

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ro():
    try:
        conn = psycopg.connect(get_settings().readonly_dsn, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"database not reachable: {e}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def admin():
    try:
        conn = psycopg.connect(get_settings().admin_dsn, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"database not reachable: {e}")
    yield conn
    conn.close()


def test_seeded(ro):
    for table in ["customers", "products", "orders", "order_items", "payments", "refunds", "subscriptions"]:
        n = ro.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert n > 0, table


@pytest.mark.parametrize("sql", [
    "INSERT INTO products (name, category, price_cents) VALUES ('x','y',1)",
    "UPDATE products SET price_cents = 0",
    "DELETE FROM products",
    "DROP TABLE products",
    "TRUNCATE products",
    "CREATE TABLE evil (id int)",
    "CREATE TABLE public.evil (id int)",
    "ALTER TABLE products ADD COLUMN x int",
    "GRANT ALL ON products TO PUBLIC",
    "CREATE ROLE evil LOGIN",
])
def test_writes_and_ddl_rejected(ro, sql):
    with pytest.raises(psycopg.errors.Error):
        ro.execute(sql)


def test_cannot_disable_readonly_and_write(ro):
    # even if the session flag is flipped, the role has no write privilege
    ro.execute("SET default_transaction_read_only = off")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute("INSERT INTO products (name, category, price_cents) VALUES ('x','y',1)")


@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers LIMIT 1",
    "SELECT phone FROM customers LIMIT 1",
    "SELECT card_last4 FROM payments LIMIT 1",
    "SELECT * FROM customers LIMIT 1",
    "SELECT * FROM payments LIMIT 1",
])
def test_pii_columns_blocked(ro, sql):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(sql)


def test_non_pii_columns_readable(ro):
    ro.execute("SELECT id, full_name, country, created_at FROM customers LIMIT 1").fetchall()
    ro.execute("SELECT id, order_id, amount_cents, status FROM payments LIMIT 1").fetchall()


def test_statement_timeout_enforced(ro):
    with pytest.raises(psycopg.errors.QueryCanceled):
        ro.execute("SELECT pg_sleep(30)")


def test_role_is_not_superuser(ro):
    su = ro.execute("SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user").fetchone()[0]
    assert su is False


def test_schema_docs_cover_every_column(admin):
    docs = yaml.safe_load((ROOT / "config" / "schema_docs.yaml").read_text(encoding="utf-8"))["tables"]
    rows = admin.execute("SELECT table_name, column_name FROM information_schema.columns "
                         "WHERE table_schema = 'public'").fetchall()
    actual = {}
    for t, c in rows:
        actual.setdefault(t, set()).add(c)
    assert set(docs) == set(actual)
    for table, cols in actual.items():
        assert set(docs[table]["columns"]) == cols, table


def test_pii_flags_match_db_grants(admin, ro):
    docs = yaml.safe_load((ROOT / "config" / "schema_docs.yaml").read_text(encoding="utf-8"))["tables"]
    for table, spec in docs.items():
        for col, meta in spec["columns"].items():
            blocked = False
            try:
                ro.execute(f"SELECT {col} FROM {table} LIMIT 1")
            except psycopg.errors.InsufficientPrivilege:
                blocked = True
            assert blocked == bool(meta.get("pii")), f"{table}.{col}"
