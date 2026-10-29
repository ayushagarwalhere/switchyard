"""Investigate a call site like an engineer would, before deciding how to test it.

The scanner sees one function. To test a call honestly you also need to know who calls it,
with what kind of input, and what the app does with the answer (a field that drives a
branch matters more than a free-text description). Nemotron Super gets read-only tools over
the repository (search, read a file, list files) and works that out in a few steps.

A static pass runs first and always: it finds every place the function is called, so even
if the endpoint rejects tool calls, the analysis still sees how the function is used.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .llm import TokenFactory, extract_json

MAX_STEPS = 6
MAX_RESULT = 3500
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", "dist", "build"}

TOOLS = [
    {"type": "function", "function": {
        "name": "search", "description": "Search all Python files in the repository with a regular expression. Returns file:line: text for each match.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read lines of a file in the repository (max 150 lines per call).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "List the repository's Python files with their line counts.",
        "parameters": {"type": "object", "properties": {}}}},
]

SYS = """You are investigating one LLM call inside an application so that it can be tested properly before it is moved to a cheaper model.
Use the tools to find out:
1. who calls the enclosing function and with what kind of input (find real example values in the code: constants, test fixtures, sample data, CLI defaults),
2. what the application does with the result: which fields or labels drive logic (branches, thresholds, lookups) and which are only displayed,
3. anything the prompt depends on that is defined elsewhere.
Be efficient: a few targeted searches and reads, then answer. Do not guess what you can look up.
When done, reply (without a tool call) with JSON only:
{"callers": [{"where": "file:line", "how": "one sentence"}],
 "downstream": "how the result is used, concretely",
 "critical": ["fields/labels/format properties the app logic depends on"],
 "input_examples": ["real input values found in the repository, verbatim, max 5"],
 "notes": "anything else a tester must know (or empty)"}"""


ANSWER_SHAPE = SYS[SYS.index("{\"callers\""):]
FALLBACK_SYS = ("You are investigating one LLM call inside an application so that it can be tested properly before it is "
                "moved to a cheaper model. From the code shown, work out who calls it and with what input, what the app does "
                "with the result (which fields or labels drive logic), and any real input values visible in the code.\n"
                "Reply with JSON only:\n" + ANSWER_SHAPE)


class RepoTools:
    """Read-only, confined to the repository root."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        import os

        from .jsscan import JS_EXT, JS_SKIP
        skip = SKIP_DIRS | JS_SKIP
        files = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]   # never walk node_modules
            files += [Path(dirpath) / f for f in filenames if f.endswith((".py",) + JS_EXT)]
        self.files = sorted(files)

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def list_files(self) -> str:
        out = []
        for p in self.files[:300]:
            try:
                out.append(f"{self._rel(p)} ({sum(1 for _ in p.open(encoding='utf-8', errors='replace'))} lines)")
            except OSError:
                continue
        return "\n".join(out) or "(no Python files)"

    def search(self, pattern: str, limit: int = 40) -> str:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits = []
        for p in self.files:
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{self._rel(p)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= limit:
                            return "\n".join(hits) + "\n(more matches truncated)"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root) or not p.is_file():
            return f"(no such file: {path})"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 149), start + 149)
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1)) or "(empty range)"

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "search":
                return self.search(str(args.get("pattern", "")))
            if name == "read_file":
                return self.read_file(str(args.get("path", "")), args.get("start") or 1, args.get("end"))
            if name == "list_files":
                return self.list_files()
        except Exception as e:  # noqa: BLE001 - a bad tool call is reported back to the model
            return f"(tool error: {type(e).__name__}: {e})"
        return f"(unknown tool {name})"


def static_callers(tools: RepoTools, site: dict, limit: int = 8) -> list[dict]:
    """Every place the enclosing function is called, with two lines of context either side."""
    fn = site.get("function")
    if not fn:
        return []
    rx = re.compile(rf"(?<![\w.]def )\b{re.escape(fn)}\s*\(")
    out = []
    for p in tools.files:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if rx.search(line) and not re.match(rf"\s*(async\s+)?def\s+{re.escape(fn)}\b", line):
                ctx = "\n".join(lines[max(0, i - 2): i + 3])
                out.append({"where": f"{tools._rel(p)}:{i + 1}", "code": ctx[:600]})
                if len(out) >= limit:
                    return out
    return out


def _brief(site: dict, callers: list[dict]) -> str:
    msgs = "\n".join(f"[{m['role']}] {m['content'][:1200]}" for m in site["messages"])
    found = "\n\n".join(f"{c['where']}\n{c['code']}" for c in callers) or "(no direct callers found by a static search)"
    return (f"Call site: {site['file']}:{site['line']} in function {site.get('function')}\n"
            f"Prompt template ({{{{var}}}} = runtime value):\n{msgs}\n\n"
            f"Enclosing code:\n```python\n{site['context'][:4000]}\n```\n\n"
            f"Places that call {site.get('function')}() (static search):\n{found}")


def _strs(x) -> list[str]:
    """A model may answer a list field with a bare string (or a dict); never iterate a string's characters."""
    if not x:
        return []
    return [str(v) for v in x] if isinstance(x, list) else [str(x)]


