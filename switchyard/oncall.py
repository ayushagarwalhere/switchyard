"""`switchyard oncall`: keep deployed apps running, with WispOps (vendored in switchyard/oncall_agent).

Every app you shipped with `switchyard deploy` becomes a WispOps service:

    health    the app's URL (/api/health for apps made by `switchyard new`)
    restart   switchyard deploy restart <app>           (Nebius endpoint stop/start, or docker restart)
    rollback  switchyard deploy rollback <app> {to}     (run an earlier image again)
    release   switchyard deploy release <app>           (the running image tag: WispOps sees every deploy)
    logs      switchyard deploy logs <app>

When an app breaks, WispOps finds the change that caused it (a new release), proposes one fix, applies it only
when you approve, proves the app recovered (healthy checks in a row) and writes the postmortem.
Approving by voice with Wispr Flow is optional: typed approvals are on unless you pass `--voice`.
`switchyard deploy logs <app> --explain` adds Nemotron's reading of the logs.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

AGENT = Path(__file__).resolve().parent / "oncall_agent"
HOME = config.HOME / "oncall"
CONFIG = HOME / "wisprops.config.json"


PKG_PARENT = str(Path(__file__).resolve().parent.parent)


def _cmd(*args: str) -> list[str]:
    """`switchyard deploy ...` that works from any folder, installed or from a source checkout."""
    boot = f"import sys; sys.path.insert(0, {PKG_PARENT!r}); from switchyard.cli import main; sys.exit(main())"
    return [sys.executable, "-c", boot, "deploy", *args]


def service_for(name: str, rec: dict) -> dict:
    path = Path(rec.get("path") or ".")
    made_by_switchyard = (path / "switchyard.json").exists() or (path / "app" / "llm.py").exists()
    url = (rec.get("url") or "").rstrip("/")
    svc = {
        "id": name, "name": name,
        "health": {"url": url + ("/api/health" if made_by_switchyard else "/"), "timeoutMs": 5000, "expect": 200},
        "restart": _cmd("restart", name),
        "rollback": _cmd("rollback", name, "{to}"),
        "release": {"command": _cmd("release", name)},
        "logs": {"command": _cmd("logs", name)},
    }
    if rec.get("private") and rec.get("token"):
        svc["health"]["headers"] = {"Authorization": f"Bearer {rec['token']}"}
    if (path / ".git").exists():
        svc["repo"] = str(path)
    return svc


def write_config(voice: bool = False, port: int | None = None) -> dict:
    """Merge every deployed app into the WispOps config, keeping the user's own settings and services."""
    from . import deploy
    apps = deploy.load_state().get("apps", {})
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {"port": 7300, "settings": {"intervalMs": 5000, "failAfter": 2, "goldenAfter": 2, "verifyOk": 3,
                                          "verifyWindowMs": 60000, "handoffHour": 7},
               "notify": {"ntfy": "", "webhook": ""}, "pr": {"push": False}, "services": []}
    if port:
        cfg["port"] = port
    cfg.setdefault("settings", {})["allowTyped"] = not voice
    ours = {name: service_for(name, rec) for name, rec in apps.items()}
    cfg["services"] = [s for s in cfg.get("services", []) if s.get("id") not in ours
                       and not s.get("_switchyard")] + [{**s, "_switchyard": True} for s in ours.values()]
    CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def node() -> str:
    exe = shutil.which("node")
    if not exe:
        raise RuntimeError("On-call needs Node.js 18.17+ (nodejs.org)")
    v = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(x) for x in (v.split(".") + ["0", "0"])[:2])
    if (major, minor) < (18, 17):
        raise RuntimeError(f"On-call needs Node.js 18.17+, found {v}")
    return exe


def start(voice: bool = False, port: int | None = None) -> int:
    cfg = write_config(voice, port)
    names = [s["id"] for s in cfg["services"] if s.get("_switchyard")]
    if not names:
        print("Nothing deployed yet: `switchyard deploy` (or `switchyard deploy --local`) an app first.")
        return 2
    print(f"On call for {len(names)} app(s): {', '.join(names)}")
    print(f"Config: {CONFIG}")
    print("Approvals: " + ("say the fix back with Wispr Flow" if voice else "typed (pass --voice to require Wispr Flow)"))
    return subprocess.call([node(), str(AGENT / "bin" / "wisprops.js"), "start", "--config", str(CONFIG)], cwd=str(HOME))


def agent_cli(args: list[str]) -> int:
    """Any other WispOps command against the Switchyard config, e.g. `switchyard oncall -- status`."""
    return subprocess.call([node(), str(AGENT / "bin" / "wisprops.js"), *args, "--config", str(CONFIG)], cwd=str(HOME))
