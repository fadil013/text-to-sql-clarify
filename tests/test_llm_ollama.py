"""Ollama provider request/response handling via httpx's mock transport (no Ollama needed)."""
import json

import httpx
import pytest

from app.llm.ollama import OllamaProvider
from app.llm.provider import LLMError
from app.schemas import SQLGeneration


def provider(handler, **kw):
    return OllamaProvider("qwen3:8b", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def reply(content):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_structured_request_shape_and_parse():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply('{"sql": "SELECT 1", "tables_used": [], "assumptions": ["a"]}')

    out = provider(handler).generate_structured("sys", "user", SQLGeneration)
    assert out.sql == "SELECT 1"
    assert seen["options"]["num_ctx"] >= 8192          # default ctx would truncate our prompts
    assert seen["think"] is False and seen["stream"] is False
    assert seen["format"]["properties"]["sql"]          # schema-constrained decoding
    assert seen["model"] == "qwen3:8b" and seen["options"]["temperature"] == 0.0


def test_invalid_json_is_retried_once_then_fails():
    calls = []

    def handler(request):
        calls.append(1)
        return reply("not json")

    with pytest.raises(LLMError):
        provider(handler).generate_structured("s", "u", SQLGeneration)
    assert len(calls) == 2


def test_text_call_sends_no_format():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return reply("hello")

    assert provider(handler).generate_text("s", "u") == "hello"
    assert "format" not in seen


def test_http_error_and_connection_error_become_llm_errors():
    with pytest.raises(LLMError, match="HTTP 404"):
        provider(lambda r: httpx.Response(404, text="model not found")).generate_text("s", "u")

    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="Cannot reach Ollama"):
        provider(refuse).generate_text("s", "u")
