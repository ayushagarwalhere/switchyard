"""`switchyard deploy`: ship an app to Nebius AI Cloud as a Serverless endpoint.

    secure check -> Dockerfile (written if missing) -> docker build (linux/amd64)
    -> Nebius Container Registry -> `nebius ai endpoint create` -> live URL + health check

The `nebius` CLI runs on Linux and macOS; on Windows Switchyard calls it inside WSL. Endpoints bill until
they are deleted: `switchyard deploy delete` removes one.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config

STATE = config.HOME / "deployments.json"
DEFAULT_PLATFORM = os.environ.get("SWITCHYARD_NEBIUS_PLATFORM", "cpu-d3")
# a CPU platform (without --platform Nebius picks a GPU one); 2 vCPU / 8 GB is plenty for a web app
DEFAULT_PRESET = os.environ.get("SWITCHYARD_NEBIUS_PRESET", "2vcpu-8gb")


class DeployError(RuntimeError):
    pass


# -- running things -----------------------------------------------------------------------------------

class Shell:
    """Runs `nebius` natively, or through WSL on Windows; runs docker natively. Swappable in tests."""

    def __init__(self):
        self._nebius: list[str] | None = None

    def nebius_prefix(self) -> list[str]:
        if self._nebius is None:
            if shutil.which("nebius"):
                self._nebius = ["nebius"]
            elif os.name == "nt" and shutil.which("wsl"):
                r = subprocess.run(["wsl", "-e", "bash", "-lc", "command -v nebius || ls ~/.nebius/bin/nebius"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    raise DeployError("The nebius CLI isn't installed in WSL. In Ubuntu run:\n"
                                      "  curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash\n"
                                      "  exec -l $SHELL && nebius profile create")
                self._nebius = ["wsl", "-e", "bash", "-lc"]
            else:
                raise DeployError("The nebius CLI isn't installed: curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash")
        return self._nebius

    def nebius(self, *args: str, input: str | None = None, check: bool = True) -> str:
        pre = self.nebius_prefix()
        # in WSL the installer's PATH entry may only be in .bashrc, so add it explicitly
        cmd = (pre + ['export PATH="$HOME/.nebius/bin:$PATH"; ' + shlex.join(["nebius", *args])]
               if pre[0] == "wsl" else pre + list(args))
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", input=input)
        if check and r.returncode != 0:
            raise DeployError(f"nebius {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-800:]}")
        return r.stdout

    def run(self, cmd: list[str], input: str | None = None, cwd: str | None = None, stream: bool = False,
            env: dict | None = None, check: bool = True) -> str:
        if not shutil.which(cmd[0]):
            raise DeployError(f"{cmd[0]} isn't installed" + (" (Docker Desktop: docker.com)" if cmd[0] == "docker" else ""))
        r = subprocess.run(cmd, capture_output=not stream, text=True, encoding="utf-8", errors="replace", input=input, cwd=cwd,
                           env={**os.environ, **env} if env else None)
        if not check:
            return (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            raise DeployError(f"{' '.join(cmd[:3])} failed: {((r.stderr or '') + (r.stdout or '')).strip()[-800:]}")
        return r.stdout or ""


def _json(text: str) -> dict:
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return {}


# -- the app ------------------------------------------------------------------------------------------

def detect(root: Path) -> dict:
    """What kind of app this is, and the port it should listen on inside the container."""
    req = "\n".join(p.read_text(encoding="utf-8", errors="replace").lower() for p in root.glob("requirements*.txt"))
    if (root / "Dockerfile").exists():
        m = re.search(r"^\s*EXPOSE\s+(\d+)", (root / "Dockerfile").read_text(encoding="utf-8", errors="replace"), re.M)
        return {"kind": "dockerfile", "port": int(m.group(1)) if m else 8080}
    if "fastapi" in req or "uvicorn" in req:
        mod = "app.main:app" if (root / "app" / "main.py").exists() else "main:app"
        return {"kind": "fastapi", "port": 8080, "module": mod}
    if "flask" in req:
        return {"kind": "flask", "port": 8080, "module": "app:app"}
    pkg = root / "package.json"
    if pkg.exists():
        data = _json(pkg.read_text(encoding="utf-8"))
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        return {"kind": "next" if "next" in deps else "node", "port": 3000}
    if (root / "index.html").exists():
        return {"kind": "static", "port": 8080}
    raise DeployError("Can't tell how to run this app (no Dockerfile, requirements.txt, package.json or index.html)")


def dockerfile(info: dict) -> str:
    k = info["kind"]
    if k in ("fastapi", "flask"):
        run = (f"uvicorn {info['module']} --host 0.0.0.0 --port ${{PORT:-8080}}" if k == "fastapi"
               else f"gunicorn -b 0.0.0.0:${{PORT:-8080}} {info['module']}")
        extra = "" if k == "fastapi" else " gunicorn==23.0.0"
        return ("FROM python:3.12-slim\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080\nWORKDIR /app\n"
                f"COPY requirements*.txt ./\nRUN pip install --no-cache-dir -r requirements.txt{extra}\nCOPY . .\n"
                f"RUN useradd --create-home appuser\nUSER appuser\nEXPOSE 8080\nCMD [\"sh\", \"-c\", \"{run}\"]\n")
    if k == "next":
        return ("FROM node:20-slim AS build\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nRUN npm run build\n"
                "FROM node:20-slim\nWORKDIR /app\nENV NODE_ENV=production PORT=3000\nCOPY --from=build /app ./\n"
                "USER node\nEXPOSE 3000\nCMD [\"npm\", \"start\"]\n")
    if k == "node":
        return ("FROM node:20-slim\nWORKDIR /app\nENV NODE_ENV=production PORT=3000\nCOPY package*.json ./\n"
                "RUN npm ci --omit=dev\nCOPY . .\nUSER node\nEXPOSE 3000\nCMD [\"npm\", \"start\"]\n")
    return ("FROM nginxinc/nginx-unprivileged:1.27-alpine\nCOPY . /usr/share/nginx/html\n"
            "RUN sed -i 's/listen       8080;/listen 8080;/' /etc/nginx/conf.d/default.conf\nEXPOSE 8080\n")


# Names copied from the app's .env / .env.local into the container (anything else there stays local).
# SWITCHYARD_* are Switchyard's own public-demo switches: deploying Switchyard itself must keep public mode,
# the run code, the per-visitor budget and the code-execution switch, or it would start wide open on your key.
APP_ENV = re.compile(r"LLM_MODEL_[A-Z0-9_]+|TOKEN_FACTORY_BASE_URL|SWITCHYARD_PUBLIC|SWITCHYARD_RUN_CODE"
                     r"|SWITCHYARD_RUN_BUDGET|SWITCHYARD_EXEC")


def app_env(root: Path) -> dict[str, str]:
    """Env vars the deployed app gets: the Token Factory key (for its AI features), LLM_MODEL_* overrides,
    Switchyard's public-demo settings and a Tavily key, when the app's .env / .env.local set them."""
    env = {}
    key = config.api_key()
    if key:
        env["NEBIUS_API_KEY"] = key
    for f in (root / ".env", root / ".env.local"):          # .env.local wins
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)", line)
                if not m:
                    continue
                name, value = m.group(1), m.group(2).strip("'\"")
                if name in config.TAVILY_NAMES:
                    env["TAVILY_API_KEY"] = value               # the name the app reads first
                elif APP_ENV.fullmatch(name):
                    env[name] = value
    return env


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")[:40] or "app"


# -- state --------------------------------------------------------------------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"registry": None, "apps": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


# -- deploy -------------------------------------------------------------------------------------------

class Deployer:
    def __init__(self, path: str | Path = ".", name: str | None = None, platform: str = DEFAULT_PLATFORM,
                 preset: str = DEFAULT_PRESET, private: bool = False, force: bool = False, emit=print,
                 shell: Shell | None = None, wait: float = 600, http=None, local: bool = False):
        self.root = Path(path).resolve()
        self.name = _slug(name or self.root.name)
        self.platform, self.preset, self.private, self.force = platform, preset, private, force
        self.emit, self.sh, self.wait = emit, shell or Shell(), wait
        self.http = http or _http_get
        self.local = local

    def registry(self, st: dict) -> dict:
        if st.get("registry"):
            return st["registry"]
        self.emit("Creating a Nebius Container Registry for your apps (once)…")
        data = _json(self.sh.nebius("registry", "create", "--name", "switchyard-apps", "--format", "json"))
        rid = (data.get("metadata") or {}).get("id") or data.get("id") or (data.get("resource_id"))
        if not rid:
            found = _json(self.sh.nebius("registry", "list", "--format", "json"))
            items = found.get("items") or []
            rid = next(((i.get("metadata") or {}).get("id") for i in items
                        if (i.get("metadata") or {}).get("name") == "switchyard-apps"), None)
        if not rid:
            raise DeployError("Couldn't create or find the container registry")
        region = (os.environ.get("NEBIUS_REGION") or ((data.get("status") or {}).get("region"))
                  or _region_from_profile(self.sh) or "eu-north1")
        reg = {"id": rid, "region": region, "host": f"cr.{region}.nebius.cloud", "path": rid.removeprefix("registry-")}
        st["registry"] = reg
        save_state(st)
        return reg

    def run(self) -> dict:
        from . import secure
        self.emit(f"Checking {self.root.name} before it ships…")
        sec = secure.run(self.root)
        if sec["blocking"] and not self.force:
            lines = "\n".join(f"  {f['severity']} {f['rule']} {f['file']}:{f['line']} {f['title']}: {f['detail']}"
                              for f in sec["blocking"][:8])
            raise DeployError(f"{len(sec['blocking'])} blocking security finding(s); fix them or pass --force:\n{lines}")
        self.emit(f"  {len(sec['findings'])} finding(s), none blocking")
        info = detect(self.root)
        wrote_dockerfile = False
        if info["kind"] != "dockerfile":
            (self.root / "Dockerfile").write_text(dockerfile(info), encoding="utf-8")
            wrote_dockerfile = True
            self.emit(f"  wrote a Dockerfile for this {info['kind']} app")
        st = load_state()
        if self.local:
            return self.run_local(st, info, len(sec["findings"]), wrote_dockerfile)
        reg = self.registry(st)
        tag = time.strftime("%Y%m%d-%H%M%S")
        image = f"{reg['host']}/{reg['path']}/{self.name}:{tag}"
        self.emit(f"Building {image} (linux/amd64)…")
        self.sh.run(["docker", "build", "--platform", "linux/amd64", "-t", image, "."], cwd=str(self.root))
        self.emit("Pushing to Nebius Container Registry…")
        token = self.sh.nebius("iam", "get-access-token").strip()
        self.sh.run(["docker", "login", reg["host"], "--username", "iam", "--password-stdin"], input=token)
        self.sh.run(["docker", "push", image])
        old = st["apps"].get(self.name)
        ep_name = f"{self.name}-{tag}"
        args = ["ai", "endpoint", "create", "--name", ep_name, "--image", image, "--platform", self.platform,
                "--preset", self.preset, "--container-port", str(info["port"]), "--format", "json"]
        auth_token = None
        if self.private:
            auth_token = os.urandom(24).hex()
            args += ["--auth", "token", "--token", auth_token]
        else:
            args += ["--auth", "none"]
        env = app_env(self.root)
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        subnet = _default_subnet(self.sh)
        if subnet:
            args += ["--subnet-id", subnet]
        self.emit(f"Creating the Serverless endpoint {ep_name} ({self.platform}, {self.preset})…")
        created = _json(self.sh.nebius(*args))
        ep_id = (created.get("metadata") or {}).get("id") or created.get("resource_id") or _endpoint_id(self.sh, ep_name)
        url = self.await_url(ep_id)
        healthy = self.health(url, auth_token)
        rec = {"endpoint_id": ep_id, "endpoint": ep_name, "image": image, "url": url, "private": self.private,
               "token": auth_token, "path": str(self.root), "created": time.time(), "healthy": healthy,
               "env": sorted(env), "container_port": info["port"], "platform": self.platform, "preset": self.preset,
               "history": ((old or {}).get("history") or [])[-9:] + [image]}
        st["apps"][self.name] = rec
        save_state(st)
        if old and old.get("endpoint_id") and healthy:
            self.emit(f"Deleting the previous endpoint {old['endpoint']} (it bills until deleted)…")
            self.sh.nebius("ai", "endpoint", "delete", old["endpoint_id"], check=False)
        return {**rec, "name": self.name, "wrote_dockerfile": wrote_dockerfile, "secure": len(sec["findings"]),
                "token": None}

    def run_local(self, st: dict, info: dict, n_findings: int, wrote_dockerfile: bool) -> dict:
        """The same image, env and health check, as a container in local Docker (free; no Nebius calls)."""
        import socket
        tag = time.strftime("%Y%m%d-%H%M%S")
        image = f"switchyard-local/{self.name}:{tag}"
        container = f"switchyard-{self.name}"
        self.emit(f"Building {image} (linux/amd64)…")
        self.sh.run(["docker", "build", "--platform", "linux/amd64", "-t", image, "."], cwd=str(self.root))
        self.sh.run(["docker", "rm", "-f", container], check=False)
        old = st["apps"].get(self.name) or {}
        port = old.get("host_port")
        if not port:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        env = app_env(self.root)
        cmd = ["docker", "run", "-d", "--name", container, "-p", f"127.0.0.1:{port}:{info['port']}"]
        for k in env:
            cmd += ["-e", k]                     # values come from the environment, never the command line
        self.emit(f"Starting it locally on port {port}…")
        self.sh.run(cmd + [image], env=env)
        url = f"http://127.0.0.1:{port}"
        healthy = self.health(url, None)
        rec = {"local": True, "container": container, "image": image, "url": url, "path": str(self.root),
               "created": time.time(), "healthy": healthy, "env": sorted(env), "host_port": port,
               "container_port": info["port"], "history": (old.get("history") or [])[-9:] + [image]}
        st["apps"][self.name] = rec
        save_state(st)
        return {**rec, "name": self.name, "endpoint": container, "wrote_dockerfile": wrote_dockerfile,
                "secure": n_findings, "token": None}

    def await_url(self, ep_id: str) -> str:
        self.emit("Waiting for the endpoint to start…")
        deadline = time.time() + self.wait
        while True:
            data = _json(self.sh.nebius("ai", "endpoint", "get", ep_id, "--format", "json"))
            status = data.get("status") or {}
            urls = [u for u in status.get("public_endpoints") or [] if str(u).startswith("http")]
            state = str(status.get("state") or "").upper()
            if urls and state in ("", "RUNNING", "READY", "ACTIVE"):
                return next((u for u in urls if u.startswith("https://")), urls[0])
            if state in ("ERROR", "FAILED"):
                raise DeployError(f"endpoint failed to start: {status}")
            if time.time() > deadline:
                raise DeployError(f"endpoint not ready after {self.wait:.0f}s (state {state or 'unknown'}); "
                                  f"check `switchyard deploy status {self.name}`")
            time.sleep(10)

    def health(self, url: str, token: str | None) -> bool:
        for path in ("/api/health", "/"):
            for _ in range(12):
                code = self.http(url.rstrip("/") + path, token)
                if code and code < 500:
                    return code < 400
                time.sleep(5)
        return False


def _http_get(url: str, token: str | None) -> int | None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 - not up yet
        return None


def _region_from_profile(sh: Shell) -> str | None:
    try:
        out = sh.nebius("config", "get", "region", check=False).strip()
    except DeployError:
        return None
    return out if re.fullmatch(r"[a-z]+-[a-z]+\d", out) else None


def _default_subnet(sh: Shell) -> str | None:
    try:
        data = _json(sh.nebius("vpc", "subnet", "list", "--format", "json", check=False))
    except DeployError:
        return None
    items = data.get("items") or []
    return (items[0].get("metadata") or {}).get("id") if items else None


def _endpoint_id(sh: Shell, name: str) -> str:
    data = _json(sh.nebius("ai", "endpoint", "get-by-name", "--name", name, "--format", "json"))
    rid = (data.get("metadata") or {}).get("id")
    if not rid:
        raise DeployError(f"endpoint {name} was not created")
    return rid


# -- manage -------------------------------------------------------------------------------------------

def _pick(name: str | None) -> tuple[str, dict]:
    apps = load_state()["apps"]
    if not apps:
        raise DeployError("Nothing deployed yet: run `switchyard deploy` in your app's folder")
    if name is None:
        here = str(Path.cwd().resolve())
        name = next((n for n, a in apps.items() if a.get("path") == here), None) or (next(iter(apps)) if len(apps) == 1 else None)
    if name not in apps:
        raise DeployError(f"Which app? Deployed: {', '.join(apps)}")
    return name, apps[name]


def status(name: str | None = None, shell: Shell | None = None) -> dict:
    name, rec = _pick(name)
    if rec.get("local"):
        out = (shell or Shell()).run(["docker", "inspect", "-f", "{{.State.Status}}", rec["container"]], check=False).strip()
        return {"name": name, "url": rec.get("url"), "state": out or "gone", "image": rec.get("image"), "endpoint": rec["container"]}
    data = _json((shell or Shell()).nebius("ai", "endpoint", "get", rec["endpoint_id"], "--format", "json", check=False))
    st = (data.get("status") or {})
    return {"name": name, "url": rec.get("url"), "state": st.get("state") or ("gone" if not data else "unknown"),
            "image": rec.get("image"), "endpoint": rec.get("endpoint")}


def logs(name: str | None = None, shell: Shell | None = None) -> str:
    name, rec = _pick(name)
    if rec.get("local"):
        return (shell or Shell()).run(["docker", "logs", "--tail", "200", rec["container"]], check=False)
    return (shell or Shell()).nebius("ai", "endpoint", "logs", rec["endpoint_id"], check=False)


def delete(name: str | None = None, shell: Shell | None = None) -> str:
    name, rec = _pick(name)
    if rec.get("local"):
        (shell or Shell()).run(["docker", "rm", "-f", rec["container"]], check=False)
    else:
        (shell or Shell()).nebius("ai", "endpoint", "delete", rec["endpoint_id"])
    st = load_state()
    st["apps"].pop(name, None)
    save_state(st)
    return name


# -- operations (used by `switchyard oncall` / WispOps as its restart, rollback and release commands) --------

def release(name: str | None = None) -> str:
    """The running release: the image tag."""
    _, rec = _pick(name)
    return rec["image"].rsplit(":", 1)[-1]


def restart(name: str | None = None, shell: Shell | None = None) -> str:
    name, rec = _pick(name)
    sh = shell or Shell()
    if rec.get("local"):
        sh.run(["docker", "restart", rec["container"]])
    else:
        sh.nebius("ai", "endpoint", "stop", rec["endpoint_id"], check=False)
        sh.nebius("ai", "endpoint", "start", rec["endpoint_id"])
    return name


def rollback(name: str | None, to: str, shell: Shell | None = None, emit=print) -> dict:
    """Run an earlier image of this app again (`to` is a tag from its history, or the full image)."""
    name, rec = _pick(name)
    sh = shell or Shell()
    image = next((i for i in reversed(rec.get("history") or []) if i == to or i.rsplit(":", 1)[-1] == to), None)
    if not image:
        known = ", ".join(i.rsplit(":", 1)[-1] for i in rec.get("history") or [])
        raise DeployError(f"{to} isn't a release of {name}; known: {known}")
    st = load_state()
    env = app_env(Path(rec["path"])) if rec.get("path") else {}
    if rec.get("local"):
        sh.run(["docker", "rm", "-f", rec["container"]], check=False)
        cmd = ["docker", "run", "-d", "--name", rec["container"], "-p",
               f"127.0.0.1:{rec['host_port']}:{rec['container_port']}"]
        for k in env:
            cmd += ["-e", k]
        sh.run(cmd + [image], env=env)
        st["apps"][name] = {**rec, "image": image, "created": time.time()}
    else:
        tag = time.strftime("%Y%m%d-%H%M%S")
        ep_name = f"{name}-{tag}"
        args = ["ai", "endpoint", "create", "--name", ep_name, "--image", image, "--platform", rec["platform"],
                "--preset", rec["preset"], "--container-port", str(rec["container_port"]), "--format", "json",
                "--auth", "token" if rec.get("private") else "none"]
        if rec.get("private") and rec.get("token"):
            args += ["--token", rec["token"]]
        for k, v in env.items():
            args += ["--env", f"{k}={v}"]
        subnet = _default_subnet(sh)
        if subnet:
            args += ["--subnet-id", subnet]
        emit(f"Creating {ep_name} from {image}")
        created = _json(sh.nebius(*args))
        ep_id = (created.get("metadata") or {}).get("id") or _endpoint_id(sh, ep_name)
        url = Deployer(rec["path"], name, shell=sh, emit=emit).await_url(ep_id)
        sh.nebius("ai", "endpoint", "delete", rec["endpoint_id"], check=False)
        st["apps"][name] = {**rec, "image": image, "endpoint_id": ep_id, "endpoint": ep_name, "url": url,
                            "created": time.time()}
    save_state(st)
    return {"name": name, "image": image}
