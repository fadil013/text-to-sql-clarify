"""Structured outputs. Every LLM call must return one of these, Pydantic-validated. No free text."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SQLGeneration(BaseModel):
    sql: str = Field(description="A single read-only SELECT statement against the `dw` schema.")
    tables_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, description="Any interpretation choices made.")


class FinalAnswer(BaseModel):
    answer: str = Field(description="Plain-English answer to the user's question.")
    sql: str
    rows_preview: list[dict] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_involved: bool = False
