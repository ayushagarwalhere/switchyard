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
name: {name}
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
      - uses: actions/checkout@v5
      - uses: ayushagarwalhere/switchyard@v0.2.0
        with:
          nebius-api-key: ${{{{ secrets.NEBIUS_API_KEY }}}}
          path: {path}
          run-file: {run_file}
"""


AUDIT_WORKFLOW = """\
# Added by `switchyard ci`. Audits every LLM call on each pull request (leaked keys, model output
# reaching eval/shell/SQL, user input in system prompts, missing timeouts and token caps) and puts
# the findings in the repository's Security tab. The static audit needs no key or secret.
name: switchyard-audit
on:
  push:
    branches: [main]
    paths: ["**/*.py"]
  pull_request:
    paths: ["**/*.py"]
  workflow_dispatch:

permissions:
  contents: read
  security-events: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: ayushagarwalhere/switchyard@v0.2.0
        id: audit
        with:
          command: audit
          path: {path}
          fail-on: critical
      - uses: github/codeql-action/upload-sarif@v4
        if: always() && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)
        with:
          sarif_file: ${{{{ steps.audit.outputs.sarif-file }}}}
"""


def add_ci(path: str = ".") -> list[str]:
    """Write the audit workflow (and the check workflow if a migration's tests are saved) into a checkout."""
    given = Path(path).resolve()
    root = toplevel(given)
    if root is None:
        raise RuntimeError(f"{given} is not inside a git checkout")
    sub = "" if given == root else given.relative_to(root).as_posix()
    written = []
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", sub).strip("-").lower()
    audit_wf = root / ".github" / "workflows" / (f"switchyard-audit-{slug}.yml" if sub else "switchyard-audit.yml")
    if not audit_wf.exists():
        audit_wf.parent.mkdir(parents=True, exist_ok=True)
        audit_wf.write_text(AUDIT_WORKFLOW.format(path=json.dumps(sub or ".")), encoding="utf-8")
        written.append(audit_wf.relative_to(root).as_posix())
    run_file, wf_file = ci_paths(sub)
    if (root / run_file).exists() and not (root / wf_file).exists():
        name = "switchyard-check" + (f"-{slug}" if sub else "")
        (root / wf_file).write_text(WORKFLOW.format(name=name, path=json.dumps(sub or "."), run_file=json.dumps(run_file)),
                                    encoding="utf-8")
        written.append(wf_file)
    return written


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:800]}")
    return r


def toplevel(path: str | Path = ".") -> Path | None:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def resolve(ref: str | None, repo: str | None, subdir: str | None) -> tuple[dict, Path, Path, str]:
    """Fill in what the developer left out: the latest run, the checkout it came from, the folder offset."""
    if ref not in (None, "", "latest") and repo is None and Path(ref).is_dir() and not (Path(ref) / "run.json").exists():
        ref, repo = None, ref          # `switchyard apply path/to/checkout`
    if ref in (None, "", "latest"):
        # the latest run *for this checkout*, so another project's run is never applied here
        ref = _latest_for(Path(repo) if repo else Path("."))
    state, run_dir = load_run(ref)
    co = state.get("checkout") or {}
    if repo:
        given = Path(repo).resolve()
        top = toplevel(given)
        root = top or given
        if subdir is None:
            if co.get("root") and Path(co["root"]).resolve() == root:
                subdir = co.get("subdir", "")                       # same checkout the run scanned
            elif given != root:
                subdir = given.relative_to(root).as_posix()         # pointed at the app's folder
            else:                                                   # another clone, or a copy of the app
                changed = (state.get("patch") or {}).get("changed") or []
                options = [state.get("source_subdir") or "", co.get("subdir") or "", ""]
                subdir = next((o for o in options if changed and all((root / o / f).exists() for f in changed)),
                              options[0] or options[1])
    elif co.get("root"):
        root = Path(co["root"])
        subdir = co.get("subdir", "") if subdir is None else subdir
    else:
        raise RuntimeError("this run was made from a URL; say which checkout to apply it to: "
                           "switchyard apply <run> <path to your clone>")
    return state, run_dir, root, (subdir or "").strip("/")


def _latest_for(path: Path) -> str:
    from .check import latest_run
    top = toplevel(path)
    found = latest_run(root=top) if top else None
    if found:
        return str(found)
    where = f" for {top}" if top else ""
    raise FileNotFoundError(f"no finished runs{where}. Start one with `switchyard run`, "
                            "or name a run: switchyard apply <run-id> [checkout]")


def ci_paths(subdir: str) -> tuple[str, str]:
    """Where the saved tests and the CI workflow go. One pair per app, so a monorepo can hold several."""
    if not subdir:
        return ".switchyard/run.json", ".github/workflows/switchyard-check.yml"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", subdir).strip("-").lower()
    return f".switchyard/{subdir}/run.json", f".github/workflows/switchyard-check-{slug}.yml"


def ci_copy(state: dict) -> str:
    """The run as CI needs it: tests, analyses, decisions, pass rates.

    Left out: the event log, model outputs, runtime request logs and paths from this machine.
    """
    keep = {k: v for k, v in state.items() if k not in ("events", "checkout", "pr_body", "notes")}
    src = keep.get("source", "")
    if not re.match(r"^(https?://|git@)", src):
        keep["source"] = Path(src).name or "local"
    sites = []
    for s in keep.get("sites", []):
        s = dict(s)
        s["evals"] = {k: {kk: vv for kk, vv in v.items() if kk != "results"} for k, v in (s.get("evals") or {}).items()}
        sites.append(s)
    keep["sites"] = sites
    if keep.get("patch"):
        keep["patch"] = {k: v for k, v in keep["patch"].items() if k not in ("runtime", "diff", "tests")}
    return json.dumps(keep, indent=1, default=str)


