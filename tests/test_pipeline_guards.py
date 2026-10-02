"""Unit tests for the Phase 2 baseline guard (no network, no DB needed)."""
import pytest

from app.pipeline import NotSelectError, _guard_select_only


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select id from dim_customer",
    "  SELECT * FROM fact_sales;  ",
])
def test_allows_single_select(sql):
    _guard_select_only(sql)  # must not raise


@pytest.mark.parametrize("sql", [
    "DROP TABLE dim_customer",
    "SELECT 1; DROP TABLE dim_customer",
    "INSERT INTO dim_customer VALUES (1)",
    "UPDATE dim_customer SET full_name = 'x'",
])
def test_rejects_non_select(sql):
    with pytest.raises(NotSelectError):
        _guard_select_only(sql)
