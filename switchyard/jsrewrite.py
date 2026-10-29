"""Rewrite JavaScript / TypeScript call sites to Nemotron on Token Factory (text edits at exact offsets).

* OpenAI SDK: `new OpenAI({...})` points at Token Factory when every OpenAI call moves; otherwise the
  migrated calls go through a dedicated `tokenFactory` client and the original client is untouched.
* Vercel AI SDK: `model: openai("gpt-4o")` becomes `model: tokenFactory.chat(...)` where
  `tokenFactory = createOpenAI({ baseURL, apiKey })` (chat-completions API, which Token Factory serves).
* Every model sits behind `process.env.LLM_MODEL_<NAME> ?? "<nemotron id>"`, so rollback is one env var.
* Anthropic SDK calls are reported for a hand port.

Checks: `node --check` for .js/.mjs/.cjs when Node is installed (syntax only, nothing runs), and a
bracket/string balance check for every file (TypeScript isn't compiled here).
"""
from __future__ import annotations

import codecs
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import config
from .jsscan import _match, _regex_end, _skip_comment, _skip_string, mask, parse_object

ENV_BASE = "TOKEN_FACTORY_BASE_URL"
ENV_KEY = "NEBIUS_API_KEY"
BASE = f'process.env.{ENV_BASE} ?? "{config.DEFAULT_BASE_URL}"'
DROP_CLIENT = {"apiKey", "baseURL", "organization", "project", "endpoint", "deployment", "apiVersion"}


def balanced(src: str) -> str | None:
    """None if brackets, strings and comments all close; otherwise what's wrong."""
    stack, i = [], 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            j = _skip_string(src, i)
            if j >= len(src) and not src.rstrip().endswith(c):
                return f"unterminated string starting at offset {i}"
            i = j
            continue
        if src.startswith("//", i) or src.startswith("/*", i):
            i = _skip_comment(src, i)
            continue
        if c == "/" and (e := _regex_end(src, i)):
            i = e
            continue
        if c in pairs:
            stack.append((pairs[c], i))
        elif c in ")]}":
            if not stack or stack[-1][0] != c:
                return f"unexpected {c!r} at line {src.count(chr(10), 0, i) + 1}"
            stack.pop()
        i += 1
    return f"unclosed {stack[-1][0]!r}" if stack else None


def _insert_point(src: str) -> int:
    """Just after the last top-level import (or the 'use client' / 'use server' directive)."""
    m_src = mask(src)
    end = 0
    for m in re.finditer(r"^[ \t]*import\b", m_src, re.M):
        semi = re.search(r"""from\s*["'][^"'\n]*["']\s*;?|^[ \t]*import\s*["'][^"'\n]*["']\s*;?""", src[m.start():], re.M)
        if semi:
            end = max(end, m.start() + semi.end())
    if end == 0:
        d = re.match(r"""\s*(["'])use (client|server)\1;?""", src)
        end = d.end() if d else 0
    nl = src.find("\n", end)
    return len(src) if nl < 0 else nl + 1


