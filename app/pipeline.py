"""Phase 2 baseline pipeline: question -> SQL -> execute (read-only) -> plain-English answer.

No clarification, no sqlglot validator yet (Phases 4-5). The only safety net right now is the
read-only DB role from Phase 1: it can only SELECT from `dw`, with a statement timeout.
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


class NotSelectError(ValueError):
    """Raised when the model returns anything other than a single SELECT. Real validation is Phase 4."""


def _guard_select_only(sql: str) -> None:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise NotSelectError("multiple statements are not allowed")
    if not s[:6].upper().startswith("SELECT"):
        raise NotSelectError("only SELECT statements are allowed")


def generate_sql(question: str, provider: LLMProvider) -> SQLGeneration:
    system = sql_generation_system_prompt()
    result = provider.generate_structured(system, f"Question: {question}", SQLGeneration)
    _guard_select_only(result.sql)
    return result


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

    sql_gen = generate_sql(question, provider)
    rows = run_sql(sql_gen.sql, settings)
    answer_text = synthesize_answer(question, sql_gen.sql, rows, provider)

    return FinalAnswer(
        answer=answer_text,
        sql=sql_gen.sql,
        rows_preview=rows[:20],
        assumptions=sql_gen.assumptions,
        clarification_involved=False,
    )
