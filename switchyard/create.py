"""`switchyard new "<idea>"`: Nemotron writes a working, deployable app from one sentence.

The parts that must be right every time (Dockerfile, pinned requirements, the Token Factory client, smoke
tests, .gitignore) are written by Switchyard itself. Nemotron writes the app logic and the frontend, then
the result is compiled and tested, and failures are fixed from the error output before you get it.

Every AI feature in the app is its own call with its own prompt, so `switchyard run` can later move each
one to the cheapest Nemotron model that passes its tests.
"""
from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path

from . import config
from .llm import TokenFactory

MODEL_IDS = {k: config.MODELS[k].id for k in ("lightning", "super", "ultra")}

PLAN_SYS = """You plan a small web app that a non-expert ("vibe coder") described in one sentence.
Stack (fixed): Python FastAPI backend in app/main.py, plain HTML/CSS/JS frontend in static/ (no build step), served by FastAPI.
AI features (if the idea needs any) call NVIDIA Nemotron through an OpenAI-compatible client that is already written for you.
Keep it small enough to be correct: 2-6 API endpoints, in-memory or JSON-file storage (no database server), no login unless the idea needs it.
Reply with JSON only:
{"name": "kebab-case-slug", "title": "Human title", "summary": "one sentence",
 "features": ["what a user can do", ...],
 "endpoints": [{"method": "GET|POST|PUT|DELETE", "path": "/api/...", "purpose": "...", "request": "JSON body fields or null", "response": "JSON shape", "uses_ai": true|false}],
 "ai_features": [{"name": "snake_case_function_name", "purpose": "what the model does", "input": "...", "output": "text | JSON with keys ...", "tier": "lightning|super"}],
 "ui": "one paragraph: the page layout and how the user interacts"}
Choose tier "lightning" for classification, extraction, routing and short rewrites; "super" for longer writing or reasoning."""

MAIN_SYS = """You write app/main.py for a FastAPI app. Output ONLY the Python file, no markdown fences, no commentary.
Requirements:
- `app = FastAPI(title=...)` at module level. Serve the frontend: `app.mount("/static", StaticFiles(directory=STATIC), name="static")` and `GET /` returning FileResponse(STATIC / "index.html"), where `STATIC = Path(__file__).resolve().parent.parent / "static"`.
- `GET /api/health` returns {"ok": true}.
- Implement every planned endpoint with Pydantic request models and clear 4xx errors for bad input.
- Storage: module-level dicts/lists, or a JSON file under DATA_DIR = Path(os.environ.get("DATA_DIR", "data")) created on demand.
- AI features: `from app.llm import client, model_for, ai_available`. Write one function per AI feature, named as planned. Each calls
  `client().chat.completions.create(model=model_for("<FUNCTION_NAME_UPPER>", "<tier>"), messages=[{"role": "system", "content": "<specific instructions>"}, {"role": "user", "content": <the user's text>}], max_tokens=<a sensible cap>, temperature=<0-0.7>)`
  with the system prompt written as a literal string (never put user text in the system message). For JSON output, ask for JSON in the system prompt and parse with json.loads inside try/except (fall back gracefully).
- If `not ai_available()`, AI endpoints return HTTP 503 with {"detail": "Set NEBIUS_API_KEY to enable AI features"} instead of crashing.
- No hard-coded keys, no debug=True, no eval/exec, no shell commands. Standard library + fastapi + pydantic + openai only."""

FRONT_SYS = """You write one frontend file for a small web app served by FastAPI at /static/ (index.html is served at /).
Output ONLY the file content, no markdown fences, no commentary.
- index.html links /static/style.css and /static/app.js (defer) and contains the full page structure with ids the JS uses.
- app.js uses fetch() against the API exactly as given (same paths, methods and JSON fields), shows loading and error states, and escapes any text it inserts as HTML.
- style.css: a clean, modern, responsive design (system font stack, generous spacing, one accent color, works at 375px wide and on desktop), dark-mode aware with prefers-color-scheme.
Make it look like a polished product, not a demo."""

FIX_SYS = """You fix a Python file so the app's tests pass. Output ONLY the full corrected file, no markdown fences, no commentary.
Keep every endpoint and feature; change only what the error requires."""

LLM_PY = '''"""Nemotron on Nebius Token Factory (OpenAI-compatible). Written by Switchyard."""
import os
from functools import lru_cache

from openai import OpenAI

BASE_URL = os.environ.get("TOKEN_FACTORY_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/")
TIERS = __TIERS__


def ai_available() -> bool:
    return bool(os.environ.get("NEBIUS_API_KEY"))


@lru_cache(maxsize=1)
def client() -> OpenAI:
    return OpenAI(base_url=BASE_URL, api_key=os.environ.get("NEBIUS_API_KEY"), timeout=60, max_retries=2)


def model_for(feature: str, tier: str = "super") -> str:
    """Each AI feature has its own model, overridable with LLM_MODEL_<FEATURE> (one-line rollback)."""
    return os.environ.get(f"LLM_MODEL_{feature.upper()}", TIERS.get(tier, TIERS["super"]))
'''

