"""Counts LLM calls and estimates tokens (chars/4) for the eval's cost numbers. Free-tier APIs do
not all report usage, so tokens are an ESTIMATE, labeled as such in every report."""
from __future__ import annotations

from app.llm.provider import LLMProvider, T


class CountingProvider(LLMProvider):
    def __init__(self, inner: LLMProvider):
        self._inner = inner
        self.name = inner.name
        self.calls = 0
        self.chars_in = 0
        self.chars_out = 0

    def generate_structured(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        self.calls += 1
        self.chars_in += len(system) + len(user)
        result = self._inner.generate_structured(system, user, schema, temperature=temperature)
        self.chars_out += len(result.model_dump_json())
        return result

    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        self.calls += 1
        self.chars_in += len(system) + len(user)
        text = self._inner.generate_text(system, user, temperature=temperature)
        self.chars_out += len(text)
        return text

    def snapshot(self) -> tuple[int, int]:
        return self.calls, (self.chars_in + self.chars_out) // 4

    def __getattr__(self, item):  # expose _model etc. of the wrapped provider
        return getattr(self._inner, item)
