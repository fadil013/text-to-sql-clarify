# CLAUDE.md

Read `progress.md` first: it holds the full plan, principles, phases, gates and status.

## Non-negotiables
- Work one phase at a time; do not start the next until the gate passes and the user approves.
- No phase is done until its tests pass.
- Never trust LLM SQL: every query goes through the validator, then a read-only DB role.
- All LLM outputs are Pydantic-validated structured output. Fail safe: ask or refuse, never guess.
- Prompts live in `prompts/` as versioned files. Secrets live in `.env` only.
- Validator and DB role changes are security-critical: flag them for user review.
- Update the status checklist in `progress.md` when a phase completes.

## Data model
Source OLTP tables live in `public` (admin only, has PII). The LLM/app only sees the `dw` star+snowflake warehouse (see `config/schema_docs.yaml`). Rebuild everything: `docker compose down -v; docker compose up -d; python -m db.seed`.

## Stack
Python 3.11+, PostgreSQL (Docker Compose), Pydantic v2, sqlglot, psycopg 3, FastAPI, Streamlit, pytest, Faker. Free LLMs only, behind a provider abstraction.

## Commands
- Setup: `python -m venv .venv` then `.venv\Scripts\activate` then `pip install -e ".[dev]"`
- Tests: `pytest`
