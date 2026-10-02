"""Live tests against the real free APIs. Need API keys in .env; skip cleanly if absent.

Marked `llm` (and `integration` for the full pipeline test, since it also needs the DB).
"""
import pytest

from app.config import get_settings
from app.llm.factory import get_provider
from app.schemas import SQLGeneration

pytestmark = pytest.mark.llm


def _provider(name):
    s = get_settings()
    key = s.gemini_api_key if name == "gemini" else s.groq_api_key
    if not key:
        pytest.skip(f"{name.upper()}_API_KEY not set")
    s.llm_provider = name
    return get_provider(s)


@pytest.mark.parametrize("name", ["gemini", "groq"])
def test_provider_generates_valid_structured_output(name):
    provider = _provider(name)
    result = provider.generate_structured(
        "You output JSON only.",
        'Return {"sql": "SELECT 1", "tables_used": [], "assumptions": []} exactly.',
        SQLGeneration,
    )
    assert isinstance(result, SQLGeneration)
    assert "SELECT" in result.sql.upper()


@pytest.mark.parametrize("name", ["gemini", "groq"])
def test_provider_generates_text(name):
    provider = _provider(name)
    text = provider.generate_text("Reply with exactly one word.", "Say: ok")
    assert text.strip()


@pytest.mark.integration
@pytest.mark.parametrize("name", ["gemini", "groq"])
def test_baseline_pipeline_answers_a_clear_question(name):
    from app.pipeline import answer_question

    provider = _provider(name)
    result = answer_question("How many products are in the catalog?", provider=provider)
    assert result.sql.strip().upper().startswith("SELECT")
    assert result.answer.strip()
