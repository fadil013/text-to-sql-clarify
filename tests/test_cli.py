"""CLI clarify-then-answer loop, driven by a scripted provider and scripted user input."""
import pytest

from app.clarification import AmbiguityAnalysis
from app.cli import run_interactive
from app.config import get_settings
from app.llm.provider import LLMProvider
from app.schemas import SQLGeneration

pytestmark = pytest.mark.integration


class _P(LLMProvider):
    name = "p"

    def __init__(self, analysis):
        self.analysis = analysis

    def generate_structured(self, system, user, schema, *, temperature=0.0):
        if schema is AmbiguityAnalysis:
            return self.analysis
        return SQLGeneration(sql="SELECT COUNT(*) AS n FROM dw.dim_customer", assumptions=["a"])

    def generate_text(self, system, user, *, temperature=0.2):
        return "There are some customers."


def test_clarify_then_answer_by_picking_option_number():
    analysis = AmbiguityAnalysis(status="ambiguous", ambiguity_type="metric",
                                 interpretations=["revenue", "orders"], confidence=0.4)
    lines, answers = [], iter(["1"])
    code = run_interactive("Who is our best customer?", _P(analysis), get_settings(),
                           input_fn=lambda _: next(answers), out=lines.append)
    text = "\n".join(lines)
    assert code == 0
    assert "1. revenue" in text and "Answer: There are some customers." in text


def test_refusal_returns_nonzero():
    analysis = AmbiguityAnalysis(status="out_of_scope", refusal_reason="Not a data question.")
    lines = []
    code = run_interactive("Tell me a joke", _P(analysis), get_settings(), input_fn=lambda _: "", out=lines.append)
    assert code == 1 and "Not a data question." in "\n".join(lines)
