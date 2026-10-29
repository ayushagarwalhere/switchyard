"""`switchyard secure`: everything to check before an app ships, in one report.

- every LLM call (audit.static: leaked keys, model output reaching eval/shell/SQL, prompt hygiene)
- secrets anywhere in the repo, and .env files tracked by git
- dependencies with known vulnerabilities (OSV database, free, no key)
- risky web settings: debug mode on, CORS open to every site with credentials, containers running as root

`switchyard deploy` runs this first and stops on CRITICAL/HIGH findings.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

from . import audit

RULES = {
    "SE1": ("Secret in a repo file", "A key or private key is committed. Anyone with the repo can use it."),
    "SE2": (".env tracked by git", "The .env file is committed, so every secret in it is public to anyone with the repo."),
    "SE3": ("Debug mode in production", "Debug mode shows stack traces and (Flask) an interactive console to anyone."),
    "SE4": ("CORS open to every site", "Any website can call this API from a visitor's browser."),
    "DP1": ("Vulnerable dependency", "A pinned dependency has a published vulnerability."),
    "DK1": ("Container runs as root", "If the app is compromised, the attacker is root inside the container."),
    "DK2": ("Unpinned base image", "`latest` changes under you: builds stop being reproducible."),
}
ALL_RULES = {**audit.RULES, **RULES}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".pytest_cache", "site-packages"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".json", ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg",
            ".env", ".txt", ".md", ".sh", ".html", ".tf", ".properties", ".conf"}
SECRET_RES = [
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:proj-|ant-(?:api\d\d-)?)?[A-Za-z0-9_\-]{24,}")),
    ("NVIDIA key", re.compile(r"\bnvapi-[A-Za-z0-9_\-]{24,}")),
    ("Tavily key", re.compile(r"\btvly-[A-Za-z0-9\-]{20,}")),
    ("LangSmith key", re.compile(r"\blsv2_[A-Za-z0-9_]{24,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("Token Factory key", re.compile(r"""(?:NEBIUS|TOKEN_FACTORY)[A-Z_]*KEY\s*[=:]\s*["']?(v1\.[A-Za-z0-9_\-.]{30,}|eyJ[A-Za-z0-9_\-.]{40,})""")),
]
PLACEHOLDER = re.compile(r"x{6,}|your[_-]|example|placeholder|dummy|\.\.\.|<[^>]+>", re.I)


def _f(rule: str, severity: str, file: str, line: int, detail: str, fix: str) -> dict:
    return {"rule": rule, "title": ALL_RULES[rule][0], "severity": severity, "file": file, "line": line,
            "function": None, "detail": detail, "fix": fix}


def iter_files(root: Path, max_bytes: int = 400_000):
    for p in sorted(root.rglob("*")):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        name = p.name.lower()
        if (p.suffix.lower() in TEXT_EXT or name.startswith(".env") or name in ("dockerfile", "procfile")) \
                and p.stat().st_size <= max_bytes:
            yield p


