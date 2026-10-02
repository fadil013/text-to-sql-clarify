"""Loads prompt templates and the schema/glossary context they're filled with."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
CONFIG_DIR = ROOT / "config"


@lru_cache
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compact_schema_docs() -> str:
    """One terse line per table, one per column -- a few hundred tokens instead of ~2-3k of YAML
    prose. Keeps every gotcha (that's where the real accuracy signal is) but drops description
    padding the model doesn't need to write correct SQL.
    """
    data = yaml.safe_load(_read(CONFIG_DIR / "schema_docs.yaml"))
    lines = [f"schema: {data['schema']}"]
    for table, spec in data["tables"].items():
        cols = ", ".join(spec["columns"].keys())
        lines.append(f"- {table} ({spec.get('kind', 'table')}): {cols}")
        for g in spec.get("gotchas", []):
            lines.append(f"    ! {g}")
    return "\n".join(lines)


def _compact_glossary() -> str:
    """One line per term: 'term = definition'. Drops YAML structure overhead."""
    data = yaml.safe_load(_read(CONFIG_DIR / "glossary.yaml"))
    lines = [f"{term} = {spec['definition'].strip()}" for term, spec in data["terms"].items()]
    lines.append("")
    for k, v in data.get("time_conventions", {}).items():
        lines.append(f"time: {k} = {v}")
    lines.append(f"defaults: {data.get('defaults', {})}")
    return "\n".join(lines)


def sql_generation_system_prompt() -> str:
    template = _read(PROMPTS_DIR / "sql_generation_v1.txt")
    return template.format(schema_docs=_compact_schema_docs(), glossary=_compact_glossary())


def answer_synthesis_system_prompt() -> str:
    return _read(PROMPTS_DIR / "answer_synthesis_v1.txt")
