"""The pipeline: clarification gate (Phase 5) -> generate -> validate (Phase 4) -> execute on the
read-only role -> bounded self-repair on mechanical errors (Phase 6) -> answer synthesis.

Safety is layered: the Phase 4 validator (sqlglot: single SELECT, table/column allowlist, blocked
functions/keywords, auto-LIMIT) runs before any SQL reaches the database -- including every
repaired query -- with the Phase 1 read-only DB role as the backstop underneath it.

Entry points: `ask()` / `continue_after_clarification()` never raise for expected failures; they
return an `AskResult` (kind = answer | clarify | refuse | error). `answer_question()` is the old
no-clarification baseline path (used by the eval harness) and does raise.
"""
from __future__ import annotations

import json

import psycopg
from pydantic import BaseModel

from app.clarification import ClarificationRequest, Decision, SessionMemory, decide
from app.config import Settings, get_settings
from app.execution import run_sql  # noqa: F401  (re-exported: eval + tests import it from here)
from app.llm.factory import get_provider
from app.llm.provider import LLMError, LLMProvider
from app.prompts import answer_synthesis_system_prompt
from app.repair import RepairFailed, RepairOutcome, execute_with_repair, propose_sql
from app.schemas import FinalAnswer, SQLGeneration
from app.validator import ValidationError, validate_and_prepare

# Kept as an alias: existing call sites (and earlier phases' tests) catch NotSelectError. The
# validator now rejects far more than "not a SELECT", but the alias avoids a noisy rename.
NotSelectError = ValidationError

PREVIEW_ROWS = 20


def generate_sql(question: str, provider: LLMProvider, settings: Settings | None = None) -> SQLGeneration:
    """Generate + validate, with no execution and no repair. Raises ValidationError if unsafe."""
    settings = settings or get_settings()
    result = propose_sql(question, provider)
    safe_sql = validate_and_prepare(result.sql, max_rows=settings.max_rows)
    return result.model_copy(update={"sql": safe_sql})


def synthesize_answer(question: str, sql: str, rows: list[dict], provider: LLMProvider,
                      max_rows: int | None = None) -> str:
    system = answer_synthesis_system_prompt()
    preview = json.dumps(rows[:PREVIEW_ROWS], default=str)
    lines = [f"Question: {question}", f"Row count: {len(rows)}"]
    if max_rows is not None and len(rows) >= max_rows:
        lines.append(f"The result was truncated at the {max_rows}-row cap; more rows may exist.")
    if len(rows) > PREVIEW_ROWS:
        lines.append(f"Showing the first {PREVIEW_ROWS} of {len(rows)} rows.")
    lines.append(f"Rows (JSON): {preview}")
    return provider.generate_text(system, "\n".join(lines)).strip()


def fallback_answer(rows: list[dict]) -> str:
    """Deterministic answer used only when the synthesis LLM call fails, so a query that already
    ran successfully is never thrown away over a failed summary."""
    if not rows:
        return "The query ran successfully but returned no rows."
    if len(rows) == 1 and len(rows[0]) == 1:
        return f"The result is {next(iter(rows[0].values()))}."
    return f"The query returned {len(rows)} row(s); see the table below."


def _build_final(question: str, outcome: RepairOutcome, provider: LLMProvider, settings: Settings,
                 assumptions: list[str], clarification_involved: bool) -> FinalAnswer:
    rows = outcome.rows
    try:
        text = synthesize_answer(question, outcome.generation.sql, rows, provider, settings.max_rows)
    except LLMError:
        text = fallback_answer(rows)
    return FinalAnswer(
        answer=text or fallback_answer(rows),
        sql=outcome.generation.sql,
        rows_preview=rows[:PREVIEW_ROWS],
        assumptions=assumptions,
        clarification_involved=clarification_involved,
        row_count=len(rows),
        truncated=len(rows) >= settings.max_rows,
        repairs=outcome.repairs,
    )


def _generate_and_run(question: str, provider: LLMProvider, settings: Settings) -> RepairOutcome:
    first = propose_sql(question, provider)
    return execute_with_repair(question, first, provider, settings)