TEST_PY = '''"""Smoke tests written by Switchyard: the app starts, serves its page, and reports healthy."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json().get("ok") is True


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_static_assets():
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
'''

DOCKERFILE = """FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/tmp/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
RUN useradd --create-home appuser
USER appuser
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
"""


def _pins() -> str:
    lines = []
    for pkg, fallback in (("fastapi", "0.136.3"), ("uvicorn", "0.47.0"), ("openai", "3.24.0"), ("pydantic", "2.13.4")):
        try:
            v = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            v = fallback
        lines.append(f"{pkg}=={v}")
    return "\n".join(lines) + "\n"


def _strip_fences(text: str) -> str:
    """The file itself: drop markdown fences, chatter around them, and a bare filename on the first line."""
    text = text.strip()
    blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)\n```", text, re.S)
    if blocks:
        text = max(blocks, key=len)
    lines = text.strip().splitlines()
    while lines and re.fullmatch(r"\s*(?:#|//|<!--)?\s*[\w./-]+\.(?:py|js|css|html|md)\s*(?:-->)?\s*:?\s*", lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip() + "\n"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "my-app"


def readme(plan: dict) -> str:
    eps = "\n".join(f"| `{e['method']} {e['path']}` | {e.get('purpose', '')} |" for e in plan.get("endpoints", []))
    ai = "\n".join(f"- **{a['name']}**: {a.get('purpose', '')} (starts on Nemotron {a.get('tier', 'super').title()})"
                   for a in plan.get("ai_features", [])) or "- none"
    return f"""# {plan.get('title', plan['name'])}

{plan.get('summary', '')}

Built with `switchyard new`. AI runs on NVIDIA Nemotron through Nebius Token Factory.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env                              # put your Token Factory key in .env
uvicorn app.main:app --reload                     # http://127.0.0.1:8000
```

## Ship it

```bash
switchyard secure      # secrets, vulnerable dependencies, risky settings, every LLM call
switchyard run         # move each AI feature to the cheapest Nemotron model that passes its tests
switchyard deploy      # container -> Nebius Container Registry -> Nebius Serverless endpoint
```

## API

| Endpoint | What it does |
|---|---|
{eps}

## AI features

{ai}

