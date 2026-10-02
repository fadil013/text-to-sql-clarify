"""Unit tests for JSON extraction/validation (no network, no API keys needed)."""
import json

import pytest
from pydantic import ValidationError

from app.llm.json_mode import parse_structured, schema_instructions
from app.schemas import SQLGeneration


def test_parses_plain_json():
    raw = json.dumps({"sql": "SELECT 1", "tables_used": [], "assumptions": []})
    result = parse_structured(raw, SQLGeneration)
    assert result.sql == "SELECT 1"


def test_parses_fenced_json():
    raw = '```json\n{"sql": "SELECT 1", "tables_used": ["dim_date"], "assumptions": []}\n```'
    result = parse_structured(raw, SQLGeneration)
    assert result.tables_used == ["dim_date"]


def test_rejects_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        parse_structured("not json at all", SQLGeneration)


def test_rejects_wrong_shape():
    with pytest.raises(ValidationError):
        parse_structured('{"wrong_field": 1}', SQLGeneration)


def test_schema_instructions_mentions_every_field():
    text = schema_instructions(SQLGeneration)
    assert "sql" in text and "tables_used" in text and "assumptions" in text
