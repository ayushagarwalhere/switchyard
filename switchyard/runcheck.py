"""Run the migrated code for real: call each switched function once, against Token Factory.

Prompt-level tests show a model *can* do the task. This step shows the patched application
code works: Switchyard loads the migrated module, calls the switched function with a test
case's inputs, and records every HTTP request it makes. A call site is **verified** when the
function returns normally and its request went to Token Factory with the chosen model.

How the call is built:
  1. If the function's parameters line up with the prompt's template variables, call it
     directly (no model involved).
  2. Otherwise Nemotron writes the call (e.g. builds a Pydantic object the function takes),
     Switchyard runs it, and on a crash feeds the traceback back for another attempt.

Only definitions are loaded (imports, classes, functions, constants): top-level demo code
such as `result = main()` is skipped, so loading a module doesn't fire off its examples.
Each call runs in its own subprocess with a timeout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .llm import TokenFactory

MARK = "@@SWITCHYARD@@"
# openai SDK exceptions raised for HTTP/API problems (as opposed to the app mishandling an answer)
API_ERROR = re.compile(r"(BadRequest|NotFound|Authentication|PermissionDenied|RateLimit|InternalServer|"
                       r"UnprocessableEntity|Conflict|APIStatus|APIConnection|APITimeout)Error\b")
TIMEOUT = int(os.environ.get("SWITCHYARD_EXEC_TIMEOUT", "120"))

HARNESS = r'''
import ast, asyncio, json, os, sys, time, traceback, types

REQUESTS = []

def _record(req):
    model = None
    try:
        model = json.loads(req.content or b"{}").get("model")
    except Exception:
        pass
    entry = {"url": str(req.url), "model": model, "status": None}
    REQUESTS.append(entry)
    return entry

def _hook(lib):
    send, asend = lib.Client.send, lib.AsyncClient.send
    def patched_send(self, request, *a, **k):
        entry = _record(request)
        resp = send(self, request, *a, **k)
        entry["status"] = getattr(resp, "status_code", None)
        return resp
    async def patched_asend(self, request, *a, **k):
        entry = _record(request)
        resp = await asend(self, request, *a, **k)
        entry["status"] = getattr(resp, "status_code", None)
        return resp
    lib.Client.send, lib.AsyncClient.send = patched_send, patched_asend

# openai 1.x/2.x send through httpx, 3.x through httpx2
for _name in ("httpx", "httpx2"):
    try:
        _hook(__import__(_name))
    except (ImportError, AttributeError):
        pass


def _calls_local(node, local):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in local for n in ast.walk(node))


def _keep(stmt, local):
    if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return True
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return stmt.value is not None and not _calls_local(stmt.value, local)
    if isinstance(stmt, ast.Try):  # try: import x / except ImportError: ...
        inner = stmt.body + [s for h in stmt.handlers for s in h.body] + stmt.orelse + stmt.finalbody
        return all(_keep(s, local) or isinstance(s, ast.Pass) for s in inner)
    return False


class _Missing:
    """Stands in for a module this environment lacks (e.g. `import nest_asyncio` used only by demo code)."""
    def __init__(self, err):
        self._err = err
    def __getattr__(self, attr):
        raise ModuleNotFoundError(self._err)
    def __call__(self, *a, **k):
        raise ModuleNotFoundError(self._err)


def load_definitions(path, name):
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    local = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    body = [s for s in tree.body if _keep(s, local)]
    mod = types.ModuleType(name)
    mod.__file__ = path
    sys.modules[name] = mod
    flags = 0
    if any(isinstance(s, ast.ImportFrom) and s.module == "__future__" and any(a.name == "annotations" for a in s.names) for s in body):
        import __future__
        flags = __future__.annotations.compiler_flag
    # one statement at a time, so an import missing here only fails the call that really uses it
    for stmt in body:
        code = compile(ast.Module(body=[stmt], type_ignores=[]), path, "exec", flags=flags, dont_inherit=True)
        try:
            exec(code, mod.__dict__)
        except ModuleNotFoundError as e:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                raise
            for a in stmt.names:
                if a.name != "*":
                    mod.__dict__[a.asname or a.name.split(".")[0]] = _Missing(str(e))
    return mod


def _preview(x):
    try:
        if hasattr(x, "model_dump"):
            return json.dumps(x.model_dump(), default=str)[:600]
    except Exception:
        pass
    return repr(x)[:600]


def main():
    spec = json.loads(sys.stdin.read())
    path = os.path.abspath(spec["file"])
    sys.path[:0] = [os.path.dirname(path), os.getcwd()]
    out = {"stage": "load"}
    t0 = time.time()
    try:
        m = load_definitions(path, "switchyard_target")
        out["stage"] = "call"
        ns = {}
        exec(spec["code"], ns)
        result = ns["make_call"](m, spec["inputs"])
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        out.update(ok=True, type=type(result).__name__, preview=_preview(result))
    except BaseException as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}"[:600], traceback=traceback.format_exc()[-2500:])
    out["requests"] = REQUESTS
    out["seconds"] = round(time.time() - t0, 2)
    print("@@SWITCHYARD@@" + json.dumps(out, default=str), flush=True)


main()
'''

AGENT_SYS = """You write Python that calls one function from an application, for an automated smoke test.
Write the BODY of `def make_call(m, inputs):` (no def line). `m` is the loaded application module; `inputs` is a dict of the values that the function's LLM prompt should end up containing (keys are prompt template variables, values are strings).
Build whatever arguments the function needs from `inputs` (parse JSON, construct the module's Pydantic models via m.ClassName, convert numbers), call m.<function> exactly once, and return its result. If it is async, return the coroutine.
Use only `m`, `inputs` and the standard library. No network calls, no printing, no mocking.
Reply with JSON only: {"code": "<function body>"}"""


def _signature(path: Path, func: str) -> list[dict] | None:
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            a = node.args
            pos = a.posonlyargs + a.args
            n_def = len(a.defaults)
            params = [{"name": p.arg, "required": i < len(pos) - n_def} for i, p in enumerate(pos)]
            params += [{"name": p.arg, "required": d is None} for p, d in zip(a.kwonlyargs, a.kw_defaults)]
            return params
    return None  # methods and nested functions need the agent


def direct_call(params: list[dict] | None, func: str, inputs: dict) -> str | None:
    """Call code when parameters line up with template variables; None if they don't."""
    if params is None:
        return None
    if params and params[0]["name"] in ("self", "cls"):
        return None
    required = [p["name"] for p in params if p["required"]]
    names = {p["name"] for p in params}
    if all(r in inputs for r in required):
        args = {k: k for k in inputs if k in names}
        kw = ", ".join(f"{k}=inputs[{k!r}]" for k in args)
        return f"def make_call(m, inputs):\n    return m.{func}({kw})\n"
    # names don't line up (e.g. f(text) with a prompt variable {doc}): guessing positionally can feed the
    # wrong value and still "verify", so Nemotron reads the source and writes the call instead
    return None


def _agent_code(tf: TokenFactory, site: dict, file_src: str, inputs: dict, feedback: str = "") -> str:
    user = (f"Function to call: {site['function']} (defined in {site['file']})\n"
            f"Prompt template variables and the values to use: {json.dumps(inputs)[:3000]}\n\n"
            f"Module source:\n```python\n{file_src[:14000]}\n```")
    if feedback:
        user += f"\n\nYour previous attempt failed. Fix it.\n{feedback}"
    data, _ = tf.json(config.ROLE_ANALYZE, [{"role": "system", "content": AGENT_SYS}, {"role": "user", "content": user}],
                      role="harness", temperature=0.1, reasoning=False, max_tokens=1500)
    body = data.get("code", "") if isinstance(data, dict) else ""
    body = textwrap.dedent(body.strip("\n"))
    if body.lstrip().startswith("def make_call"):
        return body + "\n"
    return "def make_call(m, inputs):\n" + textwrap.indent(body or "raise RuntimeError('no code')", "    ") + "\n"


def _execute(root: Path, rel: str, code: str, inputs: dict, env: dict) -> dict:
    spec = json.dumps({"file": str(root / rel), "code": code, "inputs": inputs})
    try:
        # the spec goes over stdin: Windows caps a command line at 32K chars, and an app calling
        # input() then gets EOF instead of blocking on the parent's console until the timeout
        r = subprocess.run([sys.executable, "-c", HARNESS], input=spec, cwd=root, env=env, capture_output=True,
                           text=True, timeout=TIMEOUT, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "call", "error": f"timed out after {TIMEOUT}s", "requests": []}
    line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith(MARK)), None)
    if line is None:
        return {"ok": False, "stage": "load", "error": (r.stderr or r.stdout or "no output").strip()[-600:], "requests": []}
    return json.loads(line[len(MARK):])


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("TOKEN_FACTORY_BASE_URL", config.DEFAULT_BASE_URL)
    if config.api_key():
        env["NEBIUS_API_KEY"] = config.api_key()
    # clients for calls that stayed on the original provider are still constructed at import
    env.setdefault("OPENAI_API_KEY", "sk-switchyard-placeholder")
    env.setdefault("ANTHROPIC_API_KEY", "sk-ant-switchyard-placeholder")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _pick_case(cases: list[dict]) -> dict | None:
    return next((c for c in cases if c.get("kind") == "typical"), None) or (cases[0] if cases else None)


