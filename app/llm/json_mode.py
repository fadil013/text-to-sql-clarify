"""Shared helpers for turning a free-text JSON reply into a validated Pydantic object."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def schema_instructions(schema: type[BaseModel]) -> str:
    """A compact instruction block describing the exact JSON shape the model must output."""
    fields = schema.model_json_schema().get("properties", {})
    lines = [f'  "{name}": {spec.get("type", "any")}' for name, spec in fields.items()]
    return (
        f"Respond with ONLY a single JSON object (no prose, no markdown fences) matching exactly:\n"
        f"{{\n" + ",\n".join(lines) + "\n}}"
    )


def parse_structured(raw: str, schema: type[T]) -> T:
    """Extract JSON from a model reply (tolerating ```json fences) and validate it."""
    text = raw.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    data = json.loads(text)  # raises json.JSONDecodeError -> caller retries
    return schema.model_validate(data)  # raises ValidationError -> caller retries


__all__ = ["schema_instructions", "parse_structured", "ValidationError"]
