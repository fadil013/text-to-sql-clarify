"""The repair eval's fault injection must produce faults that are real (they actually fail) and
cover every fault type, otherwise the repair-success number is meaningless."""
import pytest

from app.config import get_settings
from eval.repair_eval import FAULTS, build_fault_cases


def test_fault_functions_change_the_sql_or_decline():
    sql = "SELECT SUM(net_usd_cents) FROM dw.fact_sales f JOIN dw.dim_date d ON f.date_key = d.date_key"
    for name, fn in FAULTS.items():
        out = fn(sql)
        assert out is not None and out != sql, name
    assert FAULTS["hallucinated_column"]("SELECT 1") is None


@pytest.mark.integration
def test_every_injected_fault_really_fails_and_all_kinds_are_covered():
    from eval.repair_eval import _fails

    settings = get_settings()
    cases = build_fault_cases(settings)
    assert len(cases) >= 20
    assert {c["fault"] for c in cases} == set(FAULTS)
    for c in cases:
        assert _fails(c["broken_sql"], settings), c["case"]["id"]
