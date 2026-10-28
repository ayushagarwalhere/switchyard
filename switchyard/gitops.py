"""Apply a migration to a developer's own checkout, and optionally open a pull request."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .check import load_run


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def apply(ref: str, repo: str, branch: str | None = None, subdir: str = "") -> dict:
    """Create a branch in `repo` and apply the migration patch (no commit)."""
    state, run_dir = load_run(ref)
    patch = run_dir / "migration.patch"
    if not patch.exists() or not patch.read_text(encoding="utf-8").strip():
        raise RuntimeError("this run has no patch (no call sites were switched)")
    root = Path(repo).resolve()
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise RuntimeError(f"{root} is not a git checkout")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("working tree has uncommitted changes; commit or stash them first")
    branch = branch or f"switchyard/nemotron-{state['id']}"
    _git(root, "checkout", "-b", branch)
    args = ["apply", "--whitespace=nowarn"] + ([f"--directory={subdir.strip('/')}"] if subdir else []) + [str(patch)]
    r = _git(root, *args, check=False)
    if r.returncode != 0:
        _git(root, "checkout", "-", check=False)
        _git(root, "branch", "-D", branch, check=False)
        raise RuntimeError(f"git apply failed (is the checkout at the scanned commit {state.get('commit') or ''}?):\n{r.stderr}")
    shutil.copy(run_dir / "migration.env", root / ".env.switchyard.example")
    changed = _git(root, "status", "--porcelain").stdout.split("\n")
    return {"branch": branch, "changed": [c[3:] for c in changed if c.strip()], "pr_body": run_dir / "PR.md"}


def open_pr(ref: str, repo: str, base: str | None = None, subdir: str = "") -> str:
    """apply() + commit + push + `gh pr create` with Switchyard's report as the description."""
    res = apply(ref, repo, subdir=subdir)
    root = Path(repo).resolve()
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory (Switchyard)")
    _git(root, "push", "-u", "origin", res["branch"])
    cmd = ["gh", "pr", "create", "--title", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory",
           "--body-file", str(res["pr_body"]), "--head", res["branch"]] + (["--base", base] if base else [])
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    m = re.search(r"https://\S+", out)
    return m.group(0) if m else out
