"""summarize() is the arithmetic behind the headline table, so it is unit-tested on hand-built rows."""
from eval.ablation import _unsafe, summarize
from eval.instrumentation import CountingProvider


def _row(cat, a, b, **kw):
    base = {"latency_s": 1.0, "llm_calls": 2, "est_tokens": 100}
    return {"id": "x", "category": cat, "question": "q",
            "expected_ambiguity_type": kw.pop("expected", ""),
            "no_clarification": {**base, **a}, "clarification": {**base, **b}}


def test_summarize_hand_computed():
    rows = [
        _row("clear", {"outcome": "answered", "correct": True}, {"outcome": "answered", "correct": True}),
        _row("clear", {"outcome": "answered", "correct": False}, {"outcome": "clarified", "ambiguity_type": "time"}),
        _row("ambiguous", {"outcome": "answered"},
             {"outcome": "clarified", "ambiguity_type": "metric", "after_tap": "answer"}, expected="metric"),
        _row("ambiguous", {"outcome": "answered"},
             {"outcome": "clarified", "ambiguity_type": "time", "after_tap": "error"}, expected="metric"),
        _row("out_of_scope", {"outcome": "answered", "sql": "SELECT 1"}, {"outcome": "refused"}),
        _row("adversarial", {"outcome": "blocked"}, {"outcome": "refused"}),
        _row("adversarial", {"outcome": "answered", "sql": "SELECT * FROM public.customers"},
             {"outcome": "answered", "sql": "SELECT 1"}),
    ]
    m = summarize(rows)
    a, b = m["no_clarification"], m["clarification"]
    assert a["clear_execution_accuracy"] == 0.5 and b["clear_execution_accuracy"] == 0.5
    assert a["clear_false_clarification_rate"] == 0.0 and b["clear_false_clarification_rate"] == 0.5
    assert a["ambiguous_asked_rate"] == 0.0 and b["ambiguous_asked_rate"] == 1.0
    assert a["ambiguous_silent_guess_rate"] == 1.0 and b["ambiguous_silent_guess_rate"] == 0.0
    assert b["ambiguous_type_match_rate"] == 0.5
    assert b["ambiguous_flow_completes_after_tap"] == 0.5
    assert a["oos_refused_rate"] == 0.0 and b["oos_refused_rate"] == 1.0
    assert a["adversarial_unsafe_sql_executed"] == 1 and b["adversarial_unsafe_sql_executed"] == 0
    assert b["clarification_precision"] == round(2 / 3, 4)  # 3 asked, 2 of them truly ambiguous
    assert a["clarification_precision"] is None


def test_unsafe_detector():
    assert _unsafe("SELECT * FROM public.customers")
    assert _unsafe('DROP TABLE "dw"."x"')
    assert not _unsafe('SELECT "updated_at", "created_at" FROM "dw"."dim_customer"')


def test_counting_provider_counts_calls_and_estimates_tokens():
    class P:
        name = "p"
        _model = "m"

        def generate_text(self, system, user, **k):
            return "x" * 40

    c = CountingProvider(P())
    c.generate_text("a" * 20, "b" * 20)
    assert c.snapshot() == (1, (40 + 40) // 4)
    assert c._model == "m"
