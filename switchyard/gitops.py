"""Apply a migration to a developer's own checkout, and optionally open a pull request."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .check import load_run

WORKFLOW = """\
# Added by Switchyard. Re-runs the saved tests for every call switched to Nemotron,
# nightly and on Python changes, so a call that drifts below the bar fails CI instead
# of failing users. Needs a NEBIUS_API_KEY repository secret.
name: switchyard-check
on:
  schedule:
    - cron: "0 3 * * *"
  pull_request:
    paths: ["**/*.py", ".switchyard/**"]
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ayushagarwalhere/switchyard@main
        with:
          nebius-api-key: ${{{{ secrets.NEBIUS_API_KEY }}}}
          path: {path}
"""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def resolve(ref: str | None, repo: str | None, subdir: str | None) -> tuple[dict, Path, Path, str]:
    """Fill in what the developer left out: the latest run, the checkout it came from, the folder offset."""
    state, run_dir = load_run(ref)
    co = state.get("checkout") or {}
    if repo:
        given = Path(repo).resolve()
        top = _git(given, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        root = Path(top).resolve() if top else given
        if subdir is None:
            if co.get("root") and Path(co["root"]).resolve() == root:
                subdir = co.get("subdir", "")                       # same checkout the run scanned
            elif given != root:
                subdir = given.relative_to(root).as_posix()         # pointed at the app's folder
            else:
                subdir = state.get("source_subdir", "")             # .../tree/<branch>/<dir> URL runs
    elif co.get("root"):
        root = Path(co["root"])
        subdir = co.get("subdir", "") if subdir is None else subdir
    else:
        raise RuntimeError("this run was made from a URL; say which checkout to apply it to: "
                           "switchyard apply <run> <path to your clone>")
    return state, run_dir, root, (subdir or "").strip("/")


def _ci_copy(state: dict) -> str:
    """The run as CI needs it: tests, analyses, decisions. No event log, no paths from this machine."""
    keep = {k: v for k, v in state.items() if k not in ("events", "checkout", "pr_body")}
    src = keep.get("source", "")
    if not re.match(r"^(https?://|git@)", src):
        keep["source"] = Path(src).name or "local"
    return json.dumps(keep, indent=1, default=str)


def apply(ref: str | None = None, repo: str | None = None, branch: str | None = None,
          subdir: str | None = None, ci: bool = True) -> dict:
    """Create a branch in the checkout and apply the migration patch (no commit)."""
    state, run_dir, root, subdir = resolve(ref, repo, subdir)
    patch = run_dir / "migration.patch"
    if not patch.exists() or not patch.read_text(encoding="utf-8").strip():
        raise RuntimeError("this run has no patch (no call sites were switched)")
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise RuntimeError(f"{root} is not a git checkout")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError(f"{root} has uncommitted changes; commit or stash them first")
    branch = branch or f"switchyard/nemotron-{state['id']}"
    start = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(root, "checkout", "-b", branch)
    args = ["apply", "--whitespace=nowarn"] + ([f"--directory={subdir.strip('/')}"] if subdir else []) + [str(patch)]
    r = _git(root, *args, check=False)
    if r.returncode != 0:
        _git(root, "checkout", start, check=False)
        _git(root, "branch", "-D", branch, check=False)
        raise RuntimeError(f"git apply failed (is the checkout at the scanned commit {state.get('commit') or ''}?):\n{r.stderr}")
    app_dir = root / subdir if subdir else root
    shutil.copy(run_dir / "migration.env", app_dir / ".env.switchyard.example")
    # keep the test set with the code so `switchyard check` works in CI
    (root / ".switchyard").mkdir(exist_ok=True)
    (root / ".switchyard" / "run.json").write_text(_ci_copy(state), encoding="utf-8")
    wf = root / ".github" / "workflows" / "switchyard-check.yml"
    if ci and not wf.exists():
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(WORKFLOW.format(path=json.dumps(subdir or ".")), encoding="utf-8")
    changed = _git(root, "status", "--porcelain", "-uall").stdout.split("\n")
    return {"branch": branch, "root": str(root), "subdir": subdir,
            "changed": [c[3:] for c in changed if c.strip()], "pr_body": run_dir / "PR.md"}


def open_pr(ref: str | None = None, repo: str | None = None, base: str | None = None,
            subdir: str | None = None, ci: bool = True) -> str:
    """apply() + commit + push + `gh pr create` with Switchyard's report as the description."""
    if not shutil.which("gh"):
        raise RuntimeError("opening a PR needs the GitHub CLI (https://cli.github.com); "
                           "or use `switchyard apply` and push the branch yourself")
    res = apply(ref, repo, subdir=subdir, ci=ci)
    root = Path(res["root"])
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory (Switchyard)")
    _git(root, "push", "-u", "origin", res["branch"])
    cmd = ["gh", "pr", "create", "--title", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory",
           "--body-file", str(res["pr_body"]), "--head", res["branch"]] + (["--base", base] if base else [])
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    m = re.search(r"https://\S+", out)
    return m.group(0) if m else out