def apply(ref: str | None = None, repo: str | None = None, branch: str | None = None,
          subdir: str | None = None, ci: bool = True, whole_tree_clean: bool = False) -> dict:
    """Create a branch in the checkout and apply the migration patch (no commit)."""
    state, run_dir, root, subdir = resolve(ref, repo, subdir)
    patch = run_dir / "migration.patch"
    if not patch.exists() or not patch.read_text(encoding="utf-8").strip():
        raise RuntimeError("this run has no patch (no call sites were switched)")
    if toplevel(root) is None:
        raise RuntimeError(f"{root} is not a git checkout")
    run_file, wf_file = ci_paths(subdir)
    # only what this apply touches has to be clean; unrelated work elsewhere in a monorepo can stay
    scope = [] if whole_tree_clean else ["--", subdir or ".", run_file, wf_file]
    dirty = _git(root, "status", "--porcelain", *scope).stdout.strip()
    if dirty:
        raise RuntimeError(f"uncommitted changes in {root / subdir if subdir else root}; commit or stash them first:\n{dirty[:600]}")
    branch = branch or f"switchyard/nemotron-{state['id']}"
    if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RuntimeError(f"branch {branch} already exists (this run was applied before). "
                           f"Switch to it with `git checkout {branch}`, or delete it to re-apply")
    start = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    start_sha = _git(root, "rev-parse", "HEAD", check=False).stdout.strip()
    _git(root, "checkout", "-b", branch)
    args = ["apply", "--whitespace=nowarn"] + ([f"--directory={subdir}"] if subdir else []) + [str(patch)]
    r = _git(root, *args, check=False)
    if r.returncode != 0:
        _git(root, "checkout", *(["--detach", start_sha] if start == "HEAD" else [start]), check=False)
        _git(root, "branch", "-D", branch, check=False)
        raise RuntimeError(f"git apply failed (is the checkout at the scanned commit {state.get('commit') or ''}?):\n{r.stderr}")
    app_dir = root / subdir if subdir else root
    shutil.copy(run_dir / "migration.env", app_dir / ".env.switchyard.example")
    # keep the test set with the code so `switchyard check` works in CI
    (root / run_file).parent.mkdir(parents=True, exist_ok=True)
    (root / run_file).write_text(ci_copy(state), encoding="utf-8")
    wf = root / wf_file
    added_ci = False
    if ci and not wf.exists():
        wf.parent.mkdir(parents=True, exist_ok=True)
        name = "switchyard-check" + (f"-{Path(wf_file).stem.split('switchyard-check-')[-1]}" if subdir else "")
        wf.write_text(WORKFLOW.format(name=name, path=json.dumps(subdir or "."), run_file=json.dumps(run_file)), encoding="utf-8")
        added_ci = True
    changed = _git(root, "status", "--porcelain", "-uall").stdout.split("\n")
    return {"branch": branch, "root": str(root), "subdir": subdir, "run_file": run_file,
            "workflow": wf_file if added_ci else None, "traces": bool((state.get("settings") or {}).get("traces")),
            "changed": [c[3:] for c in changed if c.strip()], "pr_body": run_dir / "PR.md"}


def open_pr(ref: str | None = None, repo: str | None = None, base: str | None = None,
            subdir: str | None = None, ci: bool = True) -> str:
    """apply() + commit + push + `gh pr create` with Switchyard's report as the description."""
    if not shutil.which("gh"):
        raise RuntimeError("opening a PR needs the GitHub CLI (https://cli.github.com); "
                           "or use `switchyard apply` and push the branch yourself")
    res = apply(ref, repo, subdir=subdir, ci=ci, whole_tree_clean=True)   # everything gets committed
    root = Path(res["root"])
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory (Switchyard)")
    _git(root, "push", "-u", "origin", res["branch"])
    body = Path(res["pr_body"]).read_text(encoding="utf-8")
    if not res["workflow"]:
        _, wf_file = ci_paths(res["subdir"])
        body += (f"\n\n### Keep it honest in CI\n\nCopy [examples/switchyard-check.yml]"
                 f"(https://github.com/ayushagarwalhere/switchyard/blob/main/examples/switchyard-check.yml) to `{wf_file}` "
                 f"(with `run-file: {res['run_file']}`) and add a `NEBIUS_API_KEY` secret.\n")
    body_file = Path(res["pr_body"]).with_name("PR.final.md")
    body_file.write_text(body[:60000], encoding="utf-8")       # GitHub's body limit is 65,536 characters
    cmd = ["gh", "pr", "create", "--title", "Move LLM calls to NVIDIA Nemotron on Nebius Token Factory",
           "--body-file", str(body_file), "--head", res["branch"]] + (["--base", base] if base else [])
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"branch {res['branch']} was pushed, but `gh pr create` failed: {(r.stderr or r.stdout).strip()[:800]}")
    m = re.search(r"https://\S+", r.stdout)
    return m.group(0) if m else r.stdout.strip()
