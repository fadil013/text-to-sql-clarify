"""Swap providers with one config value (LLM_PROVIDER=gemini|groq|ollama)."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.llm.provider import LLMError, LLMProvider


def get_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    if s.llm_provider == "gemini":
        from app.llm.gemini import GeminiProvider
        return GeminiProvider(s.gemini_api_key, s.gemini_model)
    if s.llm_provider == "groq":
        from app.llm.groq import GroqProvider
        return GroqProvider(s.groq_api_key, s.groq_model)
    if s.llm_provider == "ollama":
        from app.llm.ollama import OllamaProvider
        return OllamaProvider(s.ollama_model, s.ollama_host)
    raise LLMError(f"Unknown LLM_PROVIDER: {s.llm_provider!r} (expected 'gemini', 'groq' or 'ollama')")