def verify_site(tf: TokenFactory, root: Path, s: dict, env: dict, attempts: int = 3) -> dict:
    site, model_id = s["site"], config.MODELS[s["decision"]["model"]].id
    base = {"id": site["id"], "file": site["file"], "line": site["line"], "function": site.get("function"), "model": model_id}
    case = _pick_case(s.get("cases") or [])
    if not site.get("function") or case is None:
        return base | {"status": "skipped", "reason": "module-level call" if case else "no test case"}
    path = root / site["file"]
    inputs = case["inputs"]
    code = direct_call(_signature(path, site["function"]), site["function"], inputs)
    how = "direct"
    log = []
    res: dict = {}
    for attempt in range(attempts):
        if code is None:
            how = "agent"
            code = _agent_code(tf, site, path.read_text(encoding="utf-8", errors="replace"), inputs,
                               feedback=f"Code:\n{log[-1]['code']}\nError:\n{log[-1]['traceback'] or log[-1]['error']}" if log else "")
        res = _execute(root, site["file"], code, inputs, env)
        log.append({"code": code, "error": res.get("error"), "traceback": res.get("traceback", "")})
        # retry only when the harness itself failed before the app reached the model
        if res.get("ok") or res.get("requests") or res.get("stage") == "load":
            break
        code = None
    tf_host = urlparse(env.get("TOKEN_FACTORY_BASE_URL", config.DEFAULT_BASE_URL)).netloc
    hits = [r for r in res.get("requests", []) if urlparse(r["url"]).netloc == tf_host]
    on_model = [r for r in hits if r.get("model") == model_id]
    answered = [r for r in on_model if 200 <= (r.get("status") or 0) < 300]
    if res.get("ok") and answered:
        status, reason = "verified", f"returned {res.get('type')} after calling {model_id} on Token Factory"
    elif res.get("ok"):
        status = "unverified"
        reason = ("ran, but no request reached Token Factory" if not hits
                  else f"ran, but called {hits[0].get('model')} instead of {model_id}" if not on_model
                  else f"returned, but Token Factory answered HTTP {on_model[-1].get('status')} (the app swallowed the error)")
    elif res.get("requests") and not hits:
        status = "failed"
        reason = f"the call went to {urlparse(res['requests'][0]['url']).netloc}, not Token Factory: {res.get('error')}"
    elif res.get("requests") and API_ERROR.match(res.get("error") or ""):
        status, reason = "failed", f"Token Factory rejected the request: {res.get('error')}"
    elif res.get("requests"):
        status, reason = "failed", f"the app code failed on the model's answer: {res.get('error')}"
    elif res.get("stage") == "load":
        status, reason = "skipped", f"couldn't load the module here: {res.get('error')}"
    else:
        status, reason = "skipped", f"couldn't build a call: {res.get('error')}"
    return base | {"status": status, "reason": reason[:400], "how": how, "attempts": len(log), "case": case.get("name"),
                   "seconds": res.get("seconds"), "returned": res.get("type"), "preview": res.get("preview"),
                   "requests": res.get("requests", [])[:6], "code": code or (log[-1]["code"] if log else "")}


