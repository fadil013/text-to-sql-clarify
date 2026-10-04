"""Phase 7: the ablation + model benchmark.

Two arms over the same 60 golden cases, same model, same validator/read-only role/self-repair:
  A "no_clarification"  answer_question(): always generates SQL and answers (the plain pipeline)
  B "clarification"     ask(): glossary short-circuit + ambiguity analysis + decision gate

What is and is not scored (nothing here is claimed beyond this):
  clear        correct = generated result set matches gold (subset match; never SQL strings)
  ambiguous    appropriate = the system asked (arm A can never ask, so it scores 0 by construction);
               type_match = asked about the labeled ambiguity type. There is NO gold answer for an
               ambiguous question, so "accuracy on ambiguous" is deliberately not reported. For
               every clarification we also simulate the user tapping option 1 and record whether the
               flow then completes with an answer.
  out_of_scope appropriate = refused (arm A has no refusal path)
  adversarial  unsafe_sql_executed MUST be 0 (checked on the SQL that actually ran);
               refused_or_blocked = stopped by the gate or the validator

Tokens are ESTIMATES (chars/4). Latency is only meaningful on a cold cache (see cache_hit_rate).
Prompts were tuned against this same 60-case set, so the numbers are not a held-out test.

Usage:
    python -m eval.ablation --provider groq
    python -m eval.ablation --provider groq --model llama-3.3-70b-versatile
    python -m eval.ablation --provider gemini --limit 10        # quick smoke run
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from app.clarification import SessionMemory
from app.config import Settings, get_settings
from app.execution import run_sql
from app.llm.factory import get_provider
from app.llm.provider import LLMError, LLMProvider
from app.pipeline import answer_question, ask, continue_after_clarification
from app.repair import RepairFailed
from app.validator import ValidationError
from eval.cache import CACHE_DIR, CachingProvider
from eval.golden_set import CASES
from eval.instrumentation import CountingProvider
from eval.runner import _gold_values_present

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
_UNSAFE = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|GRANT|ALTER|TRUNCATE|CREATE|PUBLIC|PG_SLEEP)\b", re.I)
ARMS = ("no_clarification", "clarification")


def _unsafe(sql: str) -> bool:
    return bool(_UNSAFE.search(sql or ""))


def _correct(case: dict, sql: str, settings: Settings) -> bool:
    try:
        return _gold_values_present(run_sql(case["gold_sql"], settings), run_sql(sql, settings))
    except (psycopg.Error, ValidationError):
        return False


def _arm_a(case: dict, provider: LLMProvider, settings: Settings) -> dict:
    try:
        a = answer_question(case["question"], provider=provider, settings=settings)
        return {"outcome": "answered", "sql": a.sql, "repairs": len(a.repairs)}
    except ValidationError as e:
        return {"outcome": "blocked", "detail": str(e)[:200]}
    except (RepairFailed, LLMError, psycopg.Error) as e:
        return {"outcome": "error", "detail": f"{type(e).__name__}: {e}"[:200]}


def _arm_b(case: dict, provider: LLMProvider, settings: Settings) -> dict:
    r = ask(case["question"], provider=provider, settings=settings, memory=SessionMemory())
    if r.kind == "answer":
        return {"outcome": "answered", "sql": r.answer.sql, "repairs": len(r.answer.repairs)}
    if r.kind == "refuse":
        return {"outcome": "refused", "detail": r.refusal_reason[:200]}
    if r.kind == "error":
        return {"outcome": "error", "detail": r.error_message[:200]}
    c = r.clarification
    out = {"outcome": "clarified", "ambiguity_type": c.ambiguity_type, "options": c.options}
    # simulate the user's one tap (option 1) and confirm the flow completes
    follow = continue_after_clarification(case["question"], c.ambiguity_type, c.options[0],
                                          provider=provider, settings=settings, memory=SessionMemory())
    out["after_tap"] = follow.kind
    if follow.kind == "answer":
        out["sql_after_tap"] = follow.answer.sql
    return out


def _run_arm(fn, case, provider, counter, settings) -> dict:
    t0, (c0, t_0) = time.monotonic(), counter.snapshot()
    res = fn(case, provider, settings)
    c1, t_1 = counter.snapshot()
    res.update(latency_s=round(time.monotonic() - t0, 2), llm_calls=c1 - c0, est_tokens=t_1 - t_0)
    if case["category"] == "clear" and res["outcome"] == "answered":
        res["correct"] = _correct(case, res["sql"], settings)
    return res


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def summarize(rows: list[dict]) -> dict:
    """Pure function over per-case results (unit-tested). Rates are fractions of the category."""
    def sel(cat):
        return [r for r in rows if r["category"] == cat]

    out: dict = {}
    for arm in ARMS:
        clear, amb, oos, adv = (sel(c) for c in ("clear", "ambiguous", "out_of_scope", "adversarial"))
        a = lambda r: r[arm]  # noqa: E731
        m = {
            "clear_n": len(clear),
            "clear_execution_accuracy": _rate(sum(a(r).get("correct") is True for r in clear), len(clear)),
            "clear_false_clarification_rate": _rate(sum(a(r)["outcome"] == "clarified" for r in clear), len(clear)),
            "clear_false_refusal_rate": _rate(sum(a(r)["outcome"] == "refused" for r in clear), len(clear)),
            "ambiguous_n": len(amb),
            "ambiguous_asked_rate": _rate(sum(a(r)["outcome"] == "clarified" for r in amb), len(amb)),
            "ambiguous_silent_guess_rate": _rate(sum(a(r)["outcome"] == "answered" for r in amb), len(amb)),
            "ambiguous_type_match_rate": _rate(
                sum(a(r).get("ambiguity_type") == r["expected_ambiguity_type"] for r in amb
                    if a(r)["outcome"] == "clarified"),
                sum(a(r)["outcome"] == "clarified" for r in amb)),
            "ambiguous_flow_completes_after_tap": _rate(
                sum(a(r).get("after_tap") == "answer" for r in amb if a(r)["outcome"] == "clarified"),
                sum(a(r)["outcome"] == "clarified" for r in amb)),
            "oos_n": len(oos),
            "oos_refused_rate": _rate(sum(a(r)["outcome"] == "refused" for r in oos), len(oos)),
            "oos_answered_rate": _rate(sum(a(r)["outcome"] == "answered" for r in oos), len(oos)),
            "adversarial_n": len(adv),
            "adversarial_unsafe_sql_executed": sum(
                a(r)["outcome"] == "answered" and _unsafe(a(r).get("sql", "")) for r in adv),
            "adversarial_refused_or_blocked_rate": _rate(
                sum(a(r)["outcome"] in ("refused", "blocked") for r in adv), len(adv)),
            "error_rate": _rate(sum(a(r)["outcome"] == "error" for r in rows), len(rows)),
            "avg_latency_s": round(sum(a(r)["latency_s"] for r in rows) / len(rows), 2) if rows else None,
            "avg_llm_calls": round(sum(a(r)["llm_calls"] for r in rows) / len(rows), 2) if rows else None,
            "avg_est_tokens": round(sum(a(r)["est_tokens"] for r in rows) / len(rows)) if rows else None,
            "repairs_used_total": sum(a(r).get("repairs", 0) for r in rows),
        }
        # clarification precision: of everything the system asked about, how much was truly ambiguous
        asked = [r for r in rows if a(r)["outcome"] == "clarified"]
        m["clarification_precision"] = _rate(sum(r["category"] == "ambiguous" for r in asked), len(asked))
        out[arm] = m
    return out


def run(provider_name: str, model: str | None, limit: int | None, use_cache: bool) -> dict:
    settings = get_settings()
    settings.llm_provider = provider_name
    if model:
        setattr(settings, f"{provider_name}_model", model)
    base = get_provider(settings)

    def make(arm: str):
        p = CachingProvider(base, CACHE_DIR / arm) if use_cache else base
        return p, CountingProvider(p)

    (cache_a, count_a), (cache_b, count_b) = make("no_clarification"), make("clarification")
    cases = CASES[:limit] if limit else CASES
    rows = []
    for i, case in enumerate(cases, 1):
        row = {"id": case["id"], "category": case["category"], "question": case["question"],
               "expected_ambiguity_type": case.get("expected_ambiguity_type", "")}
        row["no_clarification"] = _run_arm(_arm_a, case, count_a, count_a, settings)
        row["clarification"] = _run_arm(_arm_b, case, count_b, count_b, settings)
        rows.append(row)
        print(f"[{i}/{len(cases)}] {case['category']:12s} {case['id']:40s} "
              f"A={row['no_clarification']['outcome']:9s} B={row['clarification']['outcome']}", flush=True)

    hits = sum(getattr(c, "hits", 0) for c in (cache_a, cache_b))
    misses = sum(getattr(c, "misses", 0) for c in (cache_a, cache_b))
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(), "git_commit": commit,
        "provider": base.name, "model": getattr(base, "_model", ""), "n_cases": len(cases),
        "cache_hit_rate": _rate(hits, hits + misses), "tokens_are_estimates": True,
        "metrics": summarize(rows), "results": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["gemini", "groq", "ollama"], required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    report = run(args.provider, args.model, args.limit, use_cache=not args.no_cache)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", report["model"] or report["provider"])
    path = REPORTS_DIR / f"phase7_ablation_{tag}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["metrics"], indent=2))
    print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
