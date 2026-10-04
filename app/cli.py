"""Command-line entry point with the full clarify-then-answer loop.

Usage:  python -m app.cli "Who was our best customer last month?"
        python -m app.cli --baseline "How many products are in the catalog?"   # no clarification gate
"""
from __future__ import annotations

import sys
from typing import Callable

from app.clarification import SessionMemory
from app.config import Settings
from app.llm.provider import LLMError, LLMProvider
from app.pipeline import AskResult, ask, continue_after_clarification


def _print_answer(result: AskResult, out: Callable[[str], None]) -> None:
    a = result.answer
    out(f"SQL:\n  {a.sql}\n")
    if a.repairs:
        out(f"(self-repair: recovered after {len(a.repairs)} failed attempt(s))\n")
    if a.assumptions:
        out("Assumptions:")
        for item in a.assumptions:
            out(f"  - {item}")
        out("")
    if a.truncated:
        out(f"Note: result truncated at {a.row_count} rows.\n")
    out(f"Answer: {a.answer}")


def run_interactive(question: str, provider: LLMProvider | None = None, settings: Settings | None = None,
                    input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    memory = SessionMemory()
    result = ask(question, provider=provider, settings=settings, memory=memory)

    # ask() may clarify; the engine caps rounds, so this loop is bounded
    while result.kind == "clarify":
        c = result.clarification
        out(c.question)
        for i, opt in enumerate(c.options, 1):
            out(f"  {i}. {opt}")
        if c.allow_free_text:
            out("  or type your own answer")
        raw = input_fn("> ").strip()
        choice = c.options[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(c.options) else raw
        if not choice:
            out("No answer given; stopping.")
            return 1
        result = continue_after_clarification(question, c.ambiguity_type, choice,
                                              provider=provider, settings=settings, memory=memory)

    if result.kind == "answer":
        _print_answer(result, out)
        return 0
    out(f"{'Refused' if result.kind == 'refuse' else 'Error'}: {result.refusal_reason or result.error_message}")
    return 1


def main() -> int:
    args = sys.argv[1:]
    baseline = "--baseline" in args
    question = " ".join(a for a in args if a != "--baseline")
    if not question:
        print('Usage: python -m app.cli [--baseline] "your question"')
        return 1
    if baseline:
        from app.pipeline import answer_question
        try:
            a = answer_question(question)
        except Exception as e:  # baseline path raises by design; show it plainly
            print(f"Could not answer: {e}")
            return 1
        _print_answer(AskResult(kind="answer", answer=a), print)
        return 0
    try:
        return run_interactive(question)
    except LLMError as e:
        print(f"Could not answer: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
