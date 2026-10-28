"""Recorded production calls as test cases.

The strongest evidence that a cheaper model is good enough is your original model's real
answers on your real traffic. Switchyard accepts a JSONL file of recorded calls, matches each
call to the call site whose prompt template produced it, recovers the template variables, and
uses the recorded answer as the reference.

Accepted line shapes (one JSON object per line):
  {"messages": [...], "output": "...", "model": "gpt-4o"}
  {"request": {"model": ..., "messages": [...]}, "response": {"choices": [{"message": {"content": ...}}]}}
  LangSmith LLM runs: {"inputs": {"messages": [...]}, "outputs": {...}, "extra": {"invocation_params": {"model": ...}}}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")
MIN_ANCHOR = 12  # literal characters a template needs before we trust a match


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "" if content is None else str(content)


def _norm_messages(msgs) -> list[dict]:
    out = []
    for m in msgs or []:
        if isinstance(m, dict):
            role = m.get("role") or m.get("type") or "user"
            role = {"human": "user", "ai": "assistant", "developer": "system"}.get(role, role)
            out.append({"role": role, "content": _text(m.get("content") or m.get("data", {}).get("content"))})
        elif isinstance(m, list):  # LangChain-style [[{...}]]
            out.extend(_norm_messages(m))
    return out


def _output(obj) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "choices" in obj and obj["choices"]:
            msg = obj["choices"][0].get("message") or {}
            if msg.get("content"):
                return _text(msg["content"])
            if msg.get("parsed"):
                return json.dumps(msg["parsed"])
        for key in ("output", "content", "text", "completion"):
            if key in obj:
                return _output(obj[key])
        if "generations" in obj:
            g = obj["generations"]
            while isinstance(g, list) and g:
                g = g[0]
            if isinstance(g, dict):
                return g.get("text") or _output(g.get("message"))
        if "message" in obj:
            return _output(obj["message"])
    if isinstance(obj, list) and obj:
        return _output(obj[0])
    return None


def parse_record(rec: dict) -> dict | None:
    messages = rec.get("messages")
    model = rec.get("model")
    output = rec.get("output") if "output" in rec else rec.get("response_text")
    if messages is None and isinstance(rec.get("request"), dict):
        messages = rec["request"].get("messages")
        model = model or rec["request"].get("model")
        output = output if output is not None else _output(rec.get("response"))
    if messages is None and isinstance(rec.get("inputs"), dict):  # LangSmith
        messages = rec["inputs"].get("messages")
        output = output if output is not None else _output(rec.get("outputs"))
        model = model or (rec.get("extra", {}).get("invocation_params", {}) or {}).get("model")
    if output is not None and not isinstance(output, str):
        output = _output(output) if not isinstance(output, (dict, list)) else (_output(output) or json.dumps(output))
    msgs = _norm_messages(messages)
    if not msgs or not output:
        return None
    return {"messages": msgs, "output": output, "model": model}


def load(path: str | Path) -> list[dict]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = parse_record(json.loads(line))
        except json.JSONDecodeError:
            continue
        if rec:
            out.append(rec)
    return out


def _template_regex(messages: list[dict]) -> tuple[re.Pattern, list[str], int]:
    """Template messages -> one anchored regex over the flattened conversation."""
    parts, names, anchor = [], [], 0
    seen: dict[str, int] = {}
    for m in messages:
        if m["role"] == "history":          # *history spliced into the list: match anything
            parts.append(r"(?:<\w+>.*?</\w+>)*?")
            continue
        role = m["role"] if m["role"] in ("system", "user", "assistant") else "user"
        body = []
        for i, seg in enumerate(PLACEHOLDER.split(m["content"])):
            if i % 2 == 0:                  # literal text
                lit = seg.strip()
                anchor += len(lit)
                body.append(r"\s*" + re.escape(lit) + r"\s*" if lit else r"\s*")
            else:                           # {{var}}
                k = seen.get(seg, 0)
                seen[seg] = k + 1
                group = re.sub(r"\W", "_", seg) + (f"__{k}" if k else "")
                if group[0].isdigit():
                    group = "v_" + group
                names.append(group)
                body.append(rf"(?P<{group}>.*?)")
        parts.append(f"<{role}>" + "".join(body) + f"</{role}>")
    return re.compile("^" + "".join(parts) + "$", re.S), names, anchor


def _flatten(messages: list[dict]) -> str:
    return "".join(f"<{m['role']}>{m['content']}</{m['role']}>" for m in messages
                   if m["role"] in ("system", "user", "assistant"))


def cases_for_site(site: dict, records: list[dict], limit: int = 8) -> list[dict]:
    if not records:
        return []
    try:
        rx, names, anchor = _template_regex(site["messages"])
    except re.error:
        return []
    if anchor < MIN_ANCHOR:
        return []
    cases = []
    for rec in records:
        if site.get("model") and rec.get("model") and not str(rec["model"]).startswith(site["model"]):
            continue
        m = rx.match(_flatten(rec["messages"]))
        if not m:
            continue
        inputs = {}
        for g in names:
            if "__" in g:
                continue
            var = next((v for v in site["variables"] if re.sub(r"\W", "_", v) in (g, g.removeprefix("v_"))), g)
            inputs[var] = m.group(g).strip()
        cases.append({"id": len(cases) + 1, "name": f"production call #{len(cases) + 1}", "kind": "production",
                      "inputs": inputs, "reference": rec["output"], "expect": {},
                      "why": f"recorded answer from {rec.get('model') or 'the original model'}", "source": "production"})
        if len(cases) >= limit:
            break
    return cases


def from_langsmith(project: str, out: str | Path, limit: int = 500) -> int:
    """Export LLM runs from a LangSmith project to the JSONL format above."""
    from langsmith import Client
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for run in Client().list_runs(project_name=project, run_type="llm", limit=limit):
            rec = {"inputs": run.inputs, "outputs": run.outputs, "extra": run.extra or {}}
            if parse_record(rec):
                f.write(json.dumps(rec, default=str) + "\n")
                n += 1
    return n
