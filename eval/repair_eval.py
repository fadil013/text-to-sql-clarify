"""Phase 6 eval: how often does self-repair recover a *correct* answer from a broken query?

Method: take each clear golden case's known-good `gold_sql`, break it in one realistic way, confirm
the broken query really fails (validator or DB), then hand the broken SQL to `execute_with_repair`
and compare the repaired result set with the gold result (same comparison as the main eval: result
sets, never SQL strings).

This isolates repair ability from first-shot generation. It does NOT measure how often a real model
makes these mistakes in the wild -- that shows up as `repairs` on live runs (Phase 7).

Faults (one per case, assigned round-robin among those that apply, so each kind is exercised):
  hallucinated_column  `*_cents` -> `*` (the classic "forgot the unit suffix" hallucination)
  wrong_key_column     first `*_key` -> `*_id`
  unknown_table        first `dw.dim_x` -> `dw.dim_xs`
  syntax_error         first `)` removed

Usage:
    python -m eval.repair_eval                  # Gemini, cached
    python -m eval.repair_eval --provider groq
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from app.config import Settings, get_settings
from app.execution import run_sql
from app.llm.factory import get_provider
from app.llm.provider import LLMError, LLMProvider
from app.repair import RepairFailed, execute_with_repair
from app.schemas import SQLGeneration
from app.validator import ValidationError, validate_and_prepare
from eval.cache import CachingProvider
from eval.golden_set import CASES
from eval.runner import _gold_values_present

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _hallucinated_column(sql: str) -> str | None:
    return re.sub(r"\b(\w+?)_cents\b", r"\1", sql, count=1) if re.search(r"_cents\b", sql) else None


def _wrong_key_column(sql: str) -> str | None:
    return re.sub(r"\b(\w+)_key\b", r"\1_id", sql, count=1) if re.search(r"\w_key\b", sql) else None


def _unknown_table(sql: str) -> str | None:
    return re.sub(r"\bdw\.(dim_\w+?)\b", r"dw.\1s", sql, count=1) if re.search(r"\bdw\.dim_", sql) else None


def _syntax_error(sql: str) -> str | None:
    return sql.replace(")", "", 1) if ")" in sql else None


FAULTS = {
    "hallucinated_column": _hallucinated_column,
    "wrong_key_column": _wrong_key_column,
    "unknown_table": _unknown_table,
    "syntax_error": _syntax_error,
}


def _fails(sql: str, settings: Settings) -> bool:
    """True if `sql` genuinely fails validation or execution (a fault that doesn't break anything
    would make the repair rate meaningless)."""
    try:
        run_sql(validate_and_prepare(sql, settings.max_rows), settings)
    except (ValidationError, psycopg.Error):
        return True
    return False


def build_fault_cases(settings: Settings) -> list[dict]:
    names = list(FAULTS)
    cases = []
    for i, case in enumerate([c for c in CASES if c["category"] == "clear"]):
        order = names[i % len(names):] + names[: i % len(names)]  # round-robin start, then fallbacks
        for fault in order:
            broken = FAULTS[fault](case["gold_sql"])
            if broken and broken != case["gold_sql"] and _fails(broken, settings):
                cases.append({"case": case, "fault": fault, "broken_sql": broken})
                break
    return cases


def run(provider_name: str | None = None, use_cache: bool = True) -> dict:
    settings = get_settings()
    if provider_name:
        settings.llm_provider = provider_name
    base = get_provider(settings)
    provider: LLMProvider = CachingProvider(base) if use_cache else base

    fault_cases = build_fault_cases(settings)
    results = []
    for i, fc in enumerate(fault_cases, 1):
        case, t0 = fc["case"], time.monotonic()
        status, detail, n_attempts = "", "", 0
        try:
            outcome = execute_with_repair(case["question"], SQLGeneration(sql=fc["broken_sql"]), provider, settings)
            n_attempts = len(outcome.repairs)
            gold = run_sql(case["gold_sql"], settings)
            status = "repaired_correct" if _gold_values_present(gold, outcome.rows) else "repaired_wrong"
            if status == "repaired_wrong":
                detail = f"gold={gold} actual={outcome.rows}"
        except RepairFailed as e:
            status, n_attempts, detail = "repair_failed", len(e.attempts) - 1, str(e)
        except ValidationError as e:
            status, detail = "refused", str(e)
        except LLMError as e:
            status, detail = "llm_error", str(e)
        results.append({"case_id": case["id"], "fault": fc["fault"], "status": status,
                        "repairs_used": n_attempts, "latency_s": round(time.monotonic() - t0, 2),
                        "broken_sql": fc["broken_sql"], "detail": detail[:300]})
        print(f"[{i}/{len(fault_cases)}] {fc['fault']:20s} {case['id']:38s} {status}", flush=True)

    n = len(results)
    scored = [r for r in results if r["status"] != "llm_error"]  # provider outages aren't repair failures
    correct = sum(r["status"] == "repaired_correct" for r in scored)
    by_fault: dict[str, dict] = {}
    for r in scored:
        b = by_fault.setdefault(r["fault"], {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += r["status"] == "repaired_correct"
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": base.name, "model": getattr(base, "_model", ""), "cached": use_cache,
        "phase": "6 (self-repair)", "max_repairs": settings.max_repairs,
        "metrics": {
            "n_fault_cases": n, "n_scored": len(scored), "n_llm_errors": n - len(scored),
            "repair_success_rate": correct / len(scored) if scored else None,
            "repaired_but_wrong": sum(r["status"] == "repaired_wrong" for r in scored),
            "avg_repairs_used": (sum(r["repairs_used"] for r in scored) / len(scored)) if scored else None,
            "by_fault": by_fault,
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["gemini", "groq"], default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    report = run(args.provider, use_cache=not args.no_cache)
    m = report["metrics"]
    print(f"\nProvider: {report['provider']} ({report['model']})  scored: {m['n_scored']}/{m['n_fault_cases']}")
    print(f"  repair_success_rate: {m['repair_success_rate']:.1%}   repaired_but_wrong: {m['repaired_but_wrong']}"
          f"   avg_repairs_used: {m['avg_repairs_used']:.2f}" if m["n_scored"] else "  nothing scored")
    for fault, b in m["by_fault"].items():
        print(f"    {fault:20s} {b['correct']}/{b['n']}")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"phase6_repair_{base_name(report)}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Saved: {path}")
    return 0


def base_name(report: dict) -> str:
    return report["provider"].replace("+cache", "")


if __name__ == "__main__":
    sys.exit(main())
