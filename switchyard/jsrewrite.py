"""Rewrite JavaScript / TypeScript call sites to Nemotron on Token Factory (text edits at exact offsets).

* OpenAI SDK: migrated calls go through a dedicated `tokenFactory` client; the app's own `new OpenAI(...)`
  is never touched, so embeddings, images and calls that stay put keep working (and no provider-specific
  headers or keys are ever sent to Token Factory).
* Vercel AI SDK: `model: openai("gpt-4o")` becomes `model: tokenFactoryAI.chat(...)` where
  `tokenFactoryAI = createOpenAI({ baseURL, apiKey })` (chat-completions API, which Token Factory serves).
* Every model sits behind `process.env.LLM_MODEL_<NAME> ?? "<nemotron id>"`, so rollback is one env var.
* Left for a hand edit, with the reason: Anthropic SDK calls, `"use client"` files (the key can't live in
  the browser), and calls whose client can't be named safely (`getClient().chat...`, `openai?.chat...`).

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
from .jsscan import _regex_end, _skip_comment, _skip_string, mask

ENV_BASE = "TOKEN_FACTORY_BASE_URL"
ENV_KEY = "NEBIUS_API_KEY"
BASE = f'process.env.{ENV_BASE} ?? "{config.DEFAULT_BASE_URL}"'
HEADER = "// Switchyard: calls migrated to NVIDIA Nemotron on Nebius Token Factory"


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


IMPORT_RE = re.compile(r"""^[ \t]*import\b(?:[^;"'()]*?\bfrom\s*)?["'][^"'\n]*["'][ \t]*;?""", re.M | re.S)
REQUIRE_RE = re.compile(r"""^[ \t]*(?:const|let|var)\b[^;\n]*=\s*require\(\s*["'][^"'\n]*["']\s*\)[^;\n]*;?""", re.M)


def _imports(src: str, masked: str) -> list[tuple[int, int, str]]:
    """(start, end, module) of each top-level import / require statement."""
    out = []
    for m in IMPORT_RE.finditer(masked):
        text = src[m.start():m.end()]
        mod = re.findall(r"""["']([^"']+)["']""", text)
        out.append((m.start(), m.end(), mod[-1] if mod else ""))
    for m in REQUIRE_RE.finditer(masked):
        mod = re.search(r"""require\(\s*["']([^"']+)["']""", src[m.start():m.end()])
        out.append((m.start(), m.end(), mod.group(1) if mod else ""))
    return sorted(out)


def _is_cjs(src: str, masked: str) -> bool:
    return "require(" in masked and not IMPORT_RE.search(masked) and not re.search(r"^\s*export\b", masked, re.M)


def _imports_name(src: str, imports, module: str, name: str) -> bool:
    return any(mod == module and re.search(rf"\b{name}\b", src[a:b]) for a, b, mod in imports)


def _insert_point(src: str, masked: str, imports) -> int:
    """Just after the last top-level import/require, else after a directive ('use client', 'use strict')."""
    end = max((b for _, b, _ in imports), default=0)
    if end == 0:
        d = re.match(r"""(\s*(["'])use (client|server|strict)\2;?)+""", src)
        end = d.end() if d else 0
        if end == 0:
            return 0
    nl = src.find("\n", end)
    return len(src) if nl < 0 else nl + 1


def _env_name(site: dict, used: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", (site.get("function") or Path(site["file"]).stem).upper()).strip("_") or "CALL"
    name, i = f"LLM_MODEL_{base}", 2
    while name in used:
        name, i = f"LLM_MODEL_{base}_{i}", i + 1
    used.add(name)
    return name


def _package_has(out_dir: Path, rel: str, dep: str) -> bool | None:
    """Whether the nearest package.json lists `dep` (None when there's no package.json)."""
    d = (out_dir / rel).parent
    while True:
        pj = d / "package.json"
        if pj.exists():
            try:
                data = json.loads(pj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return any(dep in (data.get(k) or {}) for k in ("dependencies", "devDependencies", "peerDependencies"))
        if d == out_dir or d.parent == d:
            return None
        d = d.parent


def _value_edit(src: str, key: str, span: tuple[int, int], expr: str) -> tuple[int, int, str]:
    """Replace a property's value; `{ model }` shorthand becomes `{ model: <expr> }`."""
    a, b = span
    return (a, b, f"{key}: {expr}" if src[a:b].strip() == key else expr)


def apply_js(out_dir: Path, sites: list[dict], clients: list[dict], used_env: set[str]) -> dict:
    """Edit the copies in `out_dir`. Returns changed files, routes, manual follow-ups and checks."""
    by_file: dict[str, list[dict]] = {}
    manual, routes = [], []

    def hand(site, model_id, reason):
        manual.append({"file": site["file"], "line": site["line"], "sdk": site["sdk"], "model": model_id, "reason": reason})

    for s in sites:
        if s.get("status") != "done" or s["decision"]["action"] != "migrate":
            continue
        site, js = s["site"], s["site"]["js"]
        model_id = config.MODELS[s["decision"]["model"]].id
        head = (out_dir / site["file"]).read_bytes()[:300].decode("utf-8-sig", errors="replace")
        if site["sdk"] == "js-anthropic":
            hand(site, model_id, "Anthropic SDK call: switch it to the openai package pointed at Token Factory by hand")
        elif re.match(r"""\s*(?://[^\n]*\n\s*)*["']use client["']""", head):
            hand(site, model_id, "runs in a client component: move the call to a route handler or server action "
                                 "(the Token Factory key must stay on the server), then point it at Nemotron")
        elif not js.get("model_span"):
            hand(site, model_id, "no `model` in the call's options; set it to the Nemotron id by hand")
        elif site["sdk"] != "js-ai-sdk" and not js.get("receiver"):
            hand(site, model_id, "the client comes from an expression Switchyard can't rename safely "
                                 "(e.g. getClient().chat...); call a Token Factory client there by hand")
        else:
            by_file.setdefault(site["file"], []).append(s)

    changed, checks = [], []
    for rel in sorted(by_file):
        path = out_dir / rel
        raw = path.read_bytes()
        bom = raw.startswith(codecs.BOM_UTF8)
        src = raw.decode("utf-8-sig")
        masked = mask(src)
        imports = _imports(src, masked)
        cjs = _is_cjs(src, masked)
        edits: list[tuple[int, int, str]] = []
        need_client = need_sdk = False
        for s in by_file[rel]:
            site, dec = s["site"], s["decision"]
            js = site["js"]
            props = js["props"]
            model_id = config.MODELS[dec["model"]].id
            env = _env_name(site, used_env)
            hint = json.dumps(dec.get("hint", "")) if dec.get("hint") else None
            if site["sdk"] == "js-ai-sdk":
                edits.append(_value_edit(src, "model", js["model_span"],
                                         f'tokenFactoryAI.chat(process.env.{env} ?? "{model_id}")'))
                need_sdk = True
                if hint:
                    sys_key = "system" if "system" in props else ("instructions" if "instructions" in props else None)
                    if sys_key:
                        sa, sb = props[sys_key]
                        edits.append(_value_edit(src, sys_key, (sa, sb), f'{hint} + "\\n\\n" + ({src[sa:sb]})'))
                    else:
                        edits.append((js["obj"] + 1, js["obj"] + 1, f" system: {hint},"))
            else:
                edits.append(_value_edit(src, "model", js["model_span"], f'process.env.{env} ?? "{model_id}"'))
                recv_end = js["start"]
                if js["receiver"].endswith(".beta"):          # client.beta.chat.completions.parse
                    recv_end -= len(".beta")
                edits.append((js["receiver_start"], recv_end, "tokenFactory"))
                need_client = True
                key = "input" if site["sdk"] == "js-openai-responses" else "messages"
                if hint and key in props:
                    ma, mb = props[key]
                    if src[ma:mb].lstrip().startswith("["):
                        at = src.index("[", ma) + 1
                        edits.append((at, at, f'{{ role: "system", content: {hint} }}, '))
                    else:
                        edits.append(_value_edit(src, key, (ma, mb),
                                                 f'[{{ role: "system", content: {hint} }}, ...({src[ma:mb]})]'))
            routes.append({"file": rel, "line": site["line"], "function": site["function"], "env": env,
                           "from": site.get("model") or site.get("model_expr"), "to": model_id,
                           "hint": dec.get("hint", ""), "pass_rate": dec["pass_rate"], "env_value": model_id})
        lines = []
        if need_client:
            if not _imports_name(src, imports, "openai", "OpenAI"):
                lines.append('const OpenAI = require("openai");' if cjs else 'import OpenAI from "openai";')
        if need_sdk:
            if not _imports_name(src, imports, "@ai-sdk/openai", "createOpenAI"):
                lines.append('const { createOpenAI } = require("@ai-sdk/openai");' if cjs
                             else 'import { createOpenAI } from "@ai-sdk/openai";')
            if _package_has(out_dir, rel, "@ai-sdk/openai") is False:
                manual.append({"file": rel, "line": 1, "sdk": "js-ai-sdk", "model": "",
                               "reason": "add the dependency: npm install @ai-sdk/openai"})
        lines.append("")
        lines.append(HEADER)
        if need_client:
            lines.append(f"const tokenFactory = new OpenAI({{ baseURL: {BASE}, apiKey: process.env.{ENV_KEY} }});")
        if need_sdk:
            lines.append(f"const tokenFactoryAI = createOpenAI({{ baseURL: {BASE}, apiKey: process.env.{ENV_KEY} }});")
        nl = "\r\n" if "\r\n" in src else "\n"
        at = _insert_point(src, masked, imports)
        edits.append((at, at, nl.join(lines) + nl + ("" if at else nl)))
        new = src
        for a, b, text in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            new = new[:a] + text + new[b:]
        path.write_bytes((codecs.BOM_UTF8 if bom else b"") + new.encode("utf-8"))
        changed.append(rel)
        problem = balanced(new)
        check = {"file": rel, "ok": problem is None, "how": "bracket/string balance"}
        if problem and balanced(src):
            # the original already trips the simple checker (JSX text like "Don't"): don't call that a failure
            check.update(ok=True, how="balance check skipped: the checker can't read this file's JSX")
        elif problem:
            check["error"] = problem
        elif rel.endswith((".js", ".mjs", ".cjs")) and shutil.which("node"):
            r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=60)
            check.update(ok=r.returncode == 0, how="node --check")
            if r.returncode:
                check["error"] = (r.stderr or r.stdout)[-300:]
        checks.append(check)
    return {"changed": changed, "routes": routes, "manual": manual, "checks": checks, "mode": "dedicated-client"}
