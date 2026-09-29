"""Unit tests for the seed generator (no database needed)."""
from datetime import datetime, timezone

import pytest

from db.seed import generate_dataset

ANCHOR = datetime(2026, 9, 29, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def data():
    return generate_dataset(ANCHOR)


def test_deterministic():
    assert generate_dataset(ANCHOR) == generate_dataset(ANCHOR)


def test_all_tables_populated(data):
    for table, rows in data.items():
        assert rows, f"{table} is empty"


def test_foreign_keys_valid(data):
    cust = {c[0] for c in data["customers"]}
    prod = {p[0] for p in data["products"]}
    orders = {o[0] for o in data["orders"]}
    pays = {p[0] for p in data["payments"]}
    assert all(o[1] in cust for o in data["orders"])
    assert all(i[1] in orders and i[2] in prod for i in data["order_items"])
    assert all(p[1] in orders for p in data["payments"])
    assert all(r[1] in pays for r in data["refunds"])
    assert all(s[1] in cust for s in data["subscriptions"])


def test_ids_unique(data):
    for table, rows in data.items():
        ids = [r[0] for r in rows]
        assert len(ids) == len(set(ids)), table


def test_messy_data_present(data):
    orders = data["orders"]
    assert any(o[2] == "cancelled" for o in orders)
    assert any(o[4] is None for o in orders), "expected some NULL totals"
    assert any(c[4] is None for c in data["customers"]), "expected some NULL countries"
    assert any(c[6] for c in data["customers"]), "expected test accounts"
    assert {o[3] for o in orders} == {"USD", "EUR", "GBP"}
    assert data["refunds"], "expected refunds"


def test_payment_invariants(data):
    for p in data["payments"]:
        status, paid_at = p[4], p[6]
        assert (paid_at is not None) == (status == "succeeded"), p


def test_refunds_not_exceeding_payment(data):
    amount = {p[0]: p[2] for p in data["payments"]}
    assert all(r[2] <= amount[r[1]] for r in data["refunds"])


def test_subscription_invariants(data):
    for s in data["subscriptions"]:
        assert (s[6] is not None) == (s[3] == "cancelled"), s


def test_no_future_dates(data):
    for o in data["orders"]:
        assert o[5] <= ANCHOR
    for c in data["customers"]:
        assert c[7] <= ANCHOR
