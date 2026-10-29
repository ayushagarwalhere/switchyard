"""`switchyard mcp`: Switchyard as an MCP server, for Cursor, Claude Desktop, Windsurf and other MCP clients.

Client config:
    {"mcpServers": {"switchyard": {"command": "switchyard", "args": ["mcp"]}}}

The tools are plain functions (testable without MCP). Anything that spends money needs an explicit
`max_spend_usd` that the agent has to get from the user, and keys never pass through the tools:
the server reads them from the environment, the project's .env, or ~/.switchyard/.env.
"""
from __future__ import annotations

import threading
from pathlib import Path

from . import config, scanner

INSTRUCTIONS = (
    "Switchyard is a build-and-ship toolkit for apps on NVIDIA Nemotron and Nebius. Build: create_app writes a "
    "working, tested app from one sentence; change_app makes a change to an existing app and runs its tests. "
    "Secure: secure_app checks secrets, vulnerable dependencies, risky config and every LLM call (free). "
    "Optimize: scan_llm_calls (free) -> show the calls and estimated cost -> start_migration only after the user "
    "agrees to a spend limit -> migration_status until done -> migration_results -> apply_migration. "
    "Route live: routing_table + `switchyard router`. Review: review_pull_request before merging AI code. "
    "Keep it running: oncall_setup, then `switchyard oncall`. "
    "Deploy: deploy_app ships the app to a Nebius Serverless endpoint (it bills until deleted: tell the user, and "
    "use delete_deployment when they're done). Long tasks return a job_id: poll job_status. Never ask the user to "
    "paste an API key into the chat; if a key is missing, ask them to run `switchyard setup` in a terminal.")

_RUNS: dict[str, object] = {}
_lock = threading.Lock()


def _root(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("pass the project's absolute path (the server's working directory isn't your project)")
    if not p.is_dir():
        raise ValueError(f"{p} is not a folder")
    return p.resolve()


def _no_key() -> dict:
    return {"error": "no Token Factory key configured",
            "ask_user": "Please run `switchyard setup` in a terminal (it asks for the key with hidden input), "
                        "or add NEBIUS_API_KEY to the project's .env."}


def scan_llm_calls(path: str, tests_per_call: int = 8) -> dict:
    """Find every LLM call in a Python or JS/TS project (static analysis, free, no key). Returns each call's file,
    line, function, SDK, current model and prompt variables, which calls can't be tested and why, and the
    estimated Token Factory cost of testing them on Nemotron."""
    from .pipeline import estimate_cost
    res = scanner.scan_repo(_root(path))
    testable = [c for c in res["calls"] if not c.get("skip")]
    return {
        "files": res["files"], "lines": res["loc"],
        "calls": [{"file": c["file"], "line": c["line"], "function": c["function"], "sdk": c["sdk"],
                   "model": c["model"] or c["model_expr"], "variables": c["variables"], "notes": c["notes"]}
                  for c in testable],
        "not_testable": [{"file": c["file"], "line": c["line"], "reason": c["skip"]} for c in res["calls"] if c.get("skip")],
        "estimated_cost_usd": estimate_cost(testable[:12], tests_per_call),
        "key_configured": bool(config.api_key()),
    }


def audit_llm_calls(path: str, probes: bool = False, max_spend_usd: float = 0.0) -> dict:
    """Security and reliability audit of every LLM call. Static checks are free: API keys in code, model output
    reaching eval/exec/shell/SQL, user input in system prompts, missing timeouts and token caps, unguarded
    json.loads. With probes=true (costs a few cents; set max_spend_usd), each prompt is attacked on Nemotron
    with injection, system-prompt-leak and exfiltration probes, and a suggested fix is re-tested."""
    from . import audit
    root = _root(path)
    res = scanner.scan_repo(root)
    findings = audit.static(root, res)
    note = None
    if probes:
        if not config.api_key():
            return {**_no_key(), "findings": findings}
        if max_spend_usd <= 0:
            note = "probes skipped: set max_spend_usd (e.g. 0.25) after the user agrees"
        else:
            from .llm import TokenFactory
            tf = TokenFactory(budget_usd=max_spend_usd)
            notes: list = []
            findings = audit.sort(findings + audit.probes(tf, res, notes=notes))
            note = "; ".join(notes + [f"probes cost ${tf.usage.to_dict()['cost']:.4f}"])
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in audit.SEVERITY_ORDER}
    return {"counts": counts, "findings": findings, "note": note}


