"""File-based cache wrapping an LLMProvider, so re-running the eval doesn't re-burn free-tier quota.

Keyed on (provider name, model, system prompt, user prompt, response shape). Cache files are
local scratch data, not reproducible build output, so eval/.cache/ is git-ignored.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.llm.provider import LLMProvider

T = TypeVar("T", bound=BaseModel)

CACHE_DIR = Path(__file__).resolve().parent / ".cache"


class CachingProvider(LLMProvider):
    """Decorates a real provider: identical calls after the first are served from disk."""

    def __init__(self, inner: LLMProvider, cache_dir: Path = CACHE_DIR):
        self.name = f"{inner.name}+cache"
        self._inner = inner
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key(self, kind: str, system: str, user: str, extra: str) -> Path:
        h = hashlib.sha256(f"{kind}|{self._inner.name}|{extra}|{system}|{user}".encode()).hexdigest()
        return self._dir / f"{h}.json"

    def generate_structured(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        path = self._key("structured", system, user, schema.__name__)
        if path.exists():
            return schema.model_validate_json(path.read_text(encoding="utf-8"))
        result = self._inner.generate_structured(system, user, schema, temperature=temperature)
        path.write_text(result.model_dump_json(), encoding="utf-8")
        return result

    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        path = self._key("text", system, user, "")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        text = self._inner.generate_text(system, user, temperature=temperature)
        path.write_text(json.dumps({"text": text}), encoding="utf-8")
        return text
