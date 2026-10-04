"""Phase 5: the clarification engine -- the project's actual differentiator.

Flow: glossary short-circuit (free, no LLM call, instant) -> ambiguity analysis (one LLM call) ->
decision gate -> either proceed to SQL generation, return a ClarificationRequest, or refuse.
Session memory remembers a resolved choice within a session so the same ambiguity isn't asked
twice. Capped at one clarification round per question (two absolute max, per the plan).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.llm.provider import LLMProvider

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MAX_CLARIFICATION_ROUNDS = 2


# ---------------------------------------------------------------- structured outputs


class AmbiguityAnalysis(BaseModel):
    status: str = Field(description="one of: clear, ambiguous, out_of_scope")
    ambiguity_type: str = Field(default="", description="metric|time|entity|scope|missing_param|vague_term, or empty if clear")
    interpretations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasoning: str = ""
    refusal_reason: str = Field(default="", description="set only when status=out_of_scope")


class ClarificationRequest(BaseModel):
    question: str
    options: list[str] = Field(description="2-4 concrete, tappable options")
    allow_free_text: bool = True


# ---------------------------------------------------------------- glossary short-circuit


@lru_cache
def _load_glossary() -> dict:
    return yaml.safe_load((CONFIG_DIR / "glossary.yaml").read_text(encoding="utf-8"))


@lru_cache
def _glossary_terms() -> frozenset[str]:
    return frozenset(t.lower() for t in _load_glossary()["terms"])


# Deliberately-undefined terms that MUST still trigger clarification even though they sound like
# they could be glossary hits (guards against a future glossary edit accidentally "fixing" one of
# these by coincidence rather than deliberate definition -- see tests/test_golden_set.py).
_ALWAYS_AMBIGUOUS_PHRASES = (
    "best customer", "top customer", "top product", "best product", "best-selling",
    "large order", "high value", "high-value", "loyal", "recently", "declining", "inactive",
    "growth", "this period",
)


def glossary_covers(question: str) -> bool:
    """True only when the question's business terms are ALL in the glossary AND it doesn't
    contain one of the phrases we deliberately left undefined. A cheap, free, instant check that
    saves an LLM call on the common case -- most questions are not ambiguous."""
    q = question.lower()
    if any(phrase in q for phrase in _ALWAYS_AMBIGUOUS_PHRASES):
        return False
    terms_in_question = [t for t in _glossary_terms() if t in q]
    # If the question doesn't use ANY glossary term, it's not "covered" by the glossary --
    # it might still be perfectly clear (e.g. "how many products are active"), just not because
    # of a glossary hit. Only short-circuit when we recognize specific defined vocabulary.
    return bool(terms_in_question)


# ---------------------------------------------------------------- prompt


def ambiguity_analysis_system_prompt() -> str:
    from app.prompts import _compact_glossary, _compact_schema_docs  # reuse Phase 2's compact renderer
    template = (PROMPTS_DIR / "ambiguity_analysis_v1.txt").read_text(encoding="utf-8")
    return template.format(schema_docs=_compact_schema_docs(), glossary=_compact_glossary())


# ---------------------------------------------------------------- session memory


@dataclass
class SessionMemory:
    """Remembers resolved ambiguity choices within one session, keyed by ambiguity_type, so
    "best customer" -> "revenue" picked once isn't asked again for "best region" (same type:
    metric) within the same session. Deliberately process-local/in-memory: Phase 8's API layer
    owns the session id -> SessionMemory mapping; nothing here is persisted.
    """
    choices: dict[str, str] = field(default_factory=dict)
    rounds_this_question: int = 0

    def remember(self, ambiguity_type: str, choice: str) -> None:
        self.choices[ambiguity_type] = choice

    def recall(self, ambiguity_type: str) -> str | None:
        return self.choices.get(ambiguity_type)

    def reset_round_counter(self) -> None:
        self.rounds_this_question = 0


# ---------------------------------------------------------------- decision gate


class Decision(BaseModel):
    kind: str  # "proceed" | "clarify" | "refuse" | "assume"
    resolved_question: str = ""
    clarification: ClarificationRequest | None = None
    refusal_reason: str = ""
    labeled_assumption: str = ""  # set when kind == "assume" (fallback after unanswered round)


def analyze(question: str, provider: LLMProvider) -> AmbiguityAnalysis:
    system = ambiguity_analysis_system_prompt()
    return provider.generate_structured(system, f"Question: {question}", AmbiguityAnalysis)


def decide(question: str, provider: LLMProvider, memory: SessionMemory | None = None) -> Decision:
    """The core decision gate. Does NOT call the SQL-generation stage -- callers run that
    themselves once this returns kind == "proceed" or "assume"."""
    memory = memory or SessionMemory()

    if glossary_covers(question):
        return Decision(kind="proceed", resolved_question=question)

    analysis = analyze(question, provider)

    if analysis.status == "out_of_scope":
        return Decision(kind="refuse", refusal_reason=analysis.refusal_reason or
                         "This question can't be answered from this data.")

    if analysis.status == "clear" or analysis.confidence >= 0.85:
        return Decision(kind="proceed", resolved_question=question)

    # ambiguous -- check session memory first (free, no re-asking within a session)
    remembered = memory.recall(analysis.ambiguity_type)
    if remembered:
        resolved = _merge(question, analysis, remembered)
        return Decision(kind="proceed", resolved_question=resolved)

    if memory.rounds_this_question >= MAX_CLARIFICATION_ROUNDS:
        # already asked (and not answered usefully) up to the cap -- run the top interpretation,
        # clearly labeled, rather than ask again or refuse outright.
        top = analysis.interpretations[0] if analysis.interpretations else "a reasonable default"
        return Decision(kind="assume", resolved_question=question,
                         labeled_assumption=f"Assuming '{top}' for {analysis.ambiguity_type} "
                                             f"(no clarification given after {MAX_CLARIFICATION_ROUNDS} rounds)")

    options = (analysis.interpretations[:4] or ["(see reasoning)"])
    clarification = ClarificationRequest(
        question=_clarification_question_text(analysis),
        options=options,
        allow_free_text=True,
    )
    return Decision(kind="clarify", clarification=clarification)


def resolve_with_answer(question: str, ambiguity_type: str, user_choice: str, memory: SessionMemory) -> str:
    """Call after the user answers a ClarificationRequest. Merges the choice into one fully
    specified question and remembers it for the rest of the session."""
    memory.remember(ambiguity_type, user_choice)
    return _merge(question, None, user_choice, ambiguity_type=ambiguity_type)


def _merge(question: str, analysis: AmbiguityAnalysis | None, choice: str, ambiguity_type: str = "") -> str:
    atype = ambiguity_type or (analysis.ambiguity_type if analysis else "")
    return f"{question} (clarified: {atype} = {choice})"


def _clarification_question_text(analysis: AmbiguityAnalysis) -> str:
    prompts = {
        "metric": "Which metric should I use?",
        "time": "Which time range did you mean?",
        "entity": "Which definition did you mean?",
        "scope": "Can you narrow the scope?",
        "missing_param": "Can you give me a bit more detail?",
        "vague_term": "Can you clarify what you mean?",
    }
    return prompts.get(analysis.ambiguity_type, "Could you clarify what you mean?")