def start_migration(path: str, max_spend_usd: float, tests_per_call: int = 8, apply_when_done: bool = False) -> dict:
    """Start a full migration run in the background (minutes). Spends up to max_spend_usd on Token Factory,
    which the user must have agreed to after seeing scan_llm_calls' estimate. Returns a run_id; poll
    migration_status. With apply_when_done=true the patch is put on a new git branch at the end (no commit)."""
    root = _root(path)
    if not config.api_key():
        return _no_key()
    if not max_spend_usd or max_spend_usd <= 0:
        return {"error": "max_spend_usd is required: show the user scan_llm_calls' estimate and ask for a limit"}
    from .pipeline import Run
    run = Run(str(root), tests=tests_per_call, budget=max_spend_usd)
    with _lock:
        _RUNS[run.id] = run

    def work():
        state = run.run()
        if apply_when_done and state.get("status") == "done" and (state.get("patch") or {}).get("changed"):
            from .gitops import apply
            try:
                state["applied"] = {k: str(v) for k, v in apply(str(run.dir)).items()}
            except (RuntimeError, FileNotFoundError) as e:
                state["applied"] = {"error": str(e)}

    threading.Thread(target=work, daemon=True, name=f"switchyard-{run.id}").start()
    return {"run_id": run.id, "status": "running", "max_spend_usd": max_spend_usd,
            "next": "call migration_status(run_id) every 20-30 seconds"}


def migration_status(run_id: str) -> dict:
    """Progress of a migration run: status, current step, per-call decisions so far, spend so far."""
    run = _RUNS.get(run_id)
    if run is None:
        return migration_results(run_id)
    st = run.state
    events = st.get("events", [])
    steps = [e.get("message") for e in events if e.get("kind") == "step"]
    decided = [e for e in events if e.get("kind") == "decision"]
    return {"run_id": run_id, "status": st.get("status"), "step": steps[-1] if steps else None,
            "calls_decided": len(decided), "calls_total": next((e["calls"] for e in events if e.get("kind") == "scan"), None),
            "spent_usd": round((st.get("usage") or {}).get("cost", 0.0), 4),
            "done": st.get("status") != "running", "applied": st.get("applied")}


def migration_results(run_id: str = "latest", project_path: str | None = None) -> dict:
    """Results of a finished run: per call, switched to which Nemotron model or kept and why; savings;
    runtime verification of the patched code; security audit counts; paths to the patch and PR report."""
    from . import Result
    from .check import load_run
    ref = run_id
    if ref in ("", "latest"):
        from .gitops import _latest_for
        ref = _latest_for(_root(project_path) if project_path else Path("."))
    state, run_dir = load_run(ref)
    res = Result(state, run_dir)
    runtime = (state.get("patch") or {}).get("runtime") or []
    findings = (state.get("audit") or {}).get("findings") or []
    return {"run_id": res.id, "status": state.get("status"), "error": state.get("error"),
            "summary": res.summary, "decisions": res.decisions,
            "runtime": {k: sum(1 for r in runtime if r.get("status") == k) for k in ("verified", "failed", "unverified")},
            "audit": {s: sum(1 for f in findings if f["severity"] == s) for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")},
            "manual_steps": (state.get("patch") or {}).get("manual", []), "skipped": state.get("skipped", []),
            "cost_usd": round(res.cost, 4), "patch": str(res.patch_path) if res.patch_path else None,
            "report": str(run_dir / "PR.md") if (run_dir / "PR.md").exists() else None}


def apply_migration(run_id: str = "latest", project_path: str | None = None) -> dict:
    """Put a finished run's patch on a new git branch in the project (switchyard/nemotron-<run>), with the
    saved tests and a CI workflow. Nothing is committed; the user reviews with git diff."""
    from .gitops import apply
    ref = None if run_id in ("", "latest") else run_id
    if ref is None and not project_path:
        return {"error": "pass project_path (absolute) so the latest run for that project is used"}
    res = apply(ref, str(_root(project_path)) if project_path else None)
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in res.items()}


# -- build, secure, deploy --------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}


def _job(kind: str, fn) -> dict:
    import uuid
    jid = f"{kind}-{uuid.uuid4().hex[:6]}"
    rec = {"job_id": jid, "kind": kind, "status": "running", "log": [], "result": None, "error": None}
    with _lock:
        _JOBS[jid] = rec

    def work():
        try:
            rec["result"] = fn(lambda *a: rec["log"].append(" ".join(str(x) for x in a)))
            rec["status"] = "done"
        except Exception as e:  # noqa: BLE001 - reported to the agent
            rec["status"], rec["error"] = "failed", str(e)

    threading.Thread(target=work, daemon=True, name=f"switchyard-{jid}").start()
    return {"job_id": jid, "status": "running", "next": "call job_status(job_id) every 20-30 seconds"}


def job_status(job_id: str) -> dict:
    """Progress and result of a create_app / change_app / deploy_app job."""
    rec = _JOBS.get(job_id)
    if rec is None:
        return {"error": f"unknown job {job_id}"}
    return {**{k: rec[k] for k in ("job_id", "kind", "status", "result", "error")}, "log": rec["log"][-15:]}


