"""Integration tests for the Phase 5 ask()/continue_after_clarification() pipeline wiring.
Needs the real DB (read-only role); uses a stub LLM so no API key/network call is needed.
"""
import pytest
from pydantic import BaseModel

from app.clarification import AmbiguityAnalysis, SessionMemory
from app.config import get_settings
from app.llm.provider import LLMProvider
from app.pipeline import ask, continue_after_clarification
from app.schemas import SQLGeneration

pytestmark = pytest.mark.integration


class _ScriptedProvider(LLMProvider):
    """Returns canned structured outputs keyed by the Pydantic schema requested, so a single
    stub can drive the whole ask() -> clarify -> continue_after_clarification() chain."""
    name = "scripted"

    def __init__(self, analysis: AmbiguityAnalysis | None = None, sql: str = "SELECT 1 AS n"):
        self._analysis = analysis
        self._sql = sql

    def generate_structured(self, system, user, schema, *, temperature=0.0):
        if schema is AmbiguityAnalysis:
            assert self._analysis is not None, "test didn't expect an ambiguity check"
            return self._analysis
        if schema is SQLGeneration:
            return SQLGeneration(sql=self._sql, tables_used=[], assumptions=[])
        raise AssertionError(f"unexpected schema {schema}")

    def generate_text(self, system, user, *, temperature=0.2):
        return "stub answer text"


def test_ask_answers_directly_on_glossary_hit():
    provider = _ScriptedProvider(sql="SELECT COUNT(*) AS n FROM dw.dim_customer WHERE is_test_account = FALSE")
    result = ask("How many customers do we have, excluding test accounts?",
                 provider=provider, settings=get_settings())
    assert result.kind == "answer"
    assert result.answer is not None
    assert not result.answer.clarification_involved


def test_ask_returns_clarification_on_ambiguity():
    analysis = AmbiguityAnalysis(status="ambiguous", ambiguity_type="metric",
                                  interpretations=["revenue", "order_count"], confidence=0.4)
    provider = _ScriptedProvider(analysis=analysis)
    result = ask("Who is our best customer?", provider=provider, settings=get_settings())
    assert result.kind == "clarify"
    assert result.clarification is not None
    assert result.answer is None


def test_ask_refuses_out_of_scope():
    analysis = AmbiguityAnalysis(status="out_of_scope", refusal_reason="Not in this data.")
    provider = _ScriptedProvider(analysis=analysis)
    result = ask("What's the weather?", provider=provider, settings=get_settings())
    assert result.kind == "refuse"
    assert result.refusal_reason == "Not in this data."


def test_continue_after_clarification_produces_marked_answer():
    provider = _ScriptedProvider(sql="SELECT COUNT(*) AS n FROM dw.dim_customer WHERE is_test_account = FALSE")
    memory = SessionMemory()
    result = continue_after_clarification(
        "Who is our best customer?", "metric", "revenue",
        provider=provider, settings=get_settings(), memory=memory,
    )
    assert result.kind == "answer"
    assert result.answer.clarification_involved is True
    assert memory.recall("metric") == "revenue"


def test_ask_rejects_unsafe_generated_sql_via_validator():
    """Confirms Phase 4's validator still runs inside the ask() path, not just the old pipeline."""
    provider = _ScriptedProvider(sql="SELECT * FROM public.customers")
    result = ask("some clear-sounding question using revenue", provider=provider, settings=get_settings())
    # Phase 6: a blocked query is surfaced as a clean refusal, not an exception, and never executed
    assert result.kind == "refuse"
    assert result.answer is None