def verify(tf: TokenFactory, root: Path, sites: list[dict], manual: list[dict] | None = None, emit=None) -> list[dict]:
    """Run every switched (and rewritten) call site once through the migrated code."""
    env = _env()
    by_hand = {(m["file"], m["line"]) for m in manual or []}
    todo = [s for s in sites if s.get("status") == "done" and s["decision"]["action"] == "migrate"]

    def one(s):
        if s["site"].get("lang") == "js":
            site = s["site"]
            r = {"id": site["id"], "file": site["file"], "line": site["line"], "function": site.get("function"),
                 "model": config.MODELS[s["decision"]["model"]].id, "status": "skipped",
                 "reason": "JavaScript/TypeScript isn't executed by Switchyard; the patch is syntax-checked, run your app's tests"}
        elif (s["site"]["file"], s["site"]["line"]) in by_hand:
            site = s["site"]
            r = {"id": site["id"], "file": site["file"], "line": site["line"], "function": site.get("function"),
                 "model": config.MODELS[s["decision"]["model"]].id, "status": "skipped", "reason": "needs a manual port"}
        else:
            try:
                r = verify_site(tf, root, s, env)
            except Exception as e:  # noqa: BLE001 - one site's check must never sink the finished run
                site = s["site"]
                r = {"id": site["id"], "file": site["file"], "line": site["line"], "function": site.get("function"),
                     "model": config.MODELS[s["decision"]["model"]].id, "status": "skipped",
                     "reason": f"runtime check crashed: {type(e).__name__}: {e}"[:400]}
        if emit:
            emit(r)
        return r

    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(one, todo))
    return out


def enabled() -> bool:
    """Executing repo code is on for local use, off on the public demo unless explicitly allowed."""
    default = "0" if os.environ.get("SWITCHYARD_PUBLIC") == "1" else "1"
    return os.environ.get("SWITCHYARD_EXEC", default) == "1"
