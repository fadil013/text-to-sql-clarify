"""Phase 4: the real SQL safety layer. SECURITY-CRITICAL -- review before trusting in production.

Defense in depth, in order. Any one of these alone is known to be insufficient (verified by hand
before relying on it, see progress.md):
  1. Parse with sqlglot. Reject anything that isn't exactly one SELECT statement.
  2. Explicit table allowlist: every table referenced must be `dw.<known table>`. This is the
     primary defense against cross-schema access -- sqlglot's column-qualifier alone does NOT
     catch `SELECT * FROM public.customers` (no column to resolve, so nothing to check).
  3. sqlglot's `qualify()` against a real schema dict: rejects hallucinated/unknown columns on
     any known table (this is what blocks "SELECT email FROM dw.dim_customer" even though no
     such column exists anywhere for the LLM to have legitimately seen).
  4. Explicit function blocklist: pg_sleep, file/lo_*, dblink, backend-control functions, etc.
  5. Explicit keyword blocklist as a final net (catches anything 1-4 missed, e.g. inside a CTE
     sqlglot's walkers don't visit the way we expect).
  6. Auto-LIMIT injection if the query has none.

The DB-level read-only role (Phase 1) and the single-SELECT guard (Phase 2) remain in place
underneath this as the last line of defense -- this module is "never trust the LLM's SQL" made
concrete, not a replacement for the role-based protection.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

DIALECT = "postgres"
ALLOWED_SCHEMA = "dw"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# Functions with no legitimate place in a read-only analytics query.
BLOCKED_FUNCTIONS = {
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_read", "lo_write",
    "dblink", "dblink_connect", "dblink_exec",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "copy_from_program", "pg_read_server_files",
}
# Disallowed regardless of context (catches CTE/subquery cases a tree-walk might miss).
BLOCKED_KEYWORDS = {
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "TRUNCATE", "CREATE", "GRANT", "REVOKE",
    "EXECUTE", "CALL", "COPY", "VACUUM", "REINDEX", "CLUSTER", "LOCK", "DO",
}
# Off-limits even though they technically exist and a naive allowlist miss could expose them.
BLOCKED_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast", "public"}
# Defense-in-depth: block these column name *patterns* anywhere, even if the schema ever grows
# a column like this by mistake later. The warehouse has none of these today (by design).
SUSPICIOUS_COLUMN_SUBSTRINGS = ("email", "phone", "password", "ssn", "ccn", "card_number",
                                "card_last4", "secret", "token", "api_key")


class ValidationError(ValueError):
    """Raised when a generated query fails any safety check. Never caught silently -- the
    pipeline must surface this as a refusal, not retry with a weaker check."""


@lru_cache
def _load_schema() -> dict[str, dict[str, str]]:
    """{table_name: {column_name: "TEXT"}} for every dw table, for sqlglot's qualifier.
    The actual type string doesn't matter to qualify() for existence checks; "TEXT" is a stub.
    """
    docs = yaml.safe_load((CONFIG_DIR / "schema_docs.yaml").read_text(encoding="utf-8"))
    return {table: {col: "TEXT" for col in spec["columns"]} for table, spec in docs["tables"].items()}


def _sqlglot_schema() -> dict[str, dict[str, dict[str, str]]]:
    return {ALLOWED_SCHEMA: _load_schema()}


def _check_single_select(sql: str) -> exp.Select:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        raise ValidationError(f"SQL failed to parse: {e}") from e
    if len(statements) != 1:
        raise ValidationError(f"exactly one statement is required, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise ValidationError(f"only SELECT statements are allowed, got {type(stmt).__name__}")
    return stmt


def _check_tables(stmt: exp.Select, allowed_tables: set[str]) -> None:
    for table in stmt.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = table.name.lower()
        if schema in BLOCKED_SCHEMAS or name in BLOCKED_SCHEMAS:
            raise ValidationError(f"access to schema '{schema or name}' is not allowed")
        if schema and schema != ALLOWED_SCHEMA:
            raise ValidationError(f"table '{schema}.{table.name}' is outside the '{ALLOWED_SCHEMA}' schema")
        if name not in allowed_tables:
            raise ValidationError(f"unknown table '{table.name}' (not in the {ALLOWED_SCHEMA} warehouse)")


def _check_columns(stmt: exp.Select) -> exp.Select:
    try:
        return qualify(stmt, schema=_sqlglot_schema(), dialect=DIALECT, validate_qualify_columns=True)
    except OptimizeError as e:
        raise ValidationError(f"unknown or unresolvable column: {e}") from e


def _check_functions(stmt: exp.Select) -> None:
    for func in stmt.find_all(exp.Func, exp.Anonymous):
        # exp.Anonymous (unrecognized-by-sqlglot functions, e.g. pg_sleep): the real name is in
        # .name. sql_name() returns the literal string "ANONYMOUS" for these -- NOT the function
        # name -- so it must not be checked first, or every Anonymous call silently passes.
        # Recognized Func subclasses (Count, Round, ...): .name is the first ARGUMENT, not the
        # function name; sql_name() is the canonical name there.
        if isinstance(func, exp.Anonymous):
            name = (func.name or "").lower()
        else:
            name = (func.sql_name() or "").lower()
        if name in BLOCKED_FUNCTIONS:
            raise ValidationError(f"function '{name}' is not allowed")


def _check_keywords(sql: str) -> None:
    # Tokenize rather than substring-match raw text, so this doesn't false-positive on an
    # identifier like "updated_at" while still catching a keyword hidden inside a CTE.
    for tok in sqlglot.tokenize(sql, read=DIALECT):
        if tok.text.upper() in BLOCKED_KEYWORDS:
            raise ValidationError(f"disallowed keyword '{tok.text.upper()}' found in query")


def _check_suspicious_columns(stmt: exp.Select) -> None:
    for col in stmt.find_all(exp.Column):
        name = col.name.lower()
        if any(bad in name for bad in SUSPICIOUS_COLUMN_SUBSTRINGS):
            raise ValidationError(f"column '{col.name}' looks like it may expose sensitive data")


def _inject_limit(stmt: exp.Select, max_rows: int) -> exp.Select:
    existing = stmt.args.get("limit")
    if existing is None:
        return stmt.limit(max_rows)
    try:
        current = int(existing.expression.this)
    except (AttributeError, ValueError, TypeError):
        return stmt  # non-literal LIMIT (unusual); leave it, DB-level row cap still applies
    return stmt if current <= max_rows else stmt.limit(max_rows)


def validate_and_prepare(sql: str, max_rows: int = 1000) -> str:
    """Raises ValidationError on anything unsafe. Returns a safe-to-execute SQL string
    (with LIMIT applied) otherwise. This is the only function the pipeline should call.
    """
    _check_keywords(sql)
    stmt = _check_single_select(sql)
    allowed_tables = set(_load_schema())
    _check_tables(stmt, allowed_tables)
    stmt = _check_columns(stmt)
    _check_functions(stmt)
    _check_suspicious_columns(stmt)
    stmt = _inject_limit(stmt, max_rows)
    return stmt.sql(dialect=DIALECT)
