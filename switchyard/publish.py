"""Promote a finished run to the public demo and turn its numbers into write-up text.

`switchyard publish <run>`:
  1. copies run.json into runs/demo/<id>/ (the folder the public web app and Docker image serve),
     with local file paths stripped so nothing from your machine leaks,
  2. rewrites the README's Results section from the run,
  3. prints the headline numbers for the Devpost write-up and demo video.

Every number comes from the run itself, so the write-up can't drift from what the app shows.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .check import load_run
from .pipeline import RUNS_DIR

ROOT = Path(__file__).resolve().parent.parent
START, END = "<!-- results:start -->", "<!-- results:end -->"
ROLE_LABEL = {"investigate": "Investigate the codebase", "analyze": "Understand call sites", "testgen": "Write tests", "judge": "Grade outputs",
              "repair": "Adapt prompts", "harness": "Write calls for the patched code", "audit": "Attack prompts (security probes)"}


def _pct(x) -> str:
    return "–" if x is None else f"{round(x * 100)}%"


def _source_label(src: str) -> str:
    """GitHub URLs stay as they are; a local path becomes its folder name."""
    if re.match(r"^(https?://|git@)", src or ""):
        return src
    return Path(src or "local").name or "local"


def _h2h(state: dict) -> dict:
    vs = [x["decision"]["vs_source"] for x in state.get("sites", [])
          if x.get("decision", {}).get("vs_source", {}).get("chosen_score") is not None]
    if not vs:
        return {"h2h": 0, "h2h_mine": 0.0, "h2h_src": 0.0}
    return {"h2h": len(vs), "h2h_mine": sum(v["chosen_score"] for v in vs) / len(vs),
            "h2h_src": sum(v["score"] for v in vs) / len(vs)}


def headline(state: dict) -> dict:
    s = state.get("summary") or {}
    u = state.get("usage") or {}
    by_model = s.get("by_model") or {}
    return {
        "source": _source_label(state.get("source", "")),
        "sites": s.get("sites", 0),
        "migrated": s.get("migrated", 0),
        "kept": s.get("kept", 0),
        "errors": s.get("errors", 0),
        "repaired": s.get("repaired", 0),
        "saving_pct": s.get("saving_pct", 0),
        "by_model": {config.MODELS[k].label: v for k, v in by_model.items() if k in config.MODELS},
        "run_cost": u.get("cost", 0.0),
        "run_calls": u.get("calls", 0),
        **_h2h(state),
        "runtime": len((state.get("patch") or {}).get("runtime") or []),
        "verified": sum(1 for r in (state.get("patch") or {}).get("runtime") or [] if r.get("status") == "verified"),
        "minutes": round(((state.get("finished") or 0) - (state.get("started") or 0)) / 60, 1),
    }


def results_markdown(state: dict) -> str:
    h = headline(state)
    models = ", ".join(f"{n} on {m}" for m, n in h["by_model"].items()) or "none"
    src = h["source"]
    src_md = f"[{src.replace('https://github.com/', '')}]({src})" if src.startswith("http") else f"`{src}`"
    lines = [
        f"Recorded run `{state['id']}` on {src_md}:",
        "",
        f"- **{h['migrated']} of {h['sites']}** call sites switched ({models}); "
        f"{h['kept']} kept on the original model" + (f", {h['errors']} errored" if h["errors"] else "") + ".",
        f"- **{h['saving_pct']}% cheaper** per request across the switched calls (list prices).",
        f"- **{h['repaired']}** call site(s) only passed after Nemotron Ultra adapted the prompt.",
        *([f"- Head-to-head on the same tests, the chosen Nemotron model averaged **{h['h2h_mine']:.2f}** vs "
           f"**{h['h2h_src']:.2f}** for the original model ({h['h2h']} call sites)."] if h["h2h"] else []),
        *([f"- **{h['verified']} of {h['runtime']}** switched functions were run from the patched code and "
           f"returned normally after calling Token Factory."] if h["runtime"] else []),
        f"- The whole analysis cost **${h['run_cost']:.2f}** in Token Factory credits "
        f"({h['run_calls']} model calls" + (f", {h['minutes']} min" if h["minutes"] > 0 else "") + ").",
        "",
        "| Call site | Task | Was | Now | Tests passed | Saving |",
        "|---|---|---|---|---|---|",
    ]
    for x in state.get("sites", []):
        site = x["site"]
        loc = f"`{site['file']}:{site['line']}` {site.get('function') or ''}".strip()
        task = (x.get("analysis") or {}).get("task_type", "–")
        was = f"`{site.get('model') or site.get('model_expr') or 'dynamic'}`"
        if x.get("status") != "done":
            lines.append(f"| {loc} | {task} | {was} | error | – | – |")
            continue
        d, sv = x["decision"], x.get("savings") or {}
        if d["action"] == "migrate":
            now = config.MODELS[d["model"]].label + (" + adapted prompt" if d.get("hint") else "")
            lines.append(f"| {loc} | {task} | {was} | {now} | {_pct(d.get('pass_rate'))} | {sv.get('saving_pct', 0)}% |")
        else:
            lines.append(f"| {loc} | {task} | {was} | kept | – | – |")
    roles = (state.get("usage") or {}).get("by_role") or {}
    if roles:
        lines += ["", "What Switchyard itself spent, by job:", "", "| Job | Model | Calls | Cost |", "|---|---|---|---|"]
        for role, r in sorted(roles.items(), key=lambda kv: -kv[1]["cost"]):
            label = ROLE_LABEL.get(role) or ("Candidate: " + role.split(":", 1)[1] if role.startswith("candidate:") else role)
            model = config.MODELS[r["model"]].label if r.get("model") in config.MODELS else r.get("model", "")
            lines.append(f"| {label} | {model} | {r['calls']} | ${r['cost']:.4f} |")
    return "\n".join(lines)


def _sanitize(state: dict) -> dict:
    out = dict(state)
    out["source"] = _source_label(state.get("source", ""))
    out.pop("pr_body", None)  # regenerated by the server from the data
    ck = out.get("checkout")
    if isinstance(ck, dict) and ("root" in ck or "subdir" in ck):
        # a local source's checkout holds paths on this machine: they mean nothing on the demo
        out["checkout"] = {k: v for k, v in ck.items() if k not in ("root", "subdir")}
    return out


def update_readme(md: str, readme: Path) -> bool:
    text = readme.read_text(encoding="utf-8")
    block = f"{START}\n{md}\n{END}"
    if START in text and END in text:
        new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S)
    elif "## Results" in text:
        new = re.sub(r"(## Results\n)(.*?)(?=\n## )", lambda m: f"{m.group(1)}\n{block}\n", text, count=1, flags=re.S)
    else:
        return False
    if new != text:
        readme.write_text(new, encoding="utf-8")
    return True


def publish(ref: str, *, readme: Path | None = None, write_readme: bool = True, allow_simulated: bool = False) -> dict:
    state, _run_dir = load_run(ref)
    if state.get("status") != "done":
        raise ValueError(f"run {state.get('id')} is '{state.get('status')}', not done; only finished runs can be published")
    if state["id"].startswith("mock-") and not allow_simulated:
        raise ValueError("this is a simulated (offline) run; publish a real Token Factory run instead")
    dest = RUNS_DIR / "demo" / state["id"]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "run.json").write_text(json.dumps(_sanitize(state), indent=1, default=str), encoding="utf-8")
    md = results_markdown(state)
    readme = readme or ROOT / "README.md"
    updated = write_readme and readme.exists() and update_readme(md, readme)
    return {"dest": dest, "markdown": md, "readme": readme if updated else None, "headline": headline(state)}
