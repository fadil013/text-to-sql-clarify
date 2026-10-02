"""Google Gemini provider (free tier, AI Studio key). REST API, no SDK dependency."""
from __future__ import annotations

import json
import time

import httpx
from pydantic import BaseModel, ValidationError

from app.llm.json_mode import parse_structured, schema_instructions
from app.llm.provider import LLMError, LLMProvider, T
from app.llm.rate_limit import RateLimiter

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRY_STATUSES = {429, 503}
_MAX_SERVER_RETRIES = 3
_BACKOFF_SECONDS = 2.0
_MIN_INTERVAL_S = 4.0  # conservative: free-tier Flash limits are tighter and quota is precious


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self._key = api_key
        self._model = model
        self._client = httpx.Client(timeout=timeout)
        self._limiter = RateLimiter(_MIN_INTERVAL_S)

    def _call(self, system: str, user: str, temperature: float, *, json_mode: bool) -> str:
        url = f"{_BASE}/{self._model}:generateContent?key={self._key}"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        resp = None
        for attempt in range(_MAX_SERVER_RETRIES + 1):
            self._limiter.wait()
            resp = self._client.post(url, json=body)
            if resp.status_code not in _RETRY_STATUSES or attempt == _MAX_SERVER_RETRIES:
                break
            time.sleep(_BACKOFF_SECONDS * (2**attempt))  # 2s, 4s, 8s
        if resp.status_code != 200:
            raise LLMError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Gemini returned no text: {data}") from e

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
        raise LLMError(f"Gemini: invalid structured output after retry: {last_err}")

    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        return self._call(system, user, temperature, json_mode=False)
