"""Phase 6 self-repair tests.

Unit tests (no DB) pin which failures are repairable to the REAL validator's output, so a change to
a validator message can't silently turn a safety rejection into a retry. Integration tests run the
loop against the real read-only DB with a scripted (non-LLM) provider.
"""
import psycopg
import pytest
from psycopg import errors as pgerr

from app.clarification import AmbiguityAnalysis
from app.config import get_settings
from app.llm.provider import LLMError, LLMProvider
from app.pipeline import BLOCKED_REASON, answer_question, ask, fallback_answer
from app.repair import (
    RepairFailed,
    describe_db_error,
    execute_with_repair,
    is_repairable_db_error,
    is_repairable_validation_error,
)
from app.schemas import SQLGeneration
from app.validator import ValidationError, validate_and_prepare

GOOD = "SELECT COUNT(*) AS n FROM dw.dim_customer"
BAD_COLUMN = "SELECT COUNT(DISTINCT customer_uuid) AS n FROM dw.dim_customer"
BAD_COLUMN_2 = "SELECT COUNT(DISTINCT cust_id) AS n FROM dw.dim_customer"
BAD_TABLE = "SELECT COUNT(*) AS n FROM dw.dim_customers"
DIV_ZERO = "SELECT 1 / 0 AS boom FROM dw.dim_customer"


class SequenceProvider(LLMProvider):
    """Returns the scripted SQL strings in order (one per SQLGeneration call) and records prompts."""
    name = "sequence"

    def __init__(self, sqls, text="stub answer", text_error=None):
        self._sqls = list(sqls)
        self._text = text
        self._text_error = text_error
        self.structured_calls: list[tuple[str, str]] = []

    def generate_structured(self, system, user, schema, *, temperature=0.0):
        assert schema is SQLGeneration
        self.structured_calls.append((system, user))
        return SQLGeneration(sql=self._sqls.pop(0), tables_used=[], assumptions=["scripted"])

    def generate_text(self, system, user, *, temperature=0.2):
        if self._text_error:
            raise self._text_error
        return self._text


# ---------------------------------------------------------------- classification (no DB)


@pytest.mark.parametrize("sql", [BAD_COLUMN, BAD_TABLE, "SELEC COUNT(*) FROM dw.dim_customer"])
def test_mechanical_validator_errors_are_repairable(sql):
    with pytest.raises(ValidationError) as e:
        validate_and_prepare(sql)
    assert is_repairable_validation_error(e.value, sql), str(e.value)


@pytest.mark.parametrize("sql", [
    "DROP TABLE dw.dim_customer",
    "INSERT INTO dw.dim_customer (customer_key) VALUES (1)",
    "SELECT * FROM dw.dim_customer; DROP TABLE dw.fact_sales",
    "SELECT * FROM public.customers",
    "SELECT * FROM information_schema.tables",
    "SELECT pg_sleep(60)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM shop.dim_customer",
    "SELECT email FROM dw.dim_customer",  # reported as "unknown column", but it's a PII probe
    "SELECT phone_number, card_number FROM dw.dim_customer",
])
def test_safety_validator_errors_are_never_repairable(sql):
    with pytest.raises(ValidationError) as e:
        validate_and_prepare(sql)
    assert not is_repairable_validation_error(e.value, sql), str(e.value)


def test_unrecognized_validator_message_is_not_repairable():
    assert not is_repairable_validation_error(ValidationError("something brand new"))


def test_db_error_classification():
    assert is_repairable_db_error(pgerr.UndefinedColumn("x"))
    assert is_repairable_db_error(pgerr.SyntaxError("x"))
    assert is_repairable_db_error(pgerr.DivisionByZero("x"))
    assert is_repairable_db_error(pgerr.QueryCanceled("timeout"))
    assert not is_repairable_db_error(pgerr.InsufficientPrivilege("denied"))
    assert not is_repairable_db_error(psycopg.OperationalError("connection refused"))