Each one's model can be changed with `LLM_MODEL_<NAME>` (for example `LLM_MODEL_{(plan.get('ai_features') or [{'name': 'summarize'}])[0]['name'].upper()}`).
"""


def scaffold(dest: Path, plan: dict) -> None:
    """Everything that must be right every time, written without a model."""
    (dest / "app").mkdir(parents=True, exist_ok=True)
    (dest / "static").mkdir(exist_ok=True)
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "app" / "__init__.py").write_text("", encoding="utf-8")
    (dest / "app" / "llm.py").write_text(LLM_PY.replace("__TIERS__", json.dumps(MODEL_IDS)), encoding="utf-8")
    (dest / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (dest / "tests" / "test_app.py").write_text(TEST_PY, encoding="utf-8")
    (dest / "requirements.txt").write_text(_pins(), encoding="utf-8")
    (dest / "requirements-dev.txt").write_text("-r requirements.txt\npytest\nhttpx\n", encoding="utf-8")
    (dest / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (dest / ".dockerignore").write_text(".git\n.env\n.venv\n__pycache__\ntests\ndata\n", encoding="utf-8")
    (dest / ".gitignore").write_text(".env\n.venv/\n__pycache__/\ndata/\n.pytest_cache/\n", encoding="utf-8")
    envs = ["NEBIUS_API_KEY=", "# one-line rollback / override per AI feature:"]
    envs += [f"# LLM_MODEL_{a['name'].upper()}={MODEL_IDS.get(a.get('tier', 'super'), MODEL_IDS['super'])}"
             for a in plan.get("ai_features", [])]
    (dest / ".env.example").write_text("\n".join(envs) + "\n", encoding="utf-8")
    (dest / "README.md").write_text(readme(plan), encoding="utf-8")
    (dest / "switchyard.json").write_text(json.dumps({"created_by": "switchyard new", "plan": plan}, indent=1), encoding="utf-8")


def run_tests(dest: Path, timeout: int = 120) -> tuple[bool, str]:
    """Compile, then run the smoke tests in a fresh interpreter (the generated app is a separate program)."""
    for p in [*dest.glob("app/*.py"), *dest.glob("tests/*.py")]:
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e:
            return False, f"SyntaxError in {p.relative_to(dest)} line {e.lineno}: {e.msg}\n{e.text or ''}"
    env = {k: v for k, v in __import__("os").environ.items() if not k.endswith("_API_KEY") and "TOKEN_FACTORY" not in k}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"], cwd=dest,
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    out = (r.stdout + r.stderr)[-4000:]
    return r.returncode == 0, out


class Creator:
    def __init__(self, idea: str, dest: str | Path | None = None, budget: float = 0.6, emit=print, tf=None,
                 parent: str | Path | None = None):
        self.idea = idea.strip()
        self.dest = Path(dest) if dest else None
        self.parent = Path(parent) if parent else None
        self.emit = emit
        self.tf = tf or TokenFactory(budget_usd=budget)

    def _code(self, system: str, user: str, model: str = "super", max_tokens: int = 7000) -> str:
        c = self.tf.complete(model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                             role="write", temperature=0.2, max_tokens=max_tokens, reasoning=False)
        if c.error:
            raise RuntimeError(f"Token Factory error: {c.error}")
        return _strip_fences(c.text)

    def plan(self) -> dict:
        data, _ = self.tf.json("super", [{"role": "system", "content": PLAN_SYS},
                                         {"role": "user", "content": f"App idea: {self.idea}"}],
                               role="plan", temperature=0.3, reasoning=False)
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), {})
        data["name"] = _slug(data.get("name") or self.idea)
        data.setdefault("endpoints", [])
        data.setdefault("ai_features", [])
        for a in data["ai_features"]:
            a["name"] = re.sub(r"\W+", "_", a.get("name", "ai_feature")).strip("_").lower() or "ai_feature"
            if a.get("tier") not in MODEL_IDS:
                a["tier"] = "super"
        return data

    def build(self) -> dict:
        self.emit("Planning the app with Nemotron Super…")
        plan = self.plan()
        dest = (self.dest or (self.parent or Path.cwd()) / plan["name"]).resolve()
        if dest.exists() and any(dest.iterdir()):
            raise FileExistsError(f"{dest} already exists and isn't empty; pass --dir to choose another folder")
        self.emit(f"  {plan.get('title', plan['name'])}: {len(plan['endpoints'])} endpoints, "
                  f"{len(plan['ai_features'])} AI feature(s)")
        scaffold(dest, plan)
        brief = json.dumps(plan, indent=1)
        self.emit("Writing the backend (app/main.py)…")
        main = self._code(MAIN_SYS, f"App plan:\n{brief}")
        (dest / "app" / "main.py").write_text(main, encoding="utf-8")
        api = "\n".join(f"{e['method']} {e['path']}: request {e.get('request')} -> response {e.get('response')}"
                        for e in plan["endpoints"])
        for name in ("index.html", "app.js", "style.css"):
            self.emit(f"Writing the frontend (static/{name})…")
            ctx = f"App plan:\n{brief}\n\nAPI (implemented exactly like this):\n{api}\n\nBackend source for reference:\n{main[:9000]}"
            if name != "index.html":
                ctx += f"\n\nindex.html (use its ids and classes):\n{(dest / 'static' / 'index.html').read_text(encoding='utf-8')[:6000]}"
            (dest / "static" / name).write_text(self._code(FRONT_SYS, f"Write static/{name}.\n\n{ctx}", max_tokens=6000),
                                                encoding="utf-8")
        ok, log = False, ""
        for attempt in range(3):
            self.emit("Testing the app…" if not attempt else f"Fixing from the test output (try {attempt})…")
            ok, log = run_tests(dest)
            if ok:
                break
            if attempt == 2:
                break
            src = (dest / "app" / "main.py").read_text(encoding="utf-8")
            fixed = self._code(FIX_SYS, f"Test output:\n{log}\n\nTests:\n{TEST_PY}\n\napp/llm.py:\n{(dest / 'app' / 'llm.py').read_text()}"
                                        f"\n\napp/main.py:\n{src}")
            (dest / "app" / "main.py").write_text(fixed, encoding="utf-8")
        self.emit("  tests pass" if ok else "  tests still fail; see the output below and ask `switchyard code` to fix it")
        git_init(dest)
        from . import secure
        sec = secure.run(dest, deps=False)
        return {"dest": str(dest), "plan": plan, "tests_ok": ok, "test_log": log if not ok else "",
                "secure": {"findings": len(sec["findings"]), "blocking": len(sec["blocking"])},
                "usage": self.tf.usage.to_dict()}


def git_init(dest: Path) -> None:
    def git(*a):
        return subprocess.run(["git", "-C", str(dest), *a], capture_output=True, text=True)
    if git("rev-parse", "--show-toplevel").stdout.strip() == str(dest).replace("\\", "/"):
        return
    git("init", "-q", "-b", "main")
    git("add", "-A")
    ident = [] if git("config", "user.email").stdout.strip() else ["-c", "user.name=Switchyard", "-c", "user.email=switchyard@localhost"]
    subprocess.run(["git", "-C", str(dest), *ident, "commit", "-q", "-m", "New app from switchyard new"], capture_output=True)