def _tracked(root: Path) -> set[str] | None:
    """Files git tracks under root (None outside a git repo)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--full-name", "."], capture_output=True, text=True, timeout=20)
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode or top.returncode:
        return None
    base = Path(top.stdout.strip()).resolve()
    return {(base / f).resolve().relative_to(root).as_posix() for f in out.stdout.split("\0") if f
            and (base / f).resolve().is_relative_to(root)}


def scan_secrets(root: Path, tracked: set[str] | None) -> list[dict]:
    out = []
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        is_env = p.name.lower().startswith(".env") and not p.name.lower().endswith((".example", ".sample", ".template"))
        if is_env and tracked is not None and rel in tracked:
            out.append(_f("SE2", "CRITICAL", rel, 1, f"{rel} is committed to git",
                          f"git rm --cached {rel}, add it to .gitignore, and rotate every key it held."))
        if is_env and (tracked is None or rel not in tracked):
            continue                       # a local, untracked .env is where secrets belong
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for kind, rx in SECRET_RES:
                m = rx.search(line)
                if m and not PLACEHOLDER.search(m.group(0)):
                    key = m.group(m.lastindex or 0)
                    out.append(_f("SE1", "CRITICAL", rel, i, f"{kind} {key[:6]}…{key[-4:] if len(key) > 12 else ''}",
                                  "Rotate it now, then load it from an environment variable or a secret store."))
                    break
    return out


# -- dependencies (OSV) -----------------------------------------------------------------------------

def _python_deps(root: Path) -> list[tuple[str, str, str, int]]:
    out = []
    for req in root.glob("requirements*.txt"):
        for i, line in enumerate(req.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-+!]+)", line)
            if m:
                out.append(("PyPI", m.group(1), m.group(2), i, req.name))
    return out


def _node_deps(root: Path) -> list[tuple]:
    out = []
    lock = root / "package-lock.json"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        for path, meta in (data.get("packages") or {}).items():
            if path.startswith("node_modules/") and meta.get("version") and "/node_modules/" not in path[13:]:
                out.append(("npm", path[13:], meta["version"], 1, "package-lock.json"))
        return out
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except ValueError:
            return out
        for sect in ("dependencies", "devDependencies"):
            for name, ver in (data.get(sect) or {}).items():
                if re.fullmatch(r"\d+\.\d+\.\d+", str(ver)):
                    out.append(("npm", name, ver, 1, "package.json"))
    return out


def osv_query(deps: list[tuple], timeout: float = 10) -> list[list[dict]]:
    body = json.dumps({"queries": [{"package": {"ecosystem": eco, "name": n}, "version": v} for eco, n, v, *_ in deps]}).encode()
    req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [x.get("vulns") or [] for x in json.loads(r.read().decode())["results"]]


def scan_deps(root: Path, query=osv_query) -> tuple[list[dict], str | None]:
    deps = (_python_deps(root) + _node_deps(root))[:900]
    if not deps:
        return [], None
    try:
        hits = query(deps)
    except Exception as e:  # noqa: BLE001 - offline or OSV down: say so, don't block
        return [], f"dependency check skipped ({type(e).__name__}); {len(deps)} pinned packages not checked"
    out = []
    for (eco, name, ver, line, file), vulns in zip(deps, hits):
        if not vulns:
            continue
        ids = ", ".join(v["id"] for v in vulns[:3]) + (f" +{len(vulns) - 3} more" if len(vulns) > 3 else "")
        out.append(_f("DP1", "HIGH", file, line, f"{name} {ver}: {ids}",
                      f"Upgrade {name} to a fixed version (see https://osv.dev/vulnerability/{vulns[0]['id']})."))
    return out, None


# -- web settings and containers --------------------------------------------------------------------

def scan_config(root: Path) -> list[dict]:
    out = []
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = p.name.lower()
        if p.suffix == ".py":
            for i, line in enumerate(text.splitlines(), 1):
                if re.search(r"\.run\([^)]*debug\s*=\s*True", line) or re.search(r"^\s*DEBUG\s*=\s*True\b", line):
                    out.append(_f("SE3", "HIGH", rel, i, "debug mode is on", "Turn it off, or read it from an env var that is off in production."))
                if re.search(r"allow_origins\s*=\s*\[\s*[\"']\*[\"']\s*\]", line) and re.search(r"allow_credentials\s*=\s*True", text):
                    out.append(_f("SE4", "MEDIUM", rel, i, "CORS allows every origin with credentials",
                                  "List the origins that may call the API (allow_origins=[\"https://your.app\"])."))
        if low == "dockerfile" or low.endswith(".dockerfile"):
            lines = text.splitlines()
            froms = [(i, ln) for i, ln in enumerate(lines, 1) if ln.strip().upper().startswith("FROM ")]
            for i, ln in froms:
                img = ln.split()[1] if len(ln.split()) > 1 else ""
                if img.endswith(":latest") or (":" not in img and "@" not in img and img.lower() != "scratch"):
                    out.append(_f("DK2", "LOW", rel, i, f"base image {img} is not pinned", "Pin a version tag, e.g. python:3.12-slim."))
            if froms and not any(ln.strip().upper().startswith("USER ") for ln in lines[froms[-1][0]:]):
                out.append(_f("DK1", "MEDIUM", rel, froms[-1][0], "no USER after the last FROM",
                              "Add a non-root user: RUN useradd -m app && USER app."))
    return out


def run(path: str | Path = ".", deps: bool = True, query=None) -> dict:
    root = Path(path).resolve()
    tracked = _tracked(root)
    llm = audit.static(root)
    found = scan_secrets(root, tracked) + scan_config(root)
    notes = []
    if deps:
        dep_findings, note = scan_deps(root, query or osv_query)
        found += dep_findings
        if note:
            notes.append(note)
    # one finding per secret location even if both scanners matched it
    seen = {(f["file"], f["line"]) for f in found if f["rule"] == "SE1"}
    llm = [f for f in llm if not (f["rule"] == "AU1" and (f["file"], f["line"]) in seen)]
    findings = audit.sort(llm + found)
    return {"root": str(root), "findings": findings, "notes": notes, "blocking": blocking(findings)}


def blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in ("CRITICAL", "HIGH")]


def sarif(findings: list[dict], prefix: str = "") -> str:
    data = json.loads(audit.sarif(findings, prefix))
    driver = data["runs"][0]["tool"]["driver"]
    driver["name"] = "Switchyard secure"
    driver["rules"] = [{"id": rid, "name": name.replace(" ", ""), "shortDescription": {"text": name},
                        "fullDescription": {"text": desc}, "helpUri": "https://github.com/ayushagarwalhere/switchyard#secure"}
                       for rid, (name, desc) in ALL_RULES.items()]
    return json.dumps(data, indent=1)
