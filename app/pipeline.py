"""Baseline pipeline: question -> SQL -> validate (Phase 4) -> execute (read-only) -> answer.

No clarification yet (Phase 5). Safety is now layered: the Phase 4 validator (sqlglot-based:
single SELECT, table/column allowlist, blocked functions/keywords, auto-LIMIT) runs before any
SQL reaches the database, with the Phase 1 read-only DB role as the backstop underneath it.
"""
from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.llm.factory import get_provider
from app.llm.provider import LLMProvider
from app.prompts import answer_synthesis_system_prompt, sql_generation_system_prompt
from app.schemas import FinalAnswer, SQLGeneration
from app.validator import ValidationError, validate_and_prepare

# Kept as an alias: existing call sites (and earlier phases' tests) catch NotSelectError. The
# validator now rejects far more than "not a SELECT", but the alias avoids a noisy rename.
NotSelectError = ValidationError


def generate_sql(question: str, provider: LLMProvider, settings: Settings | None = None) -> SQLGeneration:
    settings = settings or get_settings()
    system = sql_generation_system_prompt()
    result = provider.generate_structured(system, f"Question: {question}", SQLGeneration)
    safe_sql = validate_and_prepare(result.sql, max_rows=settings.max_rows)
    return result.model_copy(update={"sql": safe_sql})


def run_sql(sql: str, settings: Settings) -> list[dict]:
    with psycopg.connect(settings.readonly_dsn, autocommit=True, row_factory=dict_row) as conn:
        rows = conn.execute(sql).fetchall()
    return rows[: settings.max_rows]


def synthesize_answer(question: str, sql: str, rows: list[dict], provider: LLMProvider) -> str:
    system = answer_synthesis_system_prompt()
    preview = json.dumps(rows[:20], default=str)
    user = f"Question: {question}\nRow count: {len(rows)}\nRows (JSON, up to 20): {preview}"
    return provider.generate_text(system, user).strip()


def answer_question(question: str, provider: LLMProvider | None = None, settings: Settings | None = None) -> FinalAnswer:
    settings = settings or get_settings()
    provider = provider or get_provider(settings)

    sql_gen = generate_sql(question, provider, settings)
    rows = run_sql(sql_gen.sql, settings)
    answer_text = synthesize_answer(question, sql_gen.sql, rows, provider)

    return FinalAnswer(
        answer=answer_text,
        sql=sql_gen.sql,
        rows_preview=rows[:20],
        assumptions=sql_gen.assumptions,
        clarification_involved=False,
    )
