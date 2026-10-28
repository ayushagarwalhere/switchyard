"""Human review: check Switchyard's grader against people.

`export` writes a TSV of (prompt, reference, candidate answer, grader verdict) rows, sampled
across call sites and models, in the INPUT:/GOLDEN: column style crowd platforms such as
Toloka accept. Raters answer "is the candidate as good as the reference for this task?".
`import_results` reads their verdicts back and reports grader-vs-human agreement.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from . import agents
from .check import load_run

FIELDS = ["INPUT:item_id", "INPUT:task", "INPUT:prompt", "INPUT:reference", "INPUT:candidate", "GOLDEN:grader_pass"]


def export(ref: str, out: str, per_site: int = 4, seed: int = 7) -> int:
    state, _ = load_run(ref)
    rng = random.Random(seed)
    rows = []
    for rec in state.get("sites", []):
        if rec.get("status") != "done":
            continue
        cases = {c["id"]: c for c in rec.get("cases", [])}
        pool = [(mk, r) for mk, e in rec.get("evals", {}).items() for r in e["results"] if not r.get("error")]
        # balance passes and fails so agreement is measured on both
        passes = [x for x in pool if x[1]["passed"]]
        fails = [x for x in pool if not x[1]["passed"]]
        rng.shuffle(passes)
        rng.shuffle(fails)
        pick = (fails[: per_site // 2] + passes)[:per_site]
        for mk, r in pick:
            c = cases.get(r["case"])
            if not c:
                continue
            prompt = "\n\n".join(f"[{m['role']}] {m['content']}" for m in agents.render(rec["site"]["messages"], c["inputs"]))
            rows.append({
                "INPUT:item_id": f"{rec['site']['id']}:{mk}:{r['case']}",
                "INPUT:task": (rec.get("analysis") or {}).get("summary", ""),
                "INPUT:prompt": prompt[:4000],
                "INPUT:reference": c["reference"][:3000],
                "INPUT:candidate": r["output"][:3000],
                "GOLDEN:grader_pass": "yes" if r["passed"] else "no",
            })
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def import_results(ref: str, path: str) -> dict:
    """Read rater verdicts (columns: INPUT:item_id + OUTPUT:verdict yes/no; extra columns ignored)."""
    state, run_dir = load_run(ref)
    grader: dict[str, bool] = {}
    for rec in state.get("sites", []):
        for mk, e in (rec.get("evals") or {}).items():
            for r in e["results"]:
                grader[f"{rec['site']['id']}:{mk}:{r['case']}"] = bool(r["passed"])
    votes: dict[str, list[bool]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = "\t" if "\t" in sample else ","
        for row in csv.DictReader(f, delimiter=delim):
            item = row.get("INPUT:item_id") or row.get("item_id")
            v = (row.get("OUTPUT:verdict") or row.get("verdict") or "").strip().lower()
            if item in grader and v in ("yes", "no", "true", "false", "1", "0"):
                votes.setdefault(item, []).append(v in ("yes", "true", "1"))
    agree = disagree = 0
    rows = []
    for item, vs in votes.items():
        human = sum(vs) * 2 >= len(vs)          # majority vote
        ok = human == grader[item]
        agree += ok
        disagree += not ok
        rows.append({"item": item, "human_pass": human, "grader_pass": grader[item], "raters": len(vs)})
    n = agree + disagree
    res = {"items": n, "agreement": agree / n if n else None, "rows": rows}
    state["human_review"] = {"items": n, "agreement": res["agreement"]}
    (run_dir / "human_review.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
    return res
