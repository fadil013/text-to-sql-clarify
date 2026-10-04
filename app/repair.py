"""Phase 6: bounded self-repair.

propose SQL -> validate (Phase 4) -> execute (read-only role). If that fails with a *mechanical*
error (hallucinated column/table, syntax error, a DB data/type error, a timeout), feed the exact
error back to the model and try again, at most `settings.max_repairs` times. Every repaired query
goes through the full validator again -- repair never weakens a check.

What is deliberately NOT repairable (re-raised untouched, so the caller refuses instead):
  * validator rejections that are safety signals (non-SELECT, multi-statement, blocked function,
    blocked keyword, off-limits schema, PII-looking column). Looping the model on those would only
    teach an attacker's prompt to iterate toward a bypass.
  * DB permission errors (the read-only role said no) and connection failures.

The validator's rejection messages are matched by prefix below. That coupling is intentional and
fails safe (an unrecognized message is NOT repairable); tests/test_repair.py pins it to the real
validator output so a message change can't silently turn a safety rejection into a retry.
"""
from __future__ import annotations

import psycopg
from psycopg import errors as pgerr

from app.config import Settings
from app.execution import run_sql
from app.llm.provider import LLMProvider
from app.prompts import sql_generation_system_prompt, sql_repair_system_prompt
from app.schemas import RepairAttempt, SQLGeneration
from app.validator import SUSPICIOUS_COLUMN_SUBSTRINGS, ValidationError, validate_and_prepare

# Validator messages (app/validator.py) that mean "the model got a name/syntax wrong", not "the
# model tried something unsafe".
_REPAIRABLE_VALIDATION_PREFIXES = (
    "SQL failed to parse",
    "unknown or unresolvable column",
    "unknown table",
)
_MAX_ERROR_CHARS = 600


class RepairFailed(RuntimeError):
    """Every repair attempt failed. `attempts` holds each failed query and its error."""

    def __init__(self, attempts: list[RepairAttempt]):
        self.attempts = attempts
        last = attempts[-1].error if attempts else "unknown error"
        super().__init__(f"Could not produce a working query after {len(attempts)} attempt(s): {last}")


class RepairOutcome:
    """A query that ran: the final (validated) generation, its rows, and the failures on the way."""

    def __init__(self, generation: SQLGeneration, rows: list[dict], repairs: list[RepairAttempt]):
        self.generation = generation
        self.rows = rows
        self.repairs = repairs


def is_repairable_validation_error(exc: ValidationError, sql: str = "") -> bool:
    """True only for name/syntax mistakes. A rejection for an unknown column is NOT repairable when
    the SQL reaches for a sensitive-looking name (email, phone, password...): the validator reports
    that as "unknown column" (the warehouse has no such column), but it is a PII probe, so the
    right response is to refuse, not to coach the model toward another guess."""
    if not str(exc).startswith(_REPAIRABLE_VALIDATION_PREFIXES):
        return False
    lowered = sql.lower()
    return not any(bad in lowered for bad in SUSPICIOUS_COLUMN_SUBSTRINGS)


def is_repairable_db_error(exc: psycopg.Error) -> bool:
    if isinstance(exc, pgerr.InsufficientPrivilege):
        return False  # the read-only role blocked it: a safety signal, never "fix and retry"
    if isinstance(exc, pgerr.QueryCanceled):
        return True  # statement timeout: ask for a cheaper query
    if isinstance(exc, psycopg.OperationalError):
        return False  # connection/server trouble: retrying a different query won't help
    return isinstance(exc, (psycopg.ProgrammingError, psycopg.DataError))


def describe_db_error(exc: psycopg.Error) -> str:
    """The DB's own primary message + hint (no stack trace, no connection details)."""
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) or str(exc)
    hint = getattr(diag, "message_hint", None)
    text = f"{type(exc).__name__}: {primary}" + (f" (hint: {hint})" if hint else "")
    return text[:_MAX_ERROR_CHARS]


def propose_sql(question: str, provider: LLMProvider) -> SQLGeneration:
    """First-shot generation. Returns the model's SQL UNVALIDATED -- callers must validate it."""
    return provider.generate_structured(
        sql_generation_system_prompt(), f"Question: {question}", SQLGeneration
    )


def repair_sql(question: str, attempts: list[RepairAttempt], provider: LLMProvider) -> SQLGeneration:
    history = "\n\n".join(
        f"Attempt {i} ({a.stage} error)\nSQL:\n{a.sql}\nError:\n{a.error}"
        for i, a in enumerate(attempts, 1)
    )
    user = f"Question: {question}\n\nFailed attempts, oldest first:\n{history}\n\nReturn a corrected query."
    return provider.generate_structured(sql_repair_system_prompt(), user, SQLGeneration)


def execute_with_repair(question: str, first: SQLGeneration, provider: LLMProvider,
                        settings: Settings) -> RepairOutcome:
    """Validate + run `first`; on a repairable failure ask the model to fix it, up to
    `settings.max_repairs` times. Raises ValidationError / psycopg.Error untouched for anything
    unrepairable, RepairFailed when the retries run out."""
    candidate = first
    attempts: list[RepairAttempt] = []

    while True:
        try:
            safe_sql = validate_and_prepare(candidate.sql, max_rows=settings.max_rows)
        except ValidationError as e:
            if not is_repairable_validation_error(e, candidate.sql):
                raise
            attempt = RepairAttempt(sql=candidate.sql, error=str(e)[:_MAX_ERROR_CHARS], stage="validation")
        else:
            try:
                rows = run_sql(safe_sql, settings)
            except psycopg.Error as e:
                if not is_repairable_db_error(e):
                    raise
                attempt = RepairAttempt(sql=safe_sql, error=describe_db_error(e), stage="execution")
            else:
                return RepairOutcome(candidate.model_copy(update={"sql": safe_sql}), rows, attempts)

        attempts.append(attempt)
        if len(attempts) > settings.max_repairs:
            raise RepairFailed(attempts)

        candidate = repair_sql(question, attempts, provider)
        if any(_same_sql(candidate.sql, a.sql) for a in attempts):
            # the model resubmitted a query that already failed: more retries would only burn quota
            raise RepairFailed(attempts)


def _same_sql(a: str, b: str) -> bool:
    return " ".join(a.split()).rstrip(";").lower() == " ".join(b.split()).rstrip(";").lower()


__all__ = [
    "RepairAttempt", "RepairFailed", "RepairOutcome", "describe_db_error",
    "execute_with_repair", "is_repairable_db_error", "is_repairable_validation_error",
    "propose_sql", "repair_sql",
]
