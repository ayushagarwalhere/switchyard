"""Web app: start runs, stream progress (SSE), browse results and patches."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .pipeline import RUNS_DIR, Run

WEB = Path(__file__).resolve().parent / "web"
DEMO_DIR = RUNS_DIR / "demo"
PUBLIC = os.environ.get("SWITCHYARD_PUBLIC") == "1"
RECORDED_URL = os.environ.get("SWITCHYARD_RECORDED_URL",
                              "https://raw.githubusercontent.com/ayushagarwalhere/switchyard/main/runs/demo/")


def fetch_recorded(dest: Path | None = None, base: str = RECORDED_URL, timeout: float = 15) -> list[str]:
    """An installed Switchyard has no recorded runs: download the published ones (read-only JSON) once."""
    import urllib.request
    dest = dest or DEMO_DIR
    if any(dest.glob("*/run.json")):
        return []
    got = []
    try:
        with urllib.request.urlopen(base + "featured.txt", timeout=timeout) as r:
            ids = [i for i in r.read().decode().split() if re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", i)]
        for rid in ids[:10]:
            with urllib.request.urlopen(f"{base}{rid}/run.json", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            (dest / rid).mkdir(parents=True, exist_ok=True)
            (dest / rid / "run.json").write_text(json.dumps(data), encoding="utf-8")
            got.append(rid)
        if got:
            (dest / "featured.txt").write_text("\n".join(got) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
    return got
RUN_CODE = os.environ.get("SWITCHYARD_RUN_CODE", "")
PUBLIC_BUDGET = float(os.environ.get("SWITCHYARD_RUN_BUDGET", "0.50"))

app = FastAPI(title="Switchyard")
LIVE: dict[str, Run] = {}
_lock = threading.Lock()


class RunRequest(BaseModel):
    source: str
    tests: int = 8
    target: float = 0.85
    budget: float = 1.0
    tavily: bool = True
    code: str = ""


def _load(run_id: str) -> dict | None:
    if run_id in LIVE:
        return LIVE[run_id].state
    if not re.fullmatch(r"[\w\-]+", run_id):
        return None
    for base in (DEMO_DIR, RUNS_DIR):
        p = base / run_id / "run.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


@app.get("/api/config")
def get_config():
    return {
        "live": bool(config.api_key()) and (not PUBLIC or bool(RUN_CODE)),
        "needs_code": PUBLIC and bool(RUN_CODE),
        "build": not PUBLIC, "apps_dir": None if PUBLIC else str(APPS_DIR),
        "tavily": bool(config.tavily_key()),
        "tracing": bool(config.langsmith_key()),
        "models": [{"key": m.key, "id": m.id, "label": m.label, "tier": m.tier, "price_in": m.price_in, "price_out": m.price_out}
                   for m in config.CANDIDATES],
        "roles": {"analyze": config.ROLE_ANALYZE, "testgen": config.ROLE_TESTGEN, "judge": config.ROLE_JUDGE, "repair": config.ROLE_REPAIR},
        "source_prices": config.SOURCE_PRICES,
    }


@app.get("/api/runs")
def list_runs():
    out = []
    seen = set()
    for base, featured in ((DEMO_DIR, True), (RUNS_DIR, False)):
        if not base.exists():
            continue
        for p in sorted(base.glob("*/run.json"), reverse=True):
            if p.parent.name in seen or p.parent.name == "demo":
                continue
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            seen.add(p.parent.name)
            out.append({"id": s["id"], "source": s["source"], "status": s["status"], "featured": featured,
                        "summary": s.get("summary"), "usage": s.get("usage"), "started": s.get("started"),
                        "sources": sum(len(x.get("sources") or []) for x in s.get("sites") or [])})
    # runs/demo/featured.txt names the run the home page, hero numbers and judge tour lead with
    try:
        lead = (DEMO_DIR / "featured.txt").read_text(encoding="utf-8").split()[0]
        out.sort(key=lambda r: r["id"] != lead)
    except (OSError, IndexError):
        pass
    for rid, run in LIVE.items():
        if rid not in seen:
            out.insert(0, {"id": rid, "source": run.source, "status": run.state["status"], "featured": False})
    if PUBLIC:
        out = [r for r in out if r["featured"] or r["id"] in LIVE]
    return out


@app.post("/api/runs")
def start_run(req: RunRequest):
    if not config.api_key():
        raise HTTPException(400, "Server has no NEBIUS_API_KEY; live runs are disabled")
    budget = req.budget
    if PUBLIC:
        if not RUN_CODE or req.code != RUN_CODE:
            raise HTTPException(403, "Live runs need an access code on the public demo. Replays are open to everyone.")
        if not re.fullmatch(r"https://github\.com/[\w.\-]+/[\w.\-]+(\.git)?(/tree/[\w.\-]+(/[\w.\-/]*)?)?/?", req.source.strip()):
            raise HTTPException(400, "Public demo accepts public GitHub URLs only")
        budget = min(budget, PUBLIC_BUDGET)
    with _lock:
        if sum(1 for r in LIVE.values() if r.state["status"] == "running") >= 2:
            raise HTTPException(429, "Two runs are already in progress; try again in a minute")
        run = Run(req.source, tests=max(3, min(req.tests, 12)), target=max(0.5, min(req.target, 1.0)),
                  budget=max(0.05, min(budget, 5.0)), use_tavily=req.tavily)
        LIVE[run.id] = run
    threading.Thread(target=run.run, daemon=True).start()
    return {"id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    s = _load(run_id)
    if s is None:
        raise HTTPException(404, "run not found")
    return s


@app.get("/api/runs/{run_id}/patch")
def get_patch(run_id: str):
    s = _load(run_id)
    if s is None or not s.get("patch"):
        raise HTTPException(404, "no patch for this run")
    return PlainTextResponse(s["patch"]["diff"], media_type="text/x-diff",
                             headers={"Content-Disposition": f'attachment; filename="switchyard-{run_id}.patch"'})


@app.get("/api/runs/{run_id}/pr")
def get_pr(run_id: str):
    """The pull-request description Switchyard wrote for this migration (Markdown)."""
    s = _load(run_id)
    if s is None or s.get("status") != "done":
        raise HTTPException(404, "no report for this run")
    from .report import pr_body
    return PlainTextResponse(s.get("pr_body") or pr_body(s), media_type="text/markdown")


class AuditRequest(BaseModel):
    source: str


GITHUB_RE = re.compile(r"^(https://github\.com/[\w.\-]+/[\w.\-]+?)(?:\.git)?(?:/tree/([\w.\-]+)(?:/([\w.\-/]*))?)?/?$")
_audit_slots = threading.BoundedSemaphore(2)
_audit_cache: dict[str, tuple[float, dict]] = {}


def _audit_clone(url: str, branch: str | None, dest: Path) -> None:
    """Shallow, sparse clone of only the source files: no history, no big assets, nothing executed."""
    import subprocess
    base = ["git", "-c", "core.longpaths=true"]
    subprocess.run(base + ["clone", "--depth", "1", "--filter=blob:none", "--sparse", "--quiet"]
                   + (["--branch", branch] if branch else []) + [url, str(dest)],
                   check=True, capture_output=True, text=True, timeout=60)
    patterns = ["*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs", "*.cjs", "!node_modules/", "!*.min.js"]
    subprocess.run(base + ["-C", str(dest), "sparse-checkout", "set", "--no-cone", *patterns],
                   check=True, capture_output=True, text=True, timeout=90)


@app.post("/api/audit")
def audit_repo(req: AuditRequest):
    """Free static audit of a public GitHub repo (or a local folder when not public). No key needed."""
    import shutil
    import tempfile
    import time as _time

    from . import audit, scanner
    src = req.source.strip()
    m = GITHUB_RE.match(src)
    if not m and (PUBLIC or not Path(src).is_dir()):
        raise HTTPException(400, "Paste a public GitHub URL, e.g. https://github.com/owner/repo")
    hit = _audit_cache.get(src)
    if hit and _time.time() - hit[0] < 600:
        return hit[1]
    if not _audit_slots.acquire(timeout=30):
        raise HTTPException(429, "Two audits are already running; try again in a moment")
    tmp = Path(tempfile.mkdtemp(prefix="switchyard-audit-"))
    try:
        if m:
            try:
                _audit_clone(m.group(1), m.group(2), tmp / "repo")
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"couldn't clone that repository ({type(e).__name__})")
            root = (tmp / "repo" / (m.group(3) or "")).resolve()
            if not root.is_dir() or not root.is_relative_to((tmp / "repo").resolve()):
                raise HTTPException(400, "that folder isn't in the repository")
        else:
            root = Path(src).resolve()
        res = scanner.scan_repo(root)
        findings = audit.static(root, res)
        out = {"source": src, "files": res["files"], "lines": res["loc"],
               "calls": [{"file": c["file"], "line": c["line"], "function": c["function"], "sdk": c["sdk"],
                          "model": c["model"] or c["model_expr"], "skip": c.get("skip")} for c in res["calls"]],
               "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in audit.SEVERITY_ORDER},
               "findings": findings[:200]}
        _audit_cache[src] = (_time.time(), out)
        return out
    finally:
        _audit_slots.release()
        shutil.rmtree(tmp, ignore_errors=True)


# -- build & ship (local only: these write files and deploy from this machine) -------------------------

APPS_DIR = Path(os.environ.get("SWITCHYARD_APPS", Path.home() / "switchyard-apps"))


class BuildRequest(BaseModel):
    idea: str
    budget: float = 0.6


class PathRequest(BaseModel):
    path: str
    change: str | None = None
    private: bool = False
    agreed: bool = False


def _local_only():
    if PUBLIC:
        raise HTTPException(403, "Building and deploying run on your own machine: pip install switchyard, then switchyard serve")


def _app_path(p: str) -> Path:
    root = Path(p).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(400, f"{root} is not a folder")
    return root


@app.post("/api/build")
def build_app(req: BuildRequest):
    _local_only()
    if not config.api_key():
        raise HTTPException(400, "No Token Factory key: run `switchyard setup` first")
    if len(req.idea.strip()) < 8:
        raise HTTPException(400, "Describe the app in a sentence")
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    from . import mcp_server
    return mcp_server.create_app(req.idea, str(APPS_DIR), max_spend_usd=max(0.05, min(req.budget, 2.0)))


@app.post("/api/change")
def change_app(req: PathRequest):
    _local_only()
    if not (req.change or "").strip():
        raise HTTPException(400, "Say what to change")
    from . import mcp_server
    return mcp_server.change_app(str(_app_path(req.path)), req.change)


@app.post("/api/secure")
def secure_app(req: PathRequest):
    _local_only()
    from . import secure
    res = secure.run(_app_path(req.path))
    return {"blocking": len(res["blocking"]), "findings": res["findings"][:100], "notes": res["notes"]}


@app.post("/api/deploy")
def deploy_app(req: PathRequest):
    _local_only()
    from . import mcp_server
    return mcp_server.deploy_app(str(_app_path(req.path)), req.agreed, private=req.private)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    from . import mcp_server
    res = mcp_server.job_status(job_id)
    if res.get("error") and not res.get("status"):
        raise HTTPException(404, res["error"])
    return res


@app.get("/api/deployments")
def deployments():
    if PUBLIC:
        return []
    from . import deploy
    return [{"name": n, "url": a.get("url"), "path": a.get("path"), "healthy": a.get("healthy"), "created": a.get("created")}
            for n, a in deploy.load_state().get("apps", {}).items()]


@app.delete("/api/deployments/{name}")
def delete_deployment(name: str):
    _local_only()
    from . import deploy
    try:
        return {"deleted": deploy.delete(name)}
    except deploy.DeployError as e:
        raise HTTPException(400, str(e))


# -- sandbox: run a built app in a locked-down local container and preview it in the page ----------------

@app.post("/api/sandbox")
def sandbox_start(req: PathRequest):
    _local_only()
    root = _app_path(req.path)
    from . import mcp_server
    from .deploy import Deployer
    return mcp_server._job("sandbox", lambda emit: Deployer(root, f"sandbox-{root.name}", local=True, emit=emit,
                                                             wait=120).run())


@app.get("/api/sandbox/files")
def sandbox_files(path: str):
    _local_only()
    root = _app_path(path)
    from .secure import iter_files
    return [{"path": f.relative_to(root).as_posix(), "bytes": f.stat().st_size} for f in iter_files(root)
            if not f.name.lower().startswith(".env") or f.name.lower().endswith(".example")][:300]


@app.get("/api/sandbox/file")
def sandbox_file(path: str, file: str):
    _local_only()
    root = _app_path(path)
    target = (root / file).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "no such file in the app")
    if target.name.lower().startswith(".env") and not target.name.lower().endswith(".example"):
        raise HTTPException(403, "secrets stay out of the browser")
    if target.stat().st_size > 400_000:
        raise HTTPException(413, "file too large to preview")
    return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))


@app.delete("/api/sandbox")
def sandbox_stop(path: str):
    _local_only()
    root = _app_path(path)
    from . import deploy
    try:
        return {"stopped": deploy.delete(deploy._slug(f"sandbox-{root.name}"))}
    except deploy.DeployError as e:
        raise HTTPException(400, str(e))


@app.get("/api/health")
def health():
    return {"ok": True, "live": bool(config.api_key()), "running": sum(1 for r in LIVE.values() if r.state["status"] == "running")}


@app.get("/api/runs/{run_id}/events")
async def stream(run_id: str):
    if _load(run_id) is None:
        raise HTTPException(404, "run not found")

    async def gen():
        i = 0
        while True:
            s = _load(run_id)
            events = s["events"] if s else []
            while i < len(events):
                yield f"data: {json.dumps(events[i], default=str)}\n\n"
                i += 1
            if not s or s["status"] != "running":
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")
