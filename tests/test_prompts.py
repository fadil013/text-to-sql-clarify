"""Unit tests for prompt assembly (no network needed)."""
from app.prompts import answer_synthesis_system_prompt, sql_generation_system_prompt


def test_sql_prompt_embeds_schema_and_glossary():
    text = sql_generation_system_prompt()
    assert "fact_sales" in text
    assert "dim_customer" in text
    assert "revenue" in text
    assert "{schema_docs}" not in text and "{glossary}" not in text  # placeholders were filled


def test_answer_prompt_loads():
    text = answer_synthesis_system_prompt()
    assert text.strip()
