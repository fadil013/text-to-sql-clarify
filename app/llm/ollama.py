"""Ollama provider: a local model, so no API quota, no rate limits, no data leaving the machine.

Uses Ollama's native /api/chat because its `format` field takes a JSON schema and constrains
decoding to it -- far more reliable for a small (7-8B) model than asking nicely for JSON.
`num_ctx` MUST be raised: Ollama's default context is small and would silently truncate our
~4-5k-token schema + glossary prompts, which looks like "the model ignores the schema".
"""
from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from app.llm.json_mode import parse_structured, schema_instructions
from app.llm.provider import LLMError, LLMProvider, T

_NUM_CTX = 12288          # prompt (~5k) + reply, with headroom
_KEEP_ALIVE = "30m"       # keep the model loaded between calls
_TIMEOUT_S = 300.0        # local generation on a laptop GPU can take a while


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str, host: str = "http://127.0.0.1:11434", timeout: float = _TIMEOUT_S,
                 client: httpx.Client | None = None):
        self._model = model
        self._url = f"{host.rstrip('/')}/api/chat"
        self._client = client or httpx.Client(timeout=timeout)

    def _call(self, system: str, user: str, temperature: float, *, fmt: dict | str | None) -> str:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "think": False,  # reasoning models: no hidden thinking tokens in the reply, and faster
            "keep_alive": _KEEP_ALIVE,
            "options": {"temperature": temperature, "num_ctx": _NUM_CTX},
        }
        if fmt is not None:
            body["format"] = fmt
        try:
            resp = self._client.post(self._url, json=body)
        except httpx.ConnectError as e:
            raise LLMError("Cannot reach Ollama at " + self._url + " -- is it running? (start the Ollama app "
                           "or `ollama serve`)") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Ollama timed out after {_TIMEOUT_S:.0f}s") from e
        if resp.status_code != 200:
            raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["message"]["content"]
        except (KeyError, ValueError) as e:
            raise LLMError(f"Ollama returned no text: {resp.text[:300]}") from e

    def generate_structured(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        sys_prompt = f"{system}\n\n{schema_instructions(schema)}"
        last_err: Exception | None = None
        for attempt in range(2):
            prompt = user if attempt == 0 else f"{user}\n\nYour previous reply was invalid: {last_err}\nFix it."
            raw = self._call(sys_prompt, prompt, temperature, fmt=schema.model_json_schema())
            try:
                return parse_structured(raw, schema)
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
        raise LLMError(f"Ollama: invalid structured output after retry: {last_err}")

    def generate_text(self, system: str, user: str, *, temperature: float = 0.2) -> str:
        return self._call(system, user, temperature, fmt=None)
