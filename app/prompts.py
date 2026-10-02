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


def _yaml_as_text(filename: str) -> str:
    """Re-serialize the YAML compactly so it's cheap in tokens but still readable by the model."""
    data = yaml.safe_load(_read(CONFIG_DIR / filename))
    return yaml.dump(data, sort_keys=False, allow_unicode=True, width=100)


def sql_generation_system_prompt() -> str:
    template = _read(PROMPTS_DIR / "sql_generation_v1.txt")
    return template.format(schema_docs=_yaml_as_text("schema_docs.yaml"), glossary=_yaml_as_text("glossary.yaml"))


def answer_synthesis_system_prompt() -> str:
    return _read(PROMPTS_DIR / "answer_synthesis_v1.txt")