def create_app(idea: str, parent_folder: str, max_spend_usd: float = 0.6, folder_name: str | None = None) -> dict:
    """Nemotron writes a working web app (FastAPI + HTML/JS, AI features on Token Factory) from one sentence, with a
    Dockerfile and tests, tests it and fixes failures. Costs about one cent; max_spend_usd caps it. Returns a job_id."""
    parent = _root(parent_folder)
    if not config.api_key():
        return _no_key()
    from .create import Creator
    dest = parent / folder_name if folder_name else None

    def fn(emit):
        return Creator(idea, dest, budget=max_spend_usd, emit=emit, parent=parent).build()
    return _job("create", fn)


def change_app(path: str, change: str, max_spend_usd: float = 0.6) -> dict:
    """A Nemotron coding agent makes a change to the app (reads, writes files inside it, runs the tests, fixes
    failures). Nothing is committed; the user reviews with git diff. Returns a job_id."""
    root = _root(path)
    if not config.api_key():
        return _no_key()
    from .coder import code
    return _job("change", lambda emit: code(change, root, budget=max_spend_usd, emit=emit))


def secure_app(path: str, check_dependencies: bool = True) -> dict:
    """Whole-app security check before shipping (free): leaked secrets anywhere, .env committed to git, vulnerable
    dependencies (OSV), debug mode, open CORS, containers running as root, and every LLM call."""
    from . import secure
    res = secure.run(_root(path), deps=check_dependencies)
    return {"blocking": len(res["blocking"]), "findings": res["findings"], "notes": res["notes"]}


def deploy_app(path: str, user_agreed_to_billing: bool, name: str | None = None, private: bool = False) -> dict:
    """Ship the app to Nebius: security gate, Dockerfile if missing, build, push to Nebius Container Registry, create
    a Serverless endpoint, health-check it. The endpoint bills until deleted: only call with
    user_agreed_to_billing=true after telling the user. Needs Docker and the nebius CLI. Returns a job_id."""
    root = _root(path)
    if not user_agreed_to_billing:
        return {"error": "tell the user the endpoint bills until deleted, then call again with user_agreed_to_billing=true"}
    from .deploy import Deployer
    return _job("deploy", lambda emit: Deployer(root, name, private=private, emit=emit).run())


def deployment_status(name: str | None = None) -> dict:
    """State and URL of a deployed app (all deployed apps are listed if name is ambiguous)."""
    from . import deploy
    try:
        return deploy.status(name)
    except deploy.DeployError as e:
        return {"error": str(e)}


def delete_deployment(name: str) -> dict:
    """Delete a deployed app's Nebius endpoint (it stops billing)."""
    from . import deploy
    try:
        return {"deleted": deploy.delete(name)}
    except deploy.DeployError as e:
        return {"error": str(e)}


def review_pull_request(path: str, base: str | None = None, head: str = "HEAD", monthly_calls: int = 100_000) -> dict:
    """Review a branch or PR's AI code (static, free): LLM calls added/changed, cost per month at the given volume vs
    Nemotron, which Nemotron tier is worth testing, and security issues on the changed lines only. Returns the
    Markdown comment too."""
    from . import prreview
    res = prreview.review(_root(path), base=base, head=head, volume=monthly_calls)
    return {"verdict": prreview.verdict(res), "worst": prreview.fail_level(res), "markdown": prreview.markdown(res)}


def routing_table(run_id: str | None = None) -> dict:
    """The live router's routes (feature -> Nemotron model + fallback chain), optionally from a finished migration
    run, plus how to point an app at `switchyard router`."""
    from . import router
    routes = router.load_routes()
    if run_id:
        routes.update(router.routes_from_run(run_id))
    return {"routes": {k: {"model": v.get("model"), "fallback": v.get("fallback")} for k, v in routes.items()},
            "start": "switchyard router" + (f" --run {run_id}" if run_id else ""), "snippet": router.snippet()}


def oncall_setup(require_voice: bool = False) -> dict:
    """Write the on-call (WispOps) config for every deployed app: health URL, restart, rollback, release and logs via
    `switchyard deploy`. The user starts the agent with `switchyard oncall` in a terminal."""
    from . import oncall
    cfg = oncall.write_config(require_voice)
    return {"config": str(oncall.CONFIG), "apps": [s["id"] for s in cfg["services"] if s.get("_switchyard")],
            "start": "switchyard oncall" + (" --voice" if require_voice else ""),
            "approvals": "Wispr Flow say-back" if require_voice else "typed"}


TOOLS = [review_pull_request, routing_table, oncall_setup, create_app, change_app, secure_app, scan_llm_calls, audit_llm_calls, start_migration, migration_status,
         migration_results, apply_migration, deploy_app, deployment_status, delete_deployment, job_status]


def build():
    try:
        from mcp.server import MCPServer as Server            # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError as e:
            raise RuntimeError("the MCP server needs the `mcp` package: pip install 'switchyard[mcp]'") from e
    server = Server("switchyard", instructions=INSTRUCTIONS)
    for fn in TOOLS:
        server.tool()(fn)
    return server


def main() -> None:
    build().run("stdio")
