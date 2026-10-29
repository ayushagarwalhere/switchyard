"""Markdown report used as the pull-request description for a migration."""
from __future__ import annotations

from . import config


def _pct(x) -> str:
    return "–" if x is None else f"{round(x * 100)}%"


def pr_body(state: dict) -> str:
    s = state.get("summary") or {}
    patch = state.get("patch") or {}
    sites = [x for x in state.get("sites", []) if x.get("status") == "done"]
    lines = [
        "## Move LLM calls to NVIDIA Nemotron on Nebius Token Factory",
        "",
        f"Switchyard tested **{s.get('sites', len(sites))} LLM call sites** on Nemotron Lightning, Super and Ultra "
        f"and switched **{s.get('migrated', 0)}** of them to the cheapest model that cleared the quality bar "
        f"({_pct(state.get('settings', {}).get('target'))} of tests passing). "
        f"Estimated cost per request across switched calls: **{s.get('saving_pct', 0)}% lower**.",
        "",
        "| Call site | Was | Now | Tests passed | Cost / call before → after |",
        "|---|---|---|---|---|",
    ]
    for x in sites:
        site, d, sv = x["site"], x["decision"], x.get("savings") or {}
        loc = f"`{site['file']}:{site['line']}` {site.get('function') or ''}"
        was = f"`{site.get('model') or site.get('model_expr') or 'dynamic'}`"
        if d["action"] == "migrate":
            now = f"`{config.MODELS[d['model']].id}`" + (" + adapted prompt" if d.get("hint") else "")
            cost = f"${sv.get('source_cost_per_call', 0):.6f} → ${sv.get('new_cost_per_call', 0):.6f}"
            lines.append(f"| {loc} | {was} | {now} | {_pct(d.get('pass_rate'))} | {cost} |")
        else:
            lines.append(f"| {loc} | {was} | unchanged | – | kept: {d.get('reason', '')} |")
    h2h = [x for x in sites if x["decision"].get("vs_source", {}).get("chosen_score") is not None]
    if h2h:
        lines += ["", "### Head-to-head with the original model",
                  "The original model answered the same tests and was graded the same way:", "",
                  "| Call site | Original | Score | Nemotron | Score |", "|---|---|---|---|---|"]
        for x in h2h:
            v, d = x["decision"]["vs_source"], x["decision"]
            lines.append(f"| `{x['site']['file']}:{x['site']['line']}` | `{v['model']}` | {v['score']:.2f} "
                         f"| `{config.MODELS[d['model']].id}` | {v['chosen_score']:.2f} |")
    hints = [x for x in sites if x["decision"].get("hint")]
    if hints:
        lines += ["", "### Prompt adaptations", "Nemotron Ultra adapted these prompts so a cheaper model passes; each was re-tested:"]
        for x in hints:
            lines.append(f"- `{x['site']['file']}:{x['site']['line']}`: {x['decision']['hint']}")
    lines += ["", "### Verification"]
    for c in patch.get("compile", []):
        lines.append(f"- {'✅' if c['ok'] else '❌'} `{c['file']}` {'parses and compiles' if c['ok'] else c.get('error', '')}")
    for r in patch.get("runtime") or []:
        icon = {"verified": "✅", "failed": "❌", "unverified": "⚠️"}.get(r["status"], "➖")
        lines.append(f"- {icon} ran `{r.get('function')}()` from the patched code: {r.get('reason', '')}")
    if patch.get("tests"):
        t = patch["tests"]
        lines.append(f"- {'✅ project tests pass' if t.get('ok') else '❌ project tests failed'}")
    lines += [
        "", "### Configuration",
        "Set these in your environment (each model has its own override, so rollback is one variable):",
        "```env", (patch.get("env") or "").strip(), "```",
        "", "### Keep it honest in CI",
        "The test set is saved under `.switchyard/`. Re-run it in CI to catch quality drift "
        "(it re-scans the code, so edited prompts are what get tested):",
        "```bash", "switchyard check", "```",
    ]
    if patch.get("manual"):
        lines += ["", "### Manual follow-ups"]
        for m in patch["manual"]:
            why = f" ({m['reason']})" if m.get("reason") else ""
            lines.append(f"- `{m['file']}:{m['line']}` uses the {m['sdk']} SDK{why}; suggested model `{m['model']}`")
    au = state.get("audit") or {}
    if au.get("findings") is not None:
        f = au["findings"]
        lines += ["", "### Security audit"]
        if au.get("probed"):
            hits = [x for x in f if x["rule"].startswith("PR")]
            lines.append(f"Each switched call that takes user input was attacked on its new model with prompt injection, "
                         f"system-prompt extraction and exfiltration probes: "
                         + (f"{len(hits)} finding(s) below." if hits else "none got through."))
        top = [x for x in f if x["severity"] in ("CRITICAL", "HIGH")]
        rest = len(f) - len(top)
        for x in top:
            verified = " (fix re-tested: holds)" if x.get("fix_verified") else ""
            lines.append(f"- **{x['severity']}** `{x['file']}:{x['line']}` {x['title']}: {x['detail']}. "
                         f"Fix: {x['fix']}{verified}")
        if rest:
            lines.append(f"- {rest} medium/low finding(s): run `switchyard audit` for the list.")
        if not f:
            lines.append("No findings.")
    if state.get("skipped"):
        lines += ["", "### Not tested"]
        for k in state["skipped"]:
            lines.append(f"- `{k['file']}:{k['line']}` {k.get('function') or ''} ({k.get('model') or 'model'}): {k['reason']}")
    u = state.get("usage") or {}
    lines += ["", f"<sub>Generated by Switchyard · analysis ran on Nemotron via Token Factory for ${u.get('cost', 0):.3f}</sub>"]
    return "\n".join(lines) + "\n"