def answer_question(question: str, provider: LLMProvider | None = None, settings: Settings | None = None) -> FinalAnswer:
    """Baseline path with no clarification gate. Raises on failure (ValidationError, RepairFailed,
    LLMError, psycopg.Error); use `ask()` for the never-raises entry point."""
    settings = settings or get_settings()
    provider = provider or get_provider(settings)
    outcome = _generate_and_run(question, provider, settings)
    return _build_final(question, outcome, provider, settings,
                        list(outcome.generation.assumptions), clarification_involved=False)


class AskResult(BaseModel):
    """What `ask()` returns: `kind` selects which field is meaningful.
      answer   -> `answer`
      clarify  -> `clarification`
      refuse   -> `refusal_reason` (out of scope, or blocked by the safety checks)
      error    -> `error_message` (model unavailable, repair exhausted, DB trouble)
    This is the one entry point Phase 8's API/frontend should call."""
    kind: str  # "answer" | "clarify" | "refuse" | "error"
    answer: FinalAnswer | None = None
    clarification: ClarificationRequest | None = None
    refusal_reason: str = ""
    error_message: str = ""


BLOCKED_REASON = "The query that was generated for this request was blocked by the safety checks, so nothing was run."


def _failure_result(exc: Exception) -> AskResult:
    """Turn an expected failure into a clean result. Anything unexpected is a bug and propagates."""
    if isinstance(exc, ValidationError):
        return AskResult(kind="refuse", refusal_reason=BLOCKED_REASON)
    if isinstance(exc, RepairFailed):
        return AskResult(kind="error", error_message=(
            "I couldn't produce a query that runs against this data, even after retrying. "
            "Try rephrasing the question."))
    if isinstance(exc, LLMError):
        return AskResult(kind="error", error_message=f"The language model is unavailable right now: {exc}")
    if isinstance(exc, psycopg.Error):
        return AskResult(kind="error", error_message="The database rejected the query.")
    raise exc


_EXPECTED_FAILURES = (ValidationError, RepairFailed, LLMError, psycopg.Error)


def ask(question: str, provider: LLMProvider | None = None, settings: Settings | None = None,
        memory: SessionMemory | None = None) -> AskResult:
    """Runs the clarification decision gate first, then (only if the gate says to) the
    generate -> validate -> execute (-> repair) -> synthesize pipeline."""
    settings = settings or get_settings()
    provider = provider or get_provider(settings)
    memory = memory or SessionMemory()
    try:
        decision = decide(question, provider, memory)
        return _act_on_decision(question, decision, provider, settings)
    except _EXPECTED_FAILURES as e:
        return _failure_result(e)


def continue_after_clarification(original_question: str, ambiguity_type: str, user_choice: str,
                                  provider: LLMProvider | None = None, settings: Settings | None = None,
                                  memory: SessionMemory | None = None) -> AskResult:
    """Call after the user answers a ClarificationRequest returned by ask()."""
    from app.clarification import resolve_with_answer

    settings = settings or get_settings()
    provider = provider or get_provider(settings)
    memory = memory or SessionMemory()
    memory.rounds_this_question += 1

    resolved = resolve_with_answer(original_question, ambiguity_type, user_choice, memory)
    decision = Decision(kind="proceed", resolved_question=resolved)
    try:
        return _act_on_decision(resolved, decision, provider, settings, clarification_involved=True)
    except _EXPECTED_FAILURES as e:
        return _failure_result(e)


def _act_on_decision(question: str, decision: Decision, provider: LLMProvider, settings: Settings,
                      clarification_involved: bool = False) -> AskResult:
    if decision.kind == "refuse":
        return AskResult(kind="refuse", refusal_reason=decision.refusal_reason)
    if decision.kind == "clarify":
        return AskResult(kind="clarify", clarification=decision.clarification)

    # "proceed" or "assume": run generate -> validate -> execute (-> repair) on the resolved question
    outcome = _generate_and_run(decision.resolved_question, provider, settings)
    assumptions = list(outcome.generation.assumptions)
    if decision.kind == "assume" and decision.labeled_assumption:
        assumptions.append(decision.labeled_assumption)
    final = _build_final(question, outcome, provider, settings, assumptions,
                         clarification_involved=clarification_involved or decision.kind == "assume")
    return AskResult(kind="answer", answer=final)
