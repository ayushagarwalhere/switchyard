"""`switchyard deploy logs --explain`: Nemotron reads a failing app's logs and says why, in plain words.

It sees the log tail, the app's recent commits and its release history, and answers with what is failing,
the most likely cause (quoting the log lines that show it) and one next step, as a Switchyard command when
one fits: `deploy rollback`, `deploy restart`, or `code "<fix>"`.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .llm import TokenFactory

SYS = """You are an on-call engineer explaining a production problem to the developer who owns the app (not an expert).
Use only the evidence given: logs, recent commits and the release history. Never invent log lines.
Reply in plain text, at most 12 lines:
What's failing: one sentence.
Why: the most likely cause, quoting the 1-3 log lines that show it.
Fix now: ONE next step. Prefer a Switchyard command: `switchyard deploy rollback <app> <tag>` when a recent release
introduced the problem, `switchyard deploy restart <app>` for a stuck process, or `switchyard code "<the code fix>"`
when the code is wrong. Say "not enough evidence" if the logs don't show a cause."""

ERR = re.compile(r"error|exception|traceback|fatal|panic|refused|timeout|denied|\b5\d\d\b|failed|killed|oom", re.I)


def focus(logs: str, limit: int = 160) -> str:
    """The lines worth reading: errors plus a little context, then the tail."""
    lines = logs.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if ERR.search(line):
            keep.update(range(max(0, i - 2), min(len(lines), i + 4)))
    picked = [lines[i] for i in sorted(keep)][-limit:]
    tail = lines[-40:]
    out = picked + (["…", *tail] if picked and tail and tail[-1] not in picked else tail if not picked else [])
    return "\n".join(out)[-14000:]


def explain(logs: str, app: str | None = None, tf=None) -> str:
    from . import deploy
    name, rec = deploy._pick(app)
    commits = ""
    if rec.get("path") and Path(rec["path"]).is_dir():
        commits = subprocess.run(["git", "-C", rec["path"], "log", "-5", "--format=%h %ar %an: %s"],
                                 capture_output=True, text=True).stdout.strip()
    history = ", ".join(i.rsplit(":", 1)[-1] for i in rec.get("history") or []) or "unknown"
    brief = (f"App: {name}\nRunning release: {rec.get('image', '').rsplit(':', 1)[-1]}\nRelease history (oldest first): {history}\n"
             f"Recent commits:\n{commits or '(none)'}\n\nLogs:\n{focus(logs) or '(no log output)'}")
    tf = tf or TokenFactory(budget_usd=0.05)
    c = tf.complete("super", [{"role": "system", "content": SYS}, {"role": "user", "content": brief}],
                    role="diagnose", temperature=0.1, max_tokens=700, reasoning=False)
    if c.error:
        return f"(Nemotron couldn't be reached: {c.error})\n\n{focus(logs)[-3000:]}"
    return c.text.strip()
