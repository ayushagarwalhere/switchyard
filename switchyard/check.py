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


def latest_run(root: Path | None = None) -> Path | None:
    """Most recent finished run (not the featured demo runs); with `root`, only runs of that checkout."""
    from .pipeline import RUNS_DIR
    best = None
    for d in RUNS_DIR.glob("*/run.json"):
        if d.parent.name == "demo":
            continue
        try:
            st = json.loads(d.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if root is not None:
            co = (st.get("checkout") or {}).get("root")
            if not co or Path(co).resolve() != Path(root).resolve():
                continue
        if st.get("status") == "done" and (best is None or st.get("started", 0) > best[0]):
            best = (st.get("started", 0), d.parent)
    return best[1] if best else None


def load_run(ref: str | None = None) -> tuple[dict, Path]:
    if ref in (None, "", "latest"):
        found = latest_run()
        if not found:
            raise FileNotFoundError("no finished runs yet; start one with `switchyard run`")
        ref = str(found)
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
            # several calls can share a function: match them in source order
            current.setdefault((c["file"], c["function"]), []).append(c)
    tf = TokenFactory(budget_usd=budget)
    results = []
    for rec in state.get("sites", []):
        d = rec.get("decision") or {}
        if rec.get("status") != "done" or d.get("action") != "migrate":
            continue
        site = rec["site"]
        changed = False
        if repo:
            key = (site["file"], site["function"])
            # this site's position among the saved sites of the same function
            idx = sum(1 for r in state["sites"][: state["sites"].index(rec)]
                      if (r["site"]["file"], r["site"]["function"]) == key)
            cands = current.get(key, [])
            now = cands[idx] if idx < len(cands) else None
            if now is None:
                results.append({"site": f"{site['file']}:{site['line']}", "function": site["function"],
                                "status": "missing", "detail": "call site no longer found"})
                continue
            hint_text = d.get("hint", "")
            without_hint = [m for m in now["messages"] if not (hint_text and m.get("content") == hint_text)]
            changed = without_hint != site["messages"]
            site = now
        hint = d.get("hint", "")
        if hint and any(hint in str(m.get("content", "")) for m in site.get("messages") or []):
            hint = ""   # the patch already put the hint into the code; don't add it twice
        res = evaluate.evaluate_model(tf, site, rec["analysis"], rec["cases"], d["model"], hint=hint)
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
    from .pipeline import RUNS_DIR
    # results go next to Switchyard's own runs, never into the developer's checkout
    dest = run_dir if RUNS_DIR.resolve() in run_dir.resolve().parents else RUNS_DIR / "checks"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ("check.json" if dest == run_dir else f"{state['id']}.json")).write_text(json.dumps(out, indent=1), encoding="utf-8")
    except OSError:
        pass
    return out
