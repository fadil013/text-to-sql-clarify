"""Provider-agnostic interface. Every provider returns a Pydantic-validated structured output."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when a provider call fails or returns output that doesn't validate after a retry."""


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def generate_structured(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        """Call the model and return `schema`-validated output. Retries once on bad JSON/validation."""

    @abstractmethod
    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        """Call the model and return plain text (used for the natural-language answer)."""
