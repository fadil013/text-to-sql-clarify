"""Unit tests for the eval runner's pure logic (no network, no DB)."""
from eval.runner import UNSAFE_KEYWORDS, _normalize_rows, _rate


def test_normalize_rows_ignores_column_names_and_order():
    a = [{"n": 5}]
    b = [{"count": 5}]
    assert _normalize_rows(a) == _normalize_rows(b)


def test_normalize_rows_ignores_row_order():
    a = [{"x": 1}, {"x": 2}]
    b = [{"x": 2}, {"x": 1}]
    assert _normalize_rows(a) == _normalize_rows(b)


def test_normalize_rows_detects_real_mismatch():
    assert _normalize_rows([{"n": 5}]) != _normalize_rows([{"n": 6}])


def test_rate_handles_empty():
    assert _rate([], "match") is None


def test_unsafe_keywords_cover_common_attacks():
    for kw in ("DROP", "DELETE", "UPDATE", "INSERT", "GRANT", "ALTER", "TRUNCATE", "CREATE"):
        assert any(kw in u for u in UNSAFE_KEYWORDS)


def test_normalize_rows_treats_equal_decimals_with_different_scale_as_equal():
    from decimal import Decimal
    assert _normalize_rows([{"x": Decimal("37597.15")}]) == _normalize_rows([{"x": Decimal("37597.150000000000")}])
    assert _normalize_rows([{"x": Decimal("1.50")}]) != _normalize_rows([{"x": Decimal("1.51")}])
    assert _normalize_rows([{"x": 100}]) == _normalize_rows([{"x": 100}])
