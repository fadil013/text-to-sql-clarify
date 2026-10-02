"""Pipeline-level safety tests (no network, no DB): confirms generate_sql actually calls the
Phase 4 validator and propagates its rejection, using a stub provider so no API key is needed.
Exhaustive adversarial coverage lives in tests/test_validator.py; this just checks the wiring.
"""
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm.provider import LLMProvider
from app.pipeline import NotSelectError, generate_sql
from app.schemas import SQLGeneration


class _StubProvider(LLMProvider):
    """Returns a fixed SQLGeneration regardless of the prompt, for testing the pipeline wiring
    in isolation from any real LLM call."""
    name = "stub"

    def __init__(self, sql: str):
        self._sql = sql

    def generate_structured(self, system, user, schema, *, temperature=0.0):
        return schema(sql=self._sql, tables_used=[], assumptions=[])

    def generate_text(self, system, user, *, temperature=0.2):
        return "stub answer"


@pytest.mark.parametrize("bad_sql", [
    "DROP TABLE dw.dim_customer",
    "SELECT * FROM dw.fact_sales; DROP TABLE dw.fact_sales",
    "SELECT * FROM public.customers",
    "SELECT email FROM dw.dim_customer",
])
def test_generate_sql_propagates_validator_rejection(bad_sql):
    provider = _StubProvider(bad_sql)
    with pytest.raises(NotSelectError):
        generate_sql("irrelevant question", provider, Settings(postgres_admin_password="x", postgres_readonly_password="x"))


def test_generate_sql_returns_limit_injected_safe_sql():
    provider = _StubProvider("SELECT customer_key FROM dw.dim_customer")
    settings = Settings(postgres_admin_password="x", postgres_readonly_password="x", max_rows=25)
    result = generate_sql("irrelevant question", provider, settings)
    assert isinstance(result, SQLGeneration)
    assert "LIMIT 25" in result.sql.upper()