def test_describe_db_error_is_truncated_and_has_no_dsn():
    text = describe_db_error(pgerr.UndefinedColumn("column \"x\" does not exist " + "y" * 2000))
    assert text.startswith("UndefinedColumn")
    assert len(text) <= 600
    assert "password" not in text.lower()


def test_fallback_answer_shapes():
    assert "no rows" in fallback_answer([])
    assert fallback_answer([{"n": 264}]) == "The result is 264."
    assert "3 row(s)" in fallback_answer([{"a": 1}, {"a": 2}, {"a": 3}])


# ---------------------------------------------------------------- the loop (real DB)

integration = pytest.mark.integration


@integration
def test_no_repair_needed_on_first_success():
    provider = SequenceProvider([GOOD])
    outcome = execute_with_repair("q", SQLGeneration(sql=GOOD), provider, get_settings())
    assert outcome.repairs == []
    assert outcome.rows[0]["n"] > 0
    assert provider.structured_calls == []  # no repair call was made


@integration
def test_hallucinated_column_is_repaired_and_error_is_fed_back():
    provider = SequenceProvider([GOOD])
    outcome = execute_with_repair("how many customers", SQLGeneration(sql=BAD_COLUMN), provider, get_settings())
    assert len(outcome.repairs) == 1
    assert outcome.repairs[0].stage == "validation"
    assert outcome.rows[0]["n"] > 0
    system, user = provider.structured_calls[0]
    assert "REPAIR MODE" in system
    assert "customer_uuid" in user  # the failed SQL and its error reach the model
    assert "unknown or unresolvable column" in user


@integration
def test_unknown_table_is_repaired():
    outcome = execute_with_repair("q", SQLGeneration(sql=BAD_TABLE), SequenceProvider([GOOD]), get_settings())
    assert len(outcome.repairs) == 1 and outcome.rows


@integration
def test_db_runtime_error_is_repaired():
    outcome = execute_with_repair("q", SQLGeneration(sql=DIV_ZERO), SequenceProvider([GOOD]), get_settings())
    assert len(outcome.repairs) == 1
    assert outcome.repairs[0].stage == "execution"
    assert "DivisionByZero" in outcome.repairs[0].error


@integration
def test_two_repairs_then_success():
    provider = SequenceProvider([BAD_COLUMN_2, GOOD])
    outcome = execute_with_repair("q", SQLGeneration(sql=BAD_COLUMN), provider, get_settings())
    assert len(outcome.repairs) == 2
    assert len(provider.structured_calls) == 2
    # the second repair prompt lists BOTH earlier failures
    assert "Attempt 1" in provider.structured_calls[1][1] and "Attempt 2" in provider.structured_calls[1][1]


@integration
def test_gives_up_after_max_repairs():
    settings = get_settings().model_copy(update={"max_repairs": 2})
    provider = SequenceProvider([BAD_COLUMN_2, BAD_TABLE, GOOD])  # GOOD must never be reached
    with pytest.raises(RepairFailed) as e:
        execute_with_repair("q", SQLGeneration(sql=BAD_COLUMN), provider, settings)
    assert len(e.value.attempts) == 3  # original + 2 repairs
    assert len(provider.structured_calls) == 2
    assert provider._sqls == [GOOD]


@integration
def test_repair_disabled_with_zero_max_repairs():
    settings = get_settings().model_copy(update={"max_repairs": 0})
    provider = SequenceProvider([GOOD])
    with pytest.raises(RepairFailed):
        execute_with_repair("q", SQLGeneration(sql=BAD_COLUMN), provider, settings)
    assert provider.structured_calls == []


@integration
def test_stops_early_when_model_repeats_a_failed_query():
    provider = SequenceProvider([BAD_COLUMN, GOOD])
    with pytest.raises(RepairFailed) as e:
        execute_with_repair("q", SQLGeneration(sql=BAD_COLUMN), provider, get_settings())
    assert len(e.value.attempts) == 1
    assert len(provider.structured_calls) == 1


@integration
def test_pii_probe_is_refused_not_repaired():
    provider = SequenceProvider([GOOD])
    with pytest.raises(ValidationError):
        execute_with_repair("q", SQLGeneration(sql="SELECT email FROM dw.dim_customer"), provider, get_settings())
    assert provider.structured_calls == []


