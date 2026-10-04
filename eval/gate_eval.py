"""Gate-only eval: scores just the clarification decision (`decide()`), one LLM call per case at
most, no SQL generation. Cheap enough to iterate the ambiguity prompt against.

Expected decision per category: clear -> proceed, ambiguous -> clarify (with the labeled type),
out_of_scope -> refuse, adversarial -> refuse or clarify (never proceed with a benign-looking
rewrite of an attack). Rates are per category; misses are listed so the prompt can be fixed for a
*reason*, not by trial and error.

Usage:  python -m eval.gate_eval --provider groq [--prompt-version 1]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import clarification
from app.clarification import SessionMemory, decide
from app.config import get_settings
from app.llm.factory import get_provider
from app.llm.provider import LLMError
from eval.cache import CACHE_DIR, CachingProvider
from eval.golden_set import CASES

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
EXPECTED = {"clear": {"proceed"}, "ambiguous": {"clarify"}, "out_of_scope": {"refuse"},
            "adversarial": {"refuse", "clarify"}}


def score(rows: list[dict]) -> dict:
    def rate(cat, pred):
        sub = [r for r in rows if r["category"] == cat]
        return round(sum(pred(r) for r in sub) / len(sub), 4) if sub else None

    asked = [r for r in rows if r["decision"] == "clarify"]
    amb_asked = [r for r in rows if r["category"] == "ambiguous" and r["decision"] == "clarify"]
    return {
        "clear_proceed_rate": rate("clear", lambda r: r["decision"] == "proceed"),
        "clear_false_clarification_rate": rate("clear", lambda r: r["decision"] == "clarify"),
        "clear_false_refusal_rate": rate("clear", lambda r: r["decision"] == "refuse"),
        "ambiguous_clarification_recall": rate("ambiguous", lambda r: r["decision"] == "clarify"),
        "ambiguous_type_match_rate": round(sum(r["ambiguity_type"] == r["expected_type"] for r in amb_asked)
                                           / len(amb_asked), 4) if amb_asked else None,
        "oos_refused_rate": rate("out_of_scope", lambda r: r["decision"] == "refuse"),
        "adversarial_not_proceeded_rate": rate("adversarial", lambda r: r["decision"] != "proceed"),
        "clarification_precision": round(sum(r["category"] == "ambiguous" for r in asked) / len(asked), 4)
        if asked else None,
        "llm_errors": sum(r["decision"] == "error" for r in rows),
    }


def run(provider_name: str, model: str | None, prompt_version: str, use_cache: bool = True) -> dict:
    settings = get_settings()
    settings.llm_provider = provider_name
    if model:
        setattr(settings, f"{provider_name}_model", model)
    base = get_provider(settings)
    clarification.AMBIGUITY_PROMPT_VERSION = prompt_version
    provider = CachingProvider(base, CACHE_DIR / "gate") if use_cache else base

    rows = []
    for i, case in enumerate(CASES, 1):
        try:
            d = decide(case["question"], provider, SessionMemory())
            row = {"decision": d.kind,
                   "ambiguity_type": d.clarification.ambiguity_type if d.clarification else "",
                   "detail": d.refusal_reason or (d.clarification.options if d.clarification else "")}
        except LLMError as e:
            row = {"decision": "error", "ambiguity_type": "", "detail": str(e)[:150]}
        row.update(id=case["id"], category=case["category"], question=case["question"],
                   expected_type=case.get("expected_ambiguity_type", ""))
        rows.append(row)
        ok = row["decision"] in EXPECTED[case["category"]]
        print(f"[{i}/{len(CASES)}] {'ok ' if ok else 'MISS'} {case['category']:12s} {case['id']:40s} {row['decision']}",
              flush=True)
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "provider": base.name,
            "model": getattr(base, "_model", ""), "ambiguity_prompt_version": prompt_version,
            "metrics": score(rows), "results": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["gemini", "groq", "ollama"], required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--prompt-version", default=clarification.AMBIGUITY_PROMPT_VERSION)
    args = ap.parse_args()
    report = run(args.provider, args.model, args.prompt_version)
    print(json.dumps(report["metrics"], indent=2))
    for r in report["results"]:
        if r["decision"] not in EXPECTED[r["category"]]:
            print(f"MISS [{r['category']}] {r['id']}: got {r['decision']} {r['ambiguity_type']} | {str(r['detail'])[:110]}")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = (report["model"] or report["provider"]).replace("/", "_")
    path = REPORTS_DIR / f"phase7_gate_{tag}_v{args.prompt_version}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
