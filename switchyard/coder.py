"""`switchyard code "<change>"`: a Nemotron coding agent that edits an existing app.

It reads and searches the code, writes files (inside the repo only), runs the app's tests, and keeps going
until they pass. Changes are left uncommitted so you can review them with `git diff`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from .investigate import text_tool_calls as _text_calls  # noqa: F401  (same text-call fallback format)
from .llm import TokenFactory, extract_json
from .secure import SKIP_DIRS, iter_files

MAX_RESULT = 6000

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List the repository's source files with line counts.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file (optionally a line range, max 400 lines per call).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                                        "end": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Regular-expression search over the repository's source files. Returns file:line: text.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file with its COMPLETE new content.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_tests", "description": "Run the project's tests (pytest or npm test). Returns pass/fail and the output tail.",
        "parameters": {"type": "object", "properties": {}}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

SYS = """You are a careful senior engineer making one change to an existing app for a non-expert ("vibe coder").
Work in small steps with the tools: look at the relevant files first, then write complete files, then run the tests and fix failures.
Rules:
- Keep the existing style, structure and features; change only what the request needs.
- Always write the COMPLETE file content with write_file (never a diff or a fragment).
- Never hard-code secrets, never turn on debug mode, never pass model output to eval/exec/shell/SQL.
- If you add an AI feature, follow the app's existing LLM client pattern (one function per feature with its own system prompt, max_tokens set).
- Add or update a test when you add behaviour.
When the change is done and the tests pass, reply WITHOUT a tool call, with JSON only:
{"summary": "what you changed, in one or two plain sentences", "files": ["paths you changed"]}"""


class RepoTools:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.written: list[str] = []

    def _path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root) or any(part in SKIP_DIRS for part in p.relative_to(self.root).parts):
            raise ValueError(f"{rel} is outside the app")
        return p

    def call(self, name: str, args: dict) -> str:
        try:
            return getattr(self, "t_" + name)(**args) if name in TOOL_NAMES else f"unknown tool {name}"
        except (ValueError, OSError, TypeError, re.error) as e:
            return f"error: {e}"

    def t_list_files(self) -> str:
        rows = []
        for p in iter_files(self.root):
            try:
                n = p.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            except OSError:
                continue
            rows.append(f"{p.relative_to(self.root).as_posix()} ({n} lines)")
        return "\n".join(rows[:300]) or "(no files)"

    def t_read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        lines = self._path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 399), start + 399)
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1)) or "(empty file)"

    def t_search(self, pattern: str) -> str:
        rx = re.compile(pattern)
        hits = []
        for p in iter_files(self.root):
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(self.root).as_posix()}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 80:
                        return "\n".join(hits)
        return "\n".join(hits) or "no matches"

    def t_write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.name.lower().startswith(".env") and not p.name.lower().endswith(".example"):
            return "error: don't write .env files; put example values in .env.example"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        rel = p.relative_to(self.root).as_posix()
        if rel not in self.written:
            self.written.append(rel)
        return f"wrote {rel} ({content.count(chr(10)) + 1} lines)"

    def t_run_tests(self) -> str:
        ok, out = run_tests(self.root)
        return ("PASS\n" if ok else "FAIL\n") + out[-3000:]


def run_tests(root: Path, timeout: int = 180) -> tuple[bool, str]:
    import os
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and "TOKEN_FACTORY" not in k}
    if (root / "tests").exists() or list(root.glob("test_*.py")):
        cmd = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
    elif (root / "package.json").exists() and '"test"' in (root / "package.json").read_text(encoding="utf-8", errors="replace"):
        cmd = ["npm", "test", "--silent"]
    else:
        return True, "no tests in this project"
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, env=env,
                           shell=(cmd[0] == "npm" and os.name == "nt"))
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"tests didn't finish: {e}"
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def code(task: str, path: str | Path = ".", max_steps: int = 24, budget: float = 0.6, emit=print, tf=None) -> dict:
    root = Path(path).resolve()
    tf = tf or TokenFactory(budget_usd=budget)
    tools = RepoTools(root)
    messages = [{"role": "system", "content": SYS},
                {"role": "user", "content": f"Change requested: {task}\n\nFiles:\n{tools.t_list_files()}"}]
    summary, steps, green = None, 0, 0
    for steps in range(1, max_steps + 1):
        c = tf.complete("super", messages, role="code", temperature=0.2, reasoning=False, max_tokens=8000, tools=TOOLS)
        if c.error:
            raise RuntimeError(f"Token Factory error: {c.error}")
        calls = c.tool_calls or [tc for tc in _calls_in_text(c.text)]
        if not calls:
            data = extract_json(c.text)
            summary = (data or {}).get("summary") if isinstance(data, dict) else None
            summary = summary or c.text.strip()[:400]
            break
        if c.tool_calls:
            ids = [tc.get("id") or f"call_{steps}_{i}" for i, tc in enumerate(calls)]
            messages.append({"role": "assistant", "content": c.text or None, "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
                for cid, tc in zip(ids, calls)]})
        else:
            messages.append({"role": "assistant", "content": c.text[:3000]})
            ids = [None] * len(calls)
        text_results = []
        passed = False
        for cid, tc in zip(ids, calls):
            try:
                args = json.loads(tc["arguments"] or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            args = args if isinstance(args, dict) else {}
            shown = args.get("path") or args.get("pattern") or ""
            emit(f"  {tc['name']} {shown}".rstrip())
            result = tools.call(tc["name"], args)[:MAX_RESULT]
            passed = passed or (tc["name"] == "run_tests" and result.startswith("PASS") and bool(tools.written))
            if cid:
                messages.append({"role": "tool", "tool_call_id": cid, "content": result})
            else:
                text_results.append(f"{tc['name']}: {result}")
        if text_results:
            messages.append({"role": "user", "content": "Tool results:\n\n" + "\n\n".join(text_results)})
        if passed:
            green += 1
            if green >= 2:      # it keeps polishing after the tests went green: stop here
                summary = f"Changed {', '.join(tools.written)}; the tests pass."
                break
            messages.append({"role": "user", "content": "The tests pass. If the requested change is complete, reply now "
                                                        "with only the JSON summary (no more tool calls)."})
    ok, log = run_tests(root)
    from . import secure
    sec = secure.run(root, deps=False)
    diff = subprocess.run(["git", "-C", str(root), "diff", "--stat"], capture_output=True, text=True).stdout.strip()
    return {"summary": summary or "stopped before finishing (step limit)", "files": tools.written, "steps": steps,
            "tests_ok": ok, "test_log": "" if ok else log[-1500:], "diff_stat": diff,
            "blocking": len(sec["blocking"]), "usage": tf.usage.to_dict()}


def _calls_in_text(text: str) -> list[dict]:
    """Tool calls left in the text by the server (`<tool_call>{...}</tool_call>` or bare JSON)."""
    blobs = [m.group(2) for m in re.finditer(r"<(toolcall|tool_call)>(.*?)</\1>", text or "", re.S | re.I)] or [text or ""]
    out = []
    for b in blobs:
        data = extract_json(b)
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("name") in TOOL_NAMES:
                args = item.get("arguments", item.get("parameters", {}))
                out.append({"name": item["name"], "arguments": args if isinstance(args, str) else json.dumps(args or {})})
    return out
