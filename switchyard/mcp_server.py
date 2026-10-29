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
    "Switchyard moves a Python app's LLM calls (OpenAI, Anthropic, LiteLLM, LangChain) to NVIDIA Nemotron "
    "on Nebius Token Factory, keeping only switches that pass generated tests, and audits LLM calls for "
    "security problems. Typical flow: scan_llm_calls (free) -> show the user the calls and estimated cost -> "
    "start_migration only after the user agrees to a spend limit -> migration_status until done -> "
    "migration_results -> apply_migration if the user wants the patch. audit_llm_calls is free unless "
    "probes=true. Never ask the user to paste an API key into the chat; if a key is missing, ask them to "
    "run `switchyard setup` in a terminal.")

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
    """Find every LLM call in a Python project (static analysis, free, no key). Returns each call's file,
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


TOOLS = [scan_llm_calls, audit_llm_calls, start_migration, migration_status, migration_results, apply_migration]


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