def _env_name(site: dict, used: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", (site.get("function") or Path(site["file"]).stem).upper()).strip("_") or "CALL"
    name, i = f"LLM_MODEL_{base}", 2
    while name in used:
        name, i = f"LLM_MODEL_{base}_{i}", i + 1
    used.add(name)
    return name


def _client_call(src: str, client: dict) -> str:
    js = client["js"]
    args = js.get("args", "").strip()
    if args and not args.startswith("{"):
        # new OpenAI(config): keep the caller's options, ours win
        return f"new OpenAI({{ ...({args}), baseURL: {BASE}, apiKey: process.env.{ENV_KEY} }})"
    parts = [f"baseURL: {BASE}", f"apiKey: process.env.{ENV_KEY}"]
    for k, (a, b) in js["props"].items():
        if k in DROP_CLIENT:
            continue
        text = src[a:b]
        parts.append(f"...{text}" if k.startswith("...") else (k if text == k else f"{k}: {text}"))
    return f"new OpenAI({{ {', '.join(parts)} }})"


def apply_js(out_dir: Path, sites: list[dict], clients: list[dict], used_env: set[str]) -> dict:
    """Edit the copies in `out_dir`. Returns changed files, routes, manual follow-ups and checks."""
    by_file: dict[str, list[dict]] = {}
    manual, routes = [], []
    migrating = [s for s in sites if s.get("status") == "done" and s["decision"]["action"] == "migrate"]
    for s in migrating:
        site = s["site"]
        model_id = config.MODELS[s["decision"]["model"]].id
        if site["sdk"] == "js-anthropic":
            manual.append({"file": site["file"], "line": site["line"], "sdk": site["sdk"], "model": model_id,
                           "reason": "Anthropic SDK call: switch it to the openai package pointed at Token Factory by hand"})
        elif not site["js"].get("model_span"):
            manual.append({"file": site["file"], "line": site["line"], "sdk": site["sdk"], "model": model_id,
                           "reason": "no `model` in the call's options; set it to the Nemotron id by hand"})
        else:
            by_file.setdefault(site["file"], []).append(s)
    openai_sites = [s for s in sites if s["site"]["sdk"] in ("js-openai", "js-openai-responses")]
    js_clients = [c for c in clients if c.get("js") and c["sdk"] == "openai"]
    every_openai_moves = bool(openai_sites) and all(
        s.get("status") == "done" and s["decision"]["action"] == "migrate" for s in openai_sites)
    rewrite_clients = every_openai_moves and bool(js_clients) and not any(c["sdk"] == "openai-azure" for c in clients if c.get("js"))
    files = set(by_file) | ({c["file"] for c in js_clients} if rewrite_clients else set())
    changed, checks = [], []
    for rel in sorted(files):
        path = out_dir / rel
        raw = path.read_bytes()
        bom = raw.startswith(codecs.BOM_UTF8)
        src = raw.decode("utf-8-sig")
        edits: list[tuple[int, int, str]] = []
        need_client = need_sdk = False
        if rewrite_clients:
            for c in js_clients:
                if c["file"] == rel:
                    edits.append((c["js"]["start"], c["js"]["end"], _client_call(src, c)))
        for s in by_file.get(rel, []):
            site, dec = s["site"], s["decision"]
            js = site["js"]
            model_id = config.MODELS[dec["model"]].id
            env = _env_name(site, used_env)
            a, b = js["model_span"]
            hint = json.dumps(dec.get("hint", "")) if dec.get("hint") else None
            props = js["props"]
            if site["sdk"] == "js-ai-sdk":
                edits.append((a, b, f'tokenFactory.chat(process.env.{env} ?? "{model_id}")'))
                need_sdk = True
                if hint:
                    sys_key = "system" if "system" in props else ("instructions" if "instructions" in props else None)
                    if sys_key:
                        sa, sb = props[sys_key]
                        edits.append((sa, sb, f'{hint} + "\\n\\n" + ({src[sa:sb]})'))
                    else:
                        edits.append((js["obj"] + 1, js["obj"] + 1, f" system: {hint},"))
            else:
                edits.append((a, b, f'process.env.{env} ?? "{model_id}"'))
                if not rewrite_clients and js.get("receiver") and js.get("receiver_start") is not None:
                    edits.append((js["receiver_start"], js["start"], "tokenFactory"))
                    need_client = True
                key = "input" if site["sdk"] == "js-openai-responses" else "messages"
                if hint and key in props:
                    ma, mb = props[key]
                    if src[ma:mb].lstrip().startswith("["):
                        at = src.index("[", ma) + 1
                        edits.append((at, at, f'{{ role: "system", content: {hint} }}, '))
                    else:
                        edits.append((ma, mb, f'[{{ role: "system", content: {hint} }}, ...({src[ma:mb]})]'))
            routes.append({"file": rel, "line": site["line"], "function": site["function"], "env": env,
                           "from": site.get("model") or site.get("model_expr"), "to": model_id,
                           "hint": dec.get("hint", ""), "pass_rate": dec["pass_rate"], "env_value": model_id})
        header = ""
        if need_client:
            if not re.search(r"""(?:import\s+(?:OpenAI|\{[^}]*\bOpenAI\b[^}]*\})\s+from\s*["']openai["'])|require\(\s*["']openai["']\s*\)""", src):
                header += 'import OpenAI from "openai";\n'
            header += f"\n// Switchyard: calls migrated to NVIDIA Nemotron on Nebius Token Factory\n" \
                      f"const tokenFactory = new OpenAI({{ baseURL: {BASE}, apiKey: process.env.{ENV_KEY} }});\n"
        if need_sdk:
            if not re.search(r"""\bcreateOpenAI\b[^;\n]*from\s*["']@ai-sdk/openai["']""", src):
                header += 'import { createOpenAI } from "@ai-sdk/openai";\n'
            header += f"\n// Switchyard: calls migrated to NVIDIA Nemotron on Nebius Token Factory\n" \
                      f"const tokenFactory = createOpenAI({{ baseURL: {BASE}, apiKey: process.env.{ENV_KEY} }});\n"
        if header:
            at = _insert_point(src)
            edits.append((at, at, header))
        if not edits:
            continue
        new = src
        for a, b, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            new = new[:a] + text + new[b:]
        path.write_bytes((codecs.BOM_UTF8 if bom else b"") + new.encode("utf-8"))
        changed.append(rel)
        problem = balanced(new)
        check = {"file": rel, "ok": problem is None, "how": "bracket/string balance"}
        if problem:
            check["error"] = problem
        elif rel.endswith((".js", ".mjs", ".cjs")) and shutil.which("node"):
            r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
            check.update(ok=r.returncode == 0, how="node --check")
            if r.returncode:
                check["error"] = (r.stderr or r.stdout)[-300:]
        checks.append(check)
    return {"changed": changed, "routes": routes, "manual": manual, "checks": checks,
            "mode": "rewrite-clients" if rewrite_clients else "dedicated-client"}