def _finish(data, steps: list[dict], callers: list[dict], mode: str) -> dict:
    data = data if isinstance(data, dict) else {}
    got = data.get("callers")
    got = [c if isinstance(c, dict) else {"where": str(c), "how": ""} for c in (got if isinstance(got, list) else [got] if got else [])]
    return {"mode": mode, "steps": steps,
            "callers": got or [{"where": c["where"], "how": ""} for c in callers],
            "downstream": str(data.get("downstream") or ""), "critical": _strs(data.get("critical"))[:8],
            "input_examples": [x[:600] for x in _strs(data.get("input_examples"))][:5], "notes": str(data.get("notes") or "")}


TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
TEXT_CALL = re.compile(r"<(toolcall|tool_call)>(.*?)</\1>", re.S | re.I)


def text_tool_calls(text: str) -> list[dict]:
    """Tool calls the server didn't parse and left in the text (`<TOOLCALL>[...]</TOOLCALL>`, `<tool_call>{...}`, bare JSON)."""
    blobs = [m.group(2) for m in TEXT_CALL.finditer(text or "")] or [text or ""]
    out = []
    for b in blobs:
        data = extract_json(b)
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("name") in TOOL_NAMES and not ({"callers", "downstream"} & set(item)):
                args = item.get("arguments", item.get("parameters", {}))
                out.append({"name": item["name"], "arguments": args if isinstance(args, str) else json.dumps(args or {})})
    return out


def investigate(tf: TokenFactory, root: Path, site: dict, max_steps: int = MAX_STEPS) -> dict:
    tools = RepoTools(root)
    callers = static_callers(tools, site)
    messages = [{"role": "system", "content": SYS}, {"role": "user", "content": _brief(site, callers)}]
    steps: list[dict] = []
    looked: list[str] = []   # fuller tool results, handed to the fallback if the agent runs out of steps
    for _ in range(max_steps):
        c = tf.complete(config.ROLE_ANALYZE, messages, role="investigate", temperature=0.1, reasoning=False,
                        max_tokens=2000, tools=TOOLS)
        if c.error:
            break  # endpoint refused tools (or failed): fall back below
        if not c.tool_calls and (as_text := text_tool_calls(c.text)):
            # the endpoint returned the calls as text: run them and hand back the results as a user turn
            results = []
            for tc in as_text:
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                args = args if isinstance(args, dict) else {}
                result = tools.call(tc["name"], args)
                steps.append({"tool": tc["name"], "args": args, "result": result[:300]})
                looked.append(f"{tc['name']}({json.dumps(args)}):\n{result[:1500]}")
                results.append(f"{tc['name']}({json.dumps(args)}):\n{result[:MAX_RESULT]}")
            messages += [{"role": "assistant", "content": c.text[:2000]},
                         {"role": "user", "content": "Tool results:\n\n" + "\n\n".join(results)}]
            continue
        if not c.tool_calls:
            data = extract_json(c.text)
            if data is not None:
                return _finish(data, steps, callers, "agent")
            messages += [{"role": "assistant", "content": c.text[:2000]},
                         {"role": "user", "content": "Reply with only the JSON described above."}]
            continue
        ids = [tc.get("id") or f"call_{len(steps)}_{i}" for i, tc in enumerate(c.tool_calls)]
        messages.append({"role": "assistant", "content": c.text or None, "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
            for cid, tc in zip(ids, c.tool_calls)]})
        for cid, tc in zip(ids, c.tool_calls):
            try:
                args = json.loads(tc["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            args = args if isinstance(args, dict) else {}
            result = tools.call(tc["name"], args)
            steps.append({"tool": tc["name"], "args": args, "result": result[:300]})
            looked.append(f"{tc['name']}({json.dumps(args)}):\n{result[:1500]}")
            messages.append({"role": "tool", "tool_call_id": cid, "content": result[:MAX_RESULT]})
    # no tools (or the agent ran out of steps): one plain call over the static findings plus
    # whatever the agent already looked up, so that work isn't thrown away
    brief = _brief(site, callers)
    if looked:
        brief += "\n\nWhat was already looked up:\n" + "\n\n".join(looked)[:12000]
    messages = [{"role": "system", "content": FALLBACK_SYS}, {"role": "user", "content": brief}]
    try:
        data, _ = tf.json(config.ROLE_ANALYZE, messages, role="investigate", temperature=0.1, reasoning=False, max_tokens=1500)
    except Exception:  # noqa: BLE001 - investigation is a bonus; analysis still runs without it
        data = {}
    return _finish(data, steps, callers, "static")


def as_context(inv: dict | None) -> str:
    """Findings as text for the analysis and test-writing prompts."""
    if not inv:
        return ""
    parts = []
    if inv.get("callers"):
        parts.append("Called from: " + "; ".join(f"{c.get('where')} ({c.get('how')})" if c.get("how") else str(c.get("where"))
                                                for c in inv["callers"][:6]))
    if inv.get("downstream"):
        parts.append(f"What the app does with the answer: {inv['downstream']}")
    if inv.get("critical"):
        parts.append("The app logic depends on: " + "; ".join(inv["critical"]))
    if inv.get("input_examples"):
        parts.append("Real inputs found in the repository:\n" + "\n".join(f"- {x}" for x in inv["input_examples"]))
    if inv.get("notes"):
        parts.append(f"Notes: {inv['notes']}")
    return "\n".join(parts)
