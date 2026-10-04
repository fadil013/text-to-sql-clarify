"""Runs already-validated SQL on the read-only role. Nothing here validates: callers must have
passed the SQL through `app.validator.validate_and_prepare` first."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app.config import Settings


def run_sql(sql: str, settings: Settings) -> list[dict]:
    with psycopg.connect(settings.readonly_dsn, autocommit=True, row_factory=dict_row) as conn:
        rows = conn.execute(sql).fetchall()
    return rows[: settings.max_rows]
