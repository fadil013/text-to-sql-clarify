"""Structured outputs. Every LLM call must return one of these, Pydantic-validated. No free text."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SQLGeneration(BaseModel):
    sql: str = Field(description="A single read-only SELECT statement against the `dw` schema.")
    tables_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, description="Any interpretation choices made.")


class RepairAttempt(BaseModel):
    """One failed query and why it failed. Built by code (Phase 6), never by the LLM."""
    sql: str
    error: str
    stage: str = Field(description="'validation' (rejected before the DB) or 'execution' (DB error)")


class FinalAnswer(BaseModel):
    answer: str = Field(description="Plain-English answer to the user's question.")
    sql: str
    rows_preview: list[dict] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_involved: bool = False
    row_count: int = 0
    truncated: bool = Field(default=False, description="True if the result hit the row cap.")
    repairs: list[RepairAttempt] = Field(
        default_factory=list, description="Failed attempts that self-repair recovered from."
    )
