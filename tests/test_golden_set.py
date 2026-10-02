"""Structural checks on the golden set (no network) + that every gold_sql actually runs (DB needed)."""
import pytest

from eval.golden_set import CASES

CATEGORIES = {"clear", "ambiguous", "out_of_scope", "adversarial"}


def test_60_cases_well_formed():
    assert len(CASES) == 60
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in CASES:
        assert c["category"] in CATEGORIES, c["id"]
        assert c["question"].strip(), c["id"]


def test_clear_cases_have_gold_sql():
    for c in CASES:
        if c["category"] == "clear":
            assert c.get("gold_sql", "").strip().upper().startswith("SELECT"), c["id"]


def test_ambiguous_cases_have_expected_type():
    valid_types = {"metric", "time", "entity", "scope", "missing_param", "vague_term"}
    for c in CASES:
        if c["category"] == "ambiguous":
            assert c.get("expected_ambiguity_type") in valid_types, c["id"]


def test_category_counts():
    from collections import Counter
    counts = Counter(c["category"] for c in CASES)
    assert counts["clear"] == 24
    assert counts["ambiguous"] == 16
    assert counts["out_of_scope"] == 8
    assert counts["adversarial"] == 12


@pytest.mark.integration
@pytest.mark.parametrize("case", [c for c in CASES if c["category"] == "clear"], ids=lambda c: c["id"])
def test_every_gold_sql_executes(case):
    from app.config import get_settings
    from app.pipeline import run_sql
    rows = run_sql(case["gold_sql"], get_settings())
    assert rows, f"{case['id']}: gold_sql returned zero rows (check it's not vacuously true)"
