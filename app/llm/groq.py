"""Groq provider (free tier, OpenAI-compatible REST API)."""
from __future__ import annotations

import json
import time

import httpx
from pydantic import ValidationError

from app.llm.json_mode import parse_structured, schema_instructions
from app.llm.provider import LLMError, LLMProvider, T

_URL = "https://api.groq.com/openai/v1/chat/completions"
_RETRY_STATUSES = {429, 503}
_MAX_SERVER_RETRIES = 3
_BACKOFF_SECONDS = 2.0


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set")
        self._key = api_key
        self._model = model
        self._client = httpx.Client(timeout=timeout)

    def _call(self, system: str, user: str, temperature: float, *, json_mode: bool) -> str:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = None
        for attempt in range(_MAX_SERVER_RETRIES + 1):
            resp = self._client.post(_URL, headers={"Authorization": f"Bearer {self._key}"}, json=body)
            if resp.status_code not in _RETRY_STATUSES or attempt == _MAX_SERVER_RETRIES:
                break
            time.sleep(_BACKOFF_SECONDS * (2**attempt))  # 2s, 4s, 8s
        if resp.status_code != 200:
            raise LLMError(f"Groq HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Groq returned no text: {data}") from e

    def generate_structured(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        sys_prompt = f"{system}\n\n{schema_instructions(schema)}"
        last_err: Exception | None = None
        for attempt in range(2):
            prompt = user if attempt == 0 else f"{user}\n\nYour previous reply was invalid JSON: {last_err}\nFix it."
            raw = self._call(sys_prompt, prompt, temperature, json_mode=True)
            try:
                return parse_structured(raw, schema)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
        raise LLMError(f"Groq: invalid structured output after retry: {last_err}")

    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        return self._call(system, user, temperature, json_mode=False)
