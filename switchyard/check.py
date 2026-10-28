"""Regression guard: re-run a migration's saved tests and fail if any switched call drifts.

Designed for CI: `switchyard check runs/<id> --repo .` re-scans the current code (prompts
change over time), re-runs every migrated call site's saved test set on its chosen model,
and exits non-zero when a call site falls below the bar.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import evaluate, scanner
from .llm import TokenFactory


def load_run(ref: str) -> tuple[dict, Path]:
    p = Path(ref)
    if p.is_dir():
        p = p / "run.json"
    if not p.exists():
        from .pipeline import RUNS_DIR
        for base in (RUNS_DIR / "demo", RUNS_DIR):
            if (base / ref / "run.json").exists():
                p = base / ref / "run.json"
                break
    if not p.exists():
        raise FileNotFoundError(f"no run at {ref}")
    return json.loads(p.read_text(encoding="utf-8")), p.parent


def check(ref: str, repo: str | None = None, target: float | None = None, budget: float = 1.0, emit=print) -> dict:
    state, run_dir = load_run(ref)
    target = target if target is not None else state.get("settings", {}).get("target", 0.85)
    current = {}
    if repo:
        for c in scanner.scan_repo(repo)["calls"]:
            current[(c["file"], c["function"])] = c
    tf = TokenFactory(budget_usd=budget)
    results = []
    for rec in state.get("sites", []):
        d = rec.get("decision") or {}
        if rec.get("status") != "done" or d.get("action") != "migrate":
            continue
        site = rec["site"]
        changed = False
        if repo:
            now = current.get((site["file"], site["function"]))
            if now is None:
                results.append({"site": f"{site['file']}:{site['line']}", "function": site["function"],
                                "status": "missing", "detail": "call site no longer found"})
                continue
            changed = now["messages"] != site["messages"]
            site = now
        res = evaluate.evaluate_model(tf, site, rec["analysis"], rec["cases"], d["model"], hint=d.get("hint", ""))
        was = rec["evals"][d["choice"]]["pass_rate"]
        ok = res["pass_rate"] >= target
        results.append({"site": f"{site['file']}:{site['line']}", "function": site["function"], "model": d["choice"],
                        "pass_rate": res["pass_rate"], "was": was, "prompt_changed": changed,
                        "status": "ok" if ok else "regressed",
                        "failures": [{"case": r["case"], "reason": r["reason"]} for r in res["results"] if not r["passed"]][:5]})
        emit(f"{'OK ' if ok else 'FAIL'} {site['file']}:{site['line']} {site['function'] or ''} on {d['choice']}: "
             f"{res['pass_rate']:.0%} (was {was:.0%}){' · prompt changed' if changed else ''}")
    out = {"run": state["id"], "checked_at": time.time(), "target": target, "results": results,
           "ok": all(r["status"] == "ok" for r in results), "usage": tf.usage.to_dict()}
    (run_dir / "check.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out
