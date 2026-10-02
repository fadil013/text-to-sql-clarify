"""Command-line entry point for Phase 2's baseline pipeline.

Usage:  python -m app.cli "How many customers signed up last month?"
"""
from __future__ import annotations

import sys

from app.llm.provider import LLMError
from app.pipeline import NotSelectError, answer_question


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python -m app.cli "your question"')
        return 1
    question = " ".join(sys.argv[1:])

    try:
        result = answer_question(question)
    except (LLMError, NotSelectError) as e:
        print(f"Could not answer: {e}")
        return 1

    print(f"SQL:\n  {result.sql}\n")
    if result.assumptions:
        print("Assumptions:")
        for a in result.assumptions:
            print(f"  - {a}")
        print()
    print(f"Answer: {result.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