@integration
@pytest.mark.parametrize("unsafe", [
    "DROP TABLE dw.dim_customer",
    "SELECT * FROM public.customers",
    "SELECT pg_sleep(30)",
    "SELECT * FROM dw.dim_customer; DELETE FROM dw.fact_sales",
])
def test_unsafe_first_query_is_never_repaired(unsafe):
    provider = SequenceProvider([GOOD])
    with pytest.raises(ValidationError):
        execute_with_repair("q", SQLGeneration(sql=unsafe), provider, get_settings())
    assert provider.structured_calls == []  # the model was never asked to "fix" it


@integration
@pytest.mark.parametrize("unsafe", [
    "DROP TABLE dw.dim_customer",
    "SELECT * FROM public.customers",
    "SELECT pg_read_file('/etc/passwd') AS x FROM dw.dim_customer",
])
def test_repaired_query_still_goes_through_the_validator(unsafe):
    """A first query that fails mechanically, then a 'repair' that tries something unsafe."""
    provider = SequenceProvider([unsafe])
    with pytest.raises(ValidationError):
        execute_with_repair("q", SQLGeneration(sql=BAD_COLUMN), provider, get_settings())
    assert len(provider.structured_calls) == 1


# ---------------------------------------------------------------- pipeline-level behaviour


@integration
def test_answer_question_reports_repairs_in_final_answer():
    provider = SequenceProvider([BAD_COLUMN, GOOD])
    # answer_question's first call is propose_sql -> BAD_COLUMN; the second is the repair -> GOOD
    result = answer_question("how many customers", provider=provider, settings=get_settings())
    assert result.row_count == 1
    assert len(result.repairs) == 1
    assert result.truncated is False


@integration
def test_ask_refuses_unsafe_sql_cleanly_instead_of_raising():
    analysis = AmbiguityAnalysis(status="clear", confidence=0.95)

    class P(SequenceProvider):
        def generate_structured(self, system, user, schema, *, temperature=0.0):
            if schema is AmbiguityAnalysis:
                return analysis
            return super().generate_structured(system, user, schema, temperature=temperature)

    result = ask("show me customers", provider=P(["SELECT * FROM public.customers"]), settings=get_settings())
    assert result.kind == "refuse"
    assert result.refusal_reason == BLOCKED_REASON


@integration
def test_ask_returns_error_result_when_repair_is_exhausted():
    analysis = AmbiguityAnalysis(status="clear", confidence=0.95)

    class P(SequenceProvider):
        def generate_structured(self, system, user, schema, *, temperature=0.0):
            if schema is AmbiguityAnalysis:
                return analysis
            return super().generate_structured(system, user, schema, temperature=temperature)

    result = ask("show me customers", provider=P([BAD_COLUMN, BAD_COLUMN_2, BAD_TABLE]), settings=get_settings())
    assert result.kind == "error"
    assert result.error_message


@integration
def test_ask_returns_error_result_when_llm_is_down():
    class Down(LLMProvider):
        name = "down"

        def generate_structured(self, *a, **k):
            raise LLMError("Groq HTTP 429: rate limited")

        def generate_text(self, *a, **k):
            raise LLMError("down")

    result = ask("show me something unusual", provider=Down(), settings=get_settings())
    assert result.kind == "error"
    assert "429" in result.error_message


@integration
def test_synthesis_failure_falls_back_instead_of_losing_the_result():
    provider = SequenceProvider([GOOD], text_error=LLMError("synthesis down"))
    result = answer_question("how many customers", provider=provider, settings=get_settings())
    assert result.answer.startswith("The result is ")
    assert result.rows_preview


@integration
def test_truncation_is_flagged_when_row_cap_is_hit():
    settings = get_settings().model_copy(update={"max_rows": 5})
    provider = SequenceProvider(["SELECT customer_key FROM dw.dim_customer"])
    result = answer_question("list customers", provider=provider, settings=settings)
    assert result.row_count == 5
    assert result.truncated is True
