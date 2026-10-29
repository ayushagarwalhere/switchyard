"""Apply the migration to a copy of the repo and produce a reviewable diff.

Edits are made at exact AST positions (no regex over source), then every touched file is
re-parsed and byte-compiled to prove the patch is valid Python.
"""
from __future__ import annotations

import ast
import difflib
import json
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .evaluate import SCHEMA_PREFIX

ENV_BASE = "TOKEN_FACTORY_BASE_URL"
ENV_KEY = "NEBIUS_API_KEY"


def _read(path: Path) -> str:
    """Read without newline translation, so CRLF files stay CRLF."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _match_newlines(new: str, original: str) -> str:
    """Inserted snippets use LF; convert them if the file uses CRLF."""
    if "\r\n" in original:
        return new.replace("\r\n", "\n").replace("\n", "\r\n")
    return new


def _to_char(line_text: str, byte_col: int) -> int:
    return len(line_text.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


class FileEditor:
    def __init__(self, text: str):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts = [0]
        for ln in self.lines:
            self.starts.append(self.starts[-1] + len(ln))
        self.edits: list[tuple[int, int, str]] = []

    def pos(self, line: int, byte_col: int) -> int:
        return self.starts[line - 1] + _to_char(self.lines[line - 1], byte_col)

    def replace(self, span, new: str):
        l1, c1, l2, c2 = span
        self.edits.append((self.pos(l1, c1), self.pos(l2, c2), new))

    def insert_at_line(self, line: int, new: str):
        p = self.starts[line - 1]
        self.edits.append((p, p, new))

    def result(self) -> str:
        out = self.text
        for start, end, new in sorted(self.edits, key=lambda e: (e[0], e[1]), reverse=True):
            out = out[:start] + new + out[end:]
        return out


def _env_name(site: dict, used: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", (site.get("function") or Path(site["file"]).stem).upper()).strip("_") or "CALL"
    name = f"LLM_MODEL_{base}"
    i = 2
    while name in used:
        name = f"LLM_MODEL_{base}_{i}"
        i += 1
    used.add(name)
    return name


def _import_line(tree: ast.Module) -> int:
    """Line to insert `import os` at: after module docstring and __future__ imports."""
    line = 1
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) and isinstance(stmt.value.value, str) and line == 1:
            line = (stmt.end_lineno or stmt.lineno) + 1
            continue
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
            line = (stmt.end_lineno or stmt.lineno) + 1
            continue
        break
    return line


def _has_os_import(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Import) and any(a.name == "os" and a.asname is None for a in stmt.names):
            return True
    return False


def _client_call(src: str, client: dict) -> str:
    """Rebuild OpenAI(...) pointing at Token Factory, keeping unrelated kwargs."""
    try:
        node = ast.parse(client["source"].strip(), mode="eval").body
    except SyntaxError:
        node = None
    keep = []
    if isinstance(node, ast.Call):
        for k in node.keywords:
            if k.arg in ("api_key", "base_url", "organization", "project", "azure_endpoint", "api_version", "azure_deployment"):
                continue
            keep.append(ast.unparse(k))
    func = client["source"].split("(", 1)[0]
    if client["sdk"] == "openai-azure":
        func = func.replace("AzureOpenAI", "OpenAI")
    args = [f'base_url=os.environ.get("{ENV_BASE}", "{config.DEFAULT_BASE_URL}")',
            f'api_key=os.environ["{ENV_KEY}"]'] + keep
    return f"{func}({', '.join(args)})"


ANTHROPIC_SHIM = '''

class _NemotronMessages:
    """Anthropic-style `messages.create()` served by Nemotron on Token Factory (OpenAI API).

    Returns an object shaped like Anthropic's Message: `.content[0].text`, `.stop_reason`, `.usage`.
    """

    def __init__(self, client):
        self._client = client
        self.messages = self

    @staticmethod
    def _text(content):
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in content or [])

    def create(self, *, model, messages, system=None, max_tokens=None, temperature=None, stop_sequences=None, **_):
        from types import SimpleNamespace
        msgs = [{"role": "system", "content": self._text(system)}] if system else []
        msgs += [{"role": m["role"], "content": self._text(m["content"])} for m in messages]
        r = self._client.chat.completions.create(model=model, messages=msgs, max_tokens=max_tokens,
                                                 temperature=temperature, stop=stop_sequences)
        choice = r.choices[0]
        stop = {"stop": "end_turn", "length": "max_tokens"}.get(choice.finish_reason, choice.finish_reason)
        usage = SimpleNamespace(input_tokens=r.usage.prompt_tokens, output_tokens=r.usage.completion_tokens)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=choice.message.content or "")],
                               stop_reason=stop, usage=usage, model=model, role="assistant", type="message")
'''


_LC_DROP = {"model", "model_name", "base_url", "openai_api_base", "api_key", "openai_api_key", "organization",
            "openai_organization", "azure_endpoint", "azure_deployment", "deployment_name", "api_version",
            "openai_api_version", "azure_ad_token", "azure_ad_token_provider", "model_provider"}


def _langchain_call(site: dict, model_expr: str) -> str | None:
    """Rebuild ChatOpenAI(...) / init_chat_model(...) against Token Factory, keeping unrelated settings."""
    try:
        node = ast.parse(site["snippet"], mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    func = ast.unparse(node.func)
    keep = [ast.unparse(k) for k in node.keywords if k.arg not in _LC_DROP]
    conn = [f'base_url=os.environ.get("{ENV_BASE}", "{config.DEFAULT_BASE_URL}")', f'api_key=os.environ.get("{ENV_KEY}")']
    if site["method"] == "init_chat_model":
        return f'{func}(model={model_expr}, model_provider="openai", {", ".join(conn + keep)})'
    if site["method"] == "AzureChatOpenAI":
        func = "ChatOpenAI"
    return f"{func}(model={model_expr}, {', '.join(conn + keep)})"


def _star_kwargs(site: dict) -> bool:
    try:
        node = ast.parse(site.get("snippet") or "", mode="eval").body
    except SyntaxError:
        return False
    return isinstance(node, ast.Call) and any(k.arg is None for k in node.keywords)


def _rewritable(site: dict) -> bool:
    if site["sdk"] in ("openai", "openai-responses"):
        return True
    if site["sdk"] == "litellm":
        return "model" in site.get("arg_spans", {}) and not _star_kwargs(site)
    if site["sdk"] in ("langchain-openai", "langchain-init"):
        return not _star_kwargs(site)
    if site["sdk"] == "anthropic":
        p = site.get("params", {})
        return bool(site.get("receiver_span")) and not site.get("awaited") and "tools" not in p and not p.get("stream")
    return False


def _manual_reason(site: dict) -> str:
    if site["sdk"] == "openai-legacy":
        return "pre-1.0 openai.ChatCompletion API; upgrade the SDK first"
    if site["sdk"] == "anthropic":
        return "async, streaming or tool-use Anthropic call; port to the OpenAI SDK by hand"
    if site["sdk"] in ("litellm", "langchain-openai", "langchain-init") and _star_kwargs(site):
        return ("settings come from **kwargs, which may already set model or api_key; set model, "
                "base_url/api_base and api_key there by hand")
    if site["sdk"] == "litellm":
        return "model passed positionally; pass model= as a keyword and re-run"
    if site["sdk"] == "langchain-anthropic":
        return ("ChatAnthropic can't call an OpenAI-compatible endpoint; replace it with "
                "langchain_openai.ChatOpenAI(model=..., base_url=..., api_key=...)")
    return "unsupported SDK"


def apply(repo: Path, out_dir: Path, scan: dict, sites: list[dict], run_tests: bool = False) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(repo, out_dir, ignore=shutil.ignore_patterns(".git"))
    js_sites = [s for s in sites if s["site"].get("lang") == "js"]
    sites = [s for s in sites if s["site"].get("lang") != "js"]
    by_file: dict[str, list[dict]] = {}
    manual: list[dict] = []
    for s in sites:
        if s.get("status") != "done" or s["decision"]["action"] != "migrate":
            continue
        if _rewritable(s["site"]):
            by_file.setdefault(s["site"]["file"], []).append(s)
        else:
            manual.append({"file": s["site"]["file"], "line": s["site"]["line"], "sdk": s["site"]["sdk"],
                           "model": config.MODELS[s["decision"]["model"]].id, "reason": _manual_reason(s["site"])})
    clients_by_file: dict[str, list[dict]] = {}
    for c in scan["clients"]:
        if c["sdk"].startswith("openai") and not c.get("js"):
            clients_by_file.setdefault(c["file"], []).append(c)

    used_env: set[str] = set()
    routes: list[dict] = []
    changed: list[str] = []

    # If every OpenAI call in the repo migrates, point the existing clients at Token Factory
    # (smallest diff). If some calls stay on the original provider, leave the clients alone and
    # send migrated calls through a dedicated Token Factory client instead.
    openai_sites = [s for s in sites if s["site"]["sdk"].startswith("openai")]
    all_migrate = bool(openai_sites) and all(s.get("status") == "done" and s["decision"]["action"] == "migrate"
                                             for s in openai_sites)
    mode = "rewrite-clients" if all_migrate and clients_by_file else "dedicated-client"
    files = set(by_file) | (set(clients_by_file) if mode == "rewrite-clients" else set())
    for rel in sorted(files):
        path = out_dir / rel
        text = _read(path)
        tree = ast.parse(text)
        ed = FileEditor(text)
        if mode == "rewrite-clients":
            for c in clients_by_file.get(rel, []):
                ed.replace((c["line"], c["col"], c["end_line"], c["end_col"]), _client_call(text, c))
        needs_sync = needs_async = needs_anthropic = needs_lc = False
        for s in by_file.get(rel, []):
            site, dec = s["site"], s["decision"]
            model_id = config.MODELS[dec["model"]].id
            env = _env_name(site, used_env)
            expr = f'os.environ.get("{env}", "{model_id}")'
            env_value = model_id
            if site["sdk"] == "litellm":
                # LiteLLM's "openai/" provider talks to any OpenAI-compatible endpoint
                env_value = f"openai/{model_id}"
                spans = site["arg_spans"]
                base = f'os.environ.get("{ENV_BASE}", "{config.DEFAULT_BASE_URL}")'
                key = f'os.environ.get("{ENV_KEY}")'
                extra = []
                for names, value in ((("api_base", "base_url"), base), (("api_key",), key)):
                    hit = next((n for n in names if n in spans), None)
                    if hit:
                        ed.replace(spans[hit], value)
                    else:
                        extra.append(f"{names[0]}={value}")
                ed.replace(spans["model"], f'os.environ.get("{env}", "{env_value}")' + "".join(f", {e}" for e in extra))
            elif site["sdk"].startswith("langchain"):
                new = _langchain_call(site, expr)
                if new is None:
                    manual.append({"file": rel, "line": site["line"], "sdk": site["sdk"], "model": model_id,
                                   "reason": "couldn't rebuild the constructor; set model/base_url/api_key by hand"})
                    continue
                ed.replace(site["call_span"], new)
                needs_lc |= site["method"] == "AzureChatOpenAI"
                if dec.get("hint"):
                    manual.append({"file": rel, "line": site["line"], "sdk": site["sdk"], "model": model_id,
                                   "reason": f"add this instruction to the prompt's system message: {dec['hint']}"})
            elif "model" in site["arg_spans"]:
                ed.replace(site["arg_spans"]["model"], expr)
            if site["sdk"] == "anthropic":
                # Anthropic clients can't be pointed at an OpenAI-compatible endpoint: route this call
                # through a shim with the same messages.create() shape, backed by Token Factory.
                ed.replace(site["receiver_span"], "_token_factory_anthropic")
                needs_anthropic = True
            elif mode == "dedicated-client" and site.get("receiver_span") and site["sdk"].startswith("openai"):
                ed.replace(site["receiver_span"], "_token_factory_async" if site.get("awaited") else "_token_factory")
                needs_async |= bool(site.get("awaited"))
                needs_sync |= not site.get("awaited")
            key = "input" if site["sdk"] == "openai-responses" else "messages"
            prefix = []
            # .parse(response_format=Model): put the schema's field descriptions in the prompt, because
            # Token Factory only enforces the shape (see evaluate.SCHEMA_PREFIX)
            if site["sdk"] in ("openai", "litellm") and (site.get("params") or {}).get("output_schema"):
                for k in ("response_format", "response_model"):
                    if k in site["arg_spans"]:
                        l1, c1, l2, c2 = site["arg_spans"][k]
                        cls = text[ed.pos(l1, c1):ed.pos(l2, c2)]
                        if re.fullmatch(r"[A-Za-z_][\w.]*", cls):
                            prefix.append(f'{{"role": "system", "content": {json.dumps(SCHEMA_PREFIX)} + '
                                          f'__import__("json").dumps(getattr({cls}, "model_json_schema", getattr({cls}, "schema", dict))())}}')
                        break
            if dec.get("hint"):
                prefix.append(f'{{"role": "system", "content": {json.dumps(dec["hint"])}}}')
            if prefix and key in site["arg_spans"]:
                l1, c1, l2, c2 = site["arg_spans"][key]
                orig = text[ed.pos(l1, c1):ed.pos(l2, c2)]
                ed.replace(site["arg_spans"][key], f'[{", ".join(prefix)}] + list({orig})')
            routes.append({"file": rel, "line": site["line"], "function": site["function"], "env": env,
                           "from": site.get("model") or site.get("model_expr"), "to": model_id,
                           "hint": dec.get("hint", ""), "pass_rate": dec["pass_rate"], "env_value": env_value})
        if not ed.edits:
            continue
        header = ""
        if not _has_os_import(tree):
            header += "import os\n"
        have = {a.name for s in tree.body if isinstance(s, ast.ImportFrom) and s.module == "openai"
                for a in s.names if a.asname is None}
        names = ", ".join(n for n, on in (("AsyncOpenAI", needs_async), ("OpenAI", needs_sync or needs_anthropic))
                          if on and n not in have)
        if names:
            header += f"from openai import {names}\n"
        if needs_lc and not any(isinstance(st, ast.ImportFrom) and st.module == "langchain_openai"
                                and any(a.name == "ChatOpenAI" and a.asname is None for a in st.names) for st in tree.body):
            header += "from langchain_openai import ChatOpenAI\n"
        line = _import_line(tree)
        if needs_sync or needs_async or needs_anthropic:
            # define the client after the last top-level import so the module's own imports run first
            last_import = max((s.end_lineno or s.lineno for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))), default=line - 1)
            client_def = "\n# Switchyard: calls migrated to NVIDIA Nemotron on Nebius Token Factory\n"
            kw = f'base_url=os.environ.get("{ENV_BASE}", "{config.DEFAULT_BASE_URL}"), api_key=os.environ.get("{ENV_KEY}")'
            if needs_sync:
                client_def += f"_token_factory = OpenAI({kw})\n"
            if needs_async:
                client_def += f"_token_factory_async = AsyncOpenAI({kw})\n"
            if needs_anthropic:
                client_def += ANTHROPIC_SHIM + f"\n_token_factory_anthropic = _NemotronMessages(OpenAI({kw}))\n"
            ed.insert_at_line(last_import + 1, client_def)
        if header:
            ed.insert_at_line(line, header)
        _write(path, _match_newlines(ed.result(), text))
        changed.append(rel)

    # verify: every touched file must parse and compile
    checks = []
    for rel in changed:
        try:
            ast.parse((out_dir / rel).read_text(encoding="utf-8"))
            py_compile.compile(str(out_dir / rel), doraise=True)
            checks.append({"file": rel, "ok": True})
        except Exception as e:  # noqa: BLE001
            checks.append({"file": rel, "ok": False, "error": str(e)[:300]})
    for p in out_dir.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)

    if js_sites:
        from .jsrewrite import apply_js
        js = apply_js(out_dir, js_sites, scan["clients"], used_env)
        changed += js["changed"]
        routes += js["routes"]
        manual += js["manual"]
        checks += js["checks"]

    tests = None
    if run_tests and any((out_dir / d).exists() for d in ("tests", "test")):
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "--no-header"], cwd=out_dir,
                               capture_output=True, text=True, timeout=300)
            tests = {"ran": True, "ok": r.returncode == 0, "tail": (r.stdout + r.stderr)[-2000:]}
        except Exception as e:  # noqa: BLE001
            tests = {"ran": False, "error": str(e)}

    diff = []
    for rel in changed:
        a = _read(repo / rel).splitlines(keepends=True)
        b = _read(out_dir / rel).splitlines(keepends=True)
        diff.extend(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}"))
    diff_text = "".join(diff)
    _write(out_dir.parent / "migration.patch", diff_text)
    js_note = (["# JavaScript/TypeScript: for Next.js put these in .env.local. They're read on the server only;",
                "# never prefix them with NEXT_PUBLIC_, which would ship the key to the browser."]
               if any(r["file"].endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")) for r in routes) else [])
    env_example = "\n".join(js_note + [f"{ENV_KEY}=your-token-factory-key", f"{ENV_BASE}={config.DEFAULT_BASE_URL}"] +
                            [f"# {r['file']}:{r['line']} {r['function']}\n{r['env']}={r.get('env_value', r['to'])}" for r in routes]) + "\n"
    (out_dir.parent / "migration.env").write_text(env_example, encoding="utf-8")
    return {"mode": mode, "changed": changed, "routes": routes, "manual": manual, "compile": checks, "tests": tests,
            "diff": diff_text, "env": env_example,
            "ok": all(c["ok"] for c in checks)}
