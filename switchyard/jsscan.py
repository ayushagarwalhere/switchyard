"""LLM calls in JavaScript / TypeScript (Next.js, Node, Vercel AI SDK), found by reading the source.

No JS engine and no npm: a small tokenizer that understands strings, template literals and
comments is enough to find calls, pull out the argument object, and turn prompts into the same
`{{var}}` templates the Python scanner produces. Nothing from the scanned repo is executed.

Supported:
  openai:        client.chat.completions.create({...}), .parse, client.responses.create({...})
  anthropic:     client.messages.create({...})       (tested and reported; ported by hand)
  Vercel AI SDK: generateText / streamText / generateObject / streamObject({ model: openai("gpt-4o"), ... })
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
JS_SKIP = {"node_modules", ".next", "dist", "build", "out", ".git", ".vercel", ".turbo", "coverage", ".venv", "venv",
           "public", "vendor", "static"}
JS_HINT = re.compile(r"openai|anthropic|generateText|streamText|generateObject|streamObject|completions", re.I)


# -- tokenizer helpers -----------------------------------------------------------------------------

def _skip_string(s: str, i: int) -> int:
    """`i` is at a quote; return the index just past the closing quote (template literals nest ${})."""
    q = s[i]
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if q == "`" and c == "$" and i + 1 < len(s) and s[i + 1] == "{":
            i = _match(s, i + 1) + 1
            continue
        if c == q:
            return i + 1
        if c == "\n" and q != "`":
            return i + 1
        i += 1
    return len(s)


REGEX_PREV = set("(,=:[!&|?{};+-*%~^")      # not < or >: in JSX, `</div>` isn't a regex


def _regex_end(s: str, i: int) -> int | None:
    """If a regex literal starts at `i` (a '/' where an expression can begin), the index past it."""
    if s[i] != "/" or s.startswith("//", i) or s.startswith("/*", i):
        return None
    j = i - 1
    while j >= 0 and s[j] in " \t\r\n":
        j -= 1
    prev = s[j] if j >= 0 else ""
    if prev and prev not in REGEX_PREV and not s[max(0, j - 5):j + 1].endswith(("return", "typeof", "case")):
        return None
    k, in_class = i + 1, False
    while k < len(s) and s[k] != "\n":
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            k += 1
            while k < len(s) and (s[k].isalpha()):
                k += 1   # flags
            return k
        k += 1
    return None


def _skip_comment(s: str, i: int) -> int:
    if s.startswith("//", i):
        j = s.find("\n", i)
        return len(s) if j < 0 else j
    j = s.find("*/", i + 2)
    return len(s) if j < 0 else j + 2


def _match(s: str, i: int) -> int:
    """Index of the bracket closing the one at `i` (skips strings and comments)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[s[i]]]
    i += 1
    while i < len(s) and stack:
        c = s[i]
        if c in "'\"`":
            i = _skip_string(s, i)
            continue
        if s.startswith("//", i) or s.startswith("/*", i):
            i = _skip_comment(s, i)
            continue
        if c == "/" and (e := _regex_end(s, i)):
            i = e
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i
        i += 1
    return len(s) - 1


def mask(s: str) -> str:
    """Same length as `s`, with string and comment contents blanked (so regexes only see code)."""
    out = list(s)
    i = 0
    while i < len(s):
        c = s[i]
        if c in "'\"`":
            j = _skip_string(s, i)
            for k in range(i + 1, max(i + 1, j - 1)):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if s.startswith("//", i) or s.startswith("/*", i):
            j = _skip_comment(s, i)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "/" and (e := _regex_end(s, i)):
            for k in range(i + 1, e - 1):
                out[k] = " "
            i = e
            continue
        i += 1
    return "".join(out)


def _split_top(s: str, start: int, end: int, sep: str = ",") -> list[tuple[int, int]]:
    """Top-level pieces of s[start:end] separated by `sep` (as (start, end) spans, trimmed)."""
    parts, i, last = [], start, start
    while i < end:
        c = s[i]
        if c in "'\"`":
            i = _skip_string(s, i)
            continue
        if s.startswith("//", i) or s.startswith("/*", i):
            i = _skip_comment(s, i)
            continue
        if c == "/" and (e := _regex_end(s, i)):
            i = e
            continue
        if c in "([{":
            i = _match(s, i) + 1
            continue
        if s.startswith(sep, i):
            parts.append((last, i))
            i += len(sep)
            last = i
            continue
        i += 1
    parts.append((last, end))
    out = []
    for a, b in parts:
        while a < b and s[a].isspace():
            a += 1
        while b > a and s[b - 1].isspace():
            b -= 1
        if a < b:
            out.append((a, b))
    return out


def parse_object(s: str, open_i: int) -> dict[str, tuple[int, int]]:
    """{ key: value, shorthand, ...spread } at `open_i` -> {key: (value_start, value_end)}; spreads as "...N"."""
    close = _match(s, open_i)
    props: dict[str, tuple[int, int]] = {}
    for n, (a, b) in enumerate(_split_top(s, open_i + 1, close)):
        piece = s[a:b]
        if piece.startswith("..."):
            props[f"...{n}"] = (a + 3, b)
            continue
        m = re.match(r"""\s*(?:["']([^"']+)["']|([A-Za-z_$][\w$]*))\s*:""", piece)
        if m:
            key = m.group(1) or m.group(2)
            va = a + m.end()
            while va < b and s[va].isspace():
                va += 1
            props[key] = (va, b)
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", piece):
            props[piece] = (a, b)          # shorthand { model, messages }
    return props


# -- values -> templates ---------------------------------------------------------------------------

def _slot(expr: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", expr.strip()).strip("_")
    return "{{" + (name[:40] or "value") + "}}"


class _Values:
    def __init__(self, src: str):
        self.src = src
        self.consts: dict[str, list[tuple[int, int]]] = {}
        masked = mask(src)
        for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*", masked):
            a = m.end()
            # the value runs to the end of the statement (a top-level ';' or a newline at depth 0)
            i = a
            while i < len(src):
                c = src[i]
                if c in "'\"`":
                    i = _skip_string(src, i)
                    continue
                if c in "([{":
                    i = _match(src, i) + 1
                    continue
                if c == ";" or (c == "\n" and not src[a:i].rstrip().endswith(("+", "=", ",", "(", "?", ":"))):
                    break
                i += 1
            self.consts.setdefault(m.group(1), []).append((a, i))

    def lookup(self, name: str, at: int) -> tuple[int, int]:
        """The nearest definition of `name` before offset `at` (two functions can each have `const prompt`)."""
        defs = self.consts[name]
        before = [d for d in defs if d[0] < at]
        return before[-1] if before else defs[0]

    def text(self, a: int, b: int, depth: int = 0) -> str | None:
        """A string expression as a template with {{slots}}, or None if it isn't string-like."""
        v = self.src[a:b].strip()
        if not v or depth > 4:
            return None
        if v.endswith(("as const", ".trim()")):
            v = re.sub(r"\s*(as const|\.trim\(\))$", "", v)
        if v[0] in "'\"" and _skip_string(v, 0) == len(v):
            return re.sub(r"\\(.)", lambda e: {"n": "\n", "t": "\t", "r": ""}.get(e.group(1), e.group(1)), v[1:-1])
        if v[0] == "`" and _skip_string(v, 0) == len(v):
            out, i, body = [], 1, v
            while i < len(body) - 1:
                if body.startswith("${", i):
                    j = _match(body, i + 1)
                    out.append(_slot(body[i + 2:j]))
                    i = j + 1
                    continue
                if body[i] == "\\" and i + 1 < len(body) - 1:
                    out.append({"n": "\n", "t": "\t", "`": "`", "$": "$"}.get(body[i + 1], body[i + 1]))
                    i += 2
                    continue
                out.append(body[i])
                i += 1
            return "".join(out)
        parts = _split_top(self.src, a, b, "+")
        if len(parts) > 1:
            texts = [self.text(x, y, depth + 1) or _slot(self.src[x:y]) for x, y in parts]
            return "".join(texts)
        if re.fullmatch(r"[A-Za-z_$][\w$]*", v) and v in self.consts:
            ca, cb = self.lookup(v, a)
            t = self.text(ca, cb, depth + 1)
            return t if t is not None else _slot(v)
        return None

    def content(self, a: int, b: int) -> str:
        t = self.text(a, b)
        return t if t is not None else _slot(self.src[a:b])

    def messages(self, a: int, b: int, depth: int = 0) -> list[dict] | None:
        v = self.src[a:b].strip()
        if not v or depth > 3:
            return None
        if re.fullmatch(r"[A-Za-z_$][\w$]*", v) and v in self.consts:
            return self.messages(*self.lookup(v, a), depth=depth + 1)
        start = self.src.find("[", a, b)
        if not v.startswith("[") or start < 0:
            return None
        end = _match(self.src, start)
        msgs = []
        for x, y in _split_top(self.src, start + 1, end):
            el = self.src[x:y]
            if el.startswith("{"):
                props = parse_object(self.src, x)
                role = self.text(*props["role"]) if "role" in props else "user"
                if "content" in props:
                    msgs.append({"role": role or "user", "content": self.content(*props["content"])})
            elif el.startswith("..."):
                inner = self.messages(x + 3, y, depth + 1)
                msgs += inner if inner is not None else [{"role": "history", "content": _slot(el[3:])}]
            else:
                msgs.append({"role": "history" if re.fullmatch(r"[\w$.]+", el) else "user", "content": _slot(el)})
        return msgs


# -- scanning --------------------------------------------------------------------------------------

CALLS = [
    (re.compile(r"\.chat\.completions\.(create|parse)\s*\("), "js-openai"),
    (re.compile(r"\.responses\.(create|parse)\s*\("), "js-openai-responses"),
    (re.compile(r"\.messages\.create\s*\("), "js-anthropic"),
    (re.compile(r"\b(generateText|streamText|generateObject|streamObject)\s*\("), "js-ai-sdk"),
]
CTOR = re.compile(r"\bnew\s+(OpenAI|AzureOpenAI|Anthropic)\s*\(")
FUNC_PATTERNS = [
    re.compile(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b"),
    re.compile(r"^\s*(?:public\s+|private\s+|static\s+|async\s+)*([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::[^{]+)?\{", re.M),
    # object-property functions: onCreateDocument: async ({ ... }) => { ... }
    re.compile(r"\b([A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?(?:\([^()]*(?:\([^()]*\)[^()]*)*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"),
    re.compile(r"\b([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?function\b"),
]
AI_SDK_MODEL = re.compile(r"""^([A-Za-z_$][\w$]*)(?:\.(chat|responses|languageModel))?\(\s*(["'`])([^"'`]+)\3""")


def _line(src: str, i: int) -> int:
    return src.count("\n", 0, i) + 1


def function_spans(masked: str) -> list[tuple[int, int, str]]:
    """(body start, body end, name) for every named function-like body, computed once per file."""
    spans = []
    for pat in FUNC_PATTERNS:
        for m in pat.finditer(masked):
            name = m.group(1)
            if name in ("if", "for", "while", "switch", "catch", "return", "function"):
                continue
            if m.group(0).rstrip().endswith("=>"):
                j = m.end()
                while j < len(masked) and masked[j].isspace():
                    j += 1
                if j < len(masked) and masked[j] == "{":
                    spans.append((m.start(), _match(masked, j), name))
                else:   # expression-bodied arrow: `x => call()` runs to the end of its line
                    eol = masked.find("\n", m.end())
                    spans.append((m.start(), len(masked) if eol < 0 else eol, name))
                continue
            # declarations and methods: skip the parameter list (it can hold braces), then the body
            paren = masked.find("(", m.start() + len(name))
            if paren < 0:
                continue
            brace = masked.find("{", _match(masked, paren))
            if brace >= 0:
                spans.append((m.start(), _match(masked, brace), name))
    return spans


def _enclosing(spans: list[tuple[int, int, str]], at: int) -> str | None:
    """The innermost span containing `at`."""
    inside = [s for s in spans if s[0] < at <= s[1]]
    return max(inside, key=lambda s: s[0])[2] if inside else None


def _model_of(sdk: str, src: str, props: dict, vals: _Values) -> tuple[str | None, str | None, tuple | None]:
    """(model, provider, span of the model *string* or call to rewrite)."""
    if "model" not in props:
        return None, None, None
    a, b = props["model"]
    raw = src[a:b].strip()
    if sdk == "js-ai-sdk":
        m = AI_SDK_MODEL.match(raw)
        if m:
            return m.group(4), m.group(1), (a, b)
        t = vals.text(a, b)                      # "openai/gpt-4o" through the AI Gateway
        if t and "/" in t:
            return t.split("/", 1)[1], t.split("/", 1)[0], (a, b)
        return None, None, (a, b)
    t = vals.text(a, b)
    return (t if t and "{{" not in t else None), None, (a, b)


def scan_js_file(path: Path, root: Path) -> tuple[list[dict], list[dict]]:
    try:
        src = path.read_bytes().decode("utf-8-sig", errors="replace")   # keeps CRLF, so offsets match the file
    except OSError:
        return [], []
    if not JS_HINT.search(src):
        return [], []
    rel = path.relative_to(root).as_posix()
    if len(src) > 400_000 or max((len(x) for x in src.splitlines()), default=0) > 3000:
        return [], []           # a bundled or minified file, not source code
    masked = mask(src)
    spans = function_spans(masked)
    vals = _Values(src)
    anthropic_file = re.search(r"anthropic", src, re.I) is not None
    calls, clients = [], []
    for m in CTOR.finditer(masked):
        open_i = m.end() - 1
        close = _match(src, open_i)
        brace = src.find("{", open_i, close)
        props = parse_object(src, brace) if brace >= 0 and not src[open_i + 1:brace].strip() else {}
        clients.append({"file": rel, "line": _line(src, m.start()), "end_line": _line(src, close), "col": 0, "end_col": 0,
                        "ctor": m.group(1), "sdk": "openai-azure" if m.group(1) == "AzureOpenAI" else
                        ("anthropic" if m.group(1) == "Anthropic" else "openai"),
                        "source": src[m.start():close + 1], "kwargs": list(props),
                        "js": {"start": m.start(), "end": close + 1, "open": open_i, "props": props,
                               "args": src[open_i + 1:close]}})
    for pat, sdk in CALLS:
        for m in pat.finditer(masked):
            if sdk == "js-anthropic" and not anthropic_file:
                continue
            open_i = m.end() - 1
            close = _match(src, open_i)
            j = open_i + 1
            while j < close and src[j].isspace():
                j += 1
            if j >= close or src[j] != "{":
                continue                     # arguments built elsewhere: nothing to template
            props = parse_object(src, j)
            notes: list[str] = []
            model, provider, model_span = _model_of(sdk, src, props, vals)
            if sdk == "js-ai-sdk":
                msgs = []
                for key in ("system", "instructions"):      # AI SDK 6 calls it `instructions`
                    if key in props:
                        msgs.append({"role": "system", "content": vals.content(*props[key])})
                if "messages" in props:
                    resolved = vals.messages(*props["messages"])
                    if resolved is None:
                        notes.append("messages are built dynamically; tests fill them as a single user turn")
                    msgs += resolved or [{"role": "user", "content": _slot(src[slice(*props["messages"])])}]
                if "prompt" in props:
                    msgs.append({"role": "user", "content": vals.content(*props["prompt"])})
            elif sdk == "js-openai-responses":
                msgs = []
                if "instructions" in props:
                    msgs.append({"role": "system", "content": vals.content(*props["instructions"])})
                if "input" in props:
                    msgs += vals.messages(*props["input"]) or [{"role": "user", "content": vals.content(*props["input"])}]
            else:
                msgs = vals.messages(*props["messages"]) if "messages" in props else None
                if sdk == "js-anthropic" and "system" in props:
                    msgs = [{"role": "system", "content": vals.content(*props["system"])}] + (msgs or [])
            if not msgs:
                expr = src[slice(*props["messages"])] if "messages" in props else "messages"
                msgs = [{"role": "user", "content": _slot(expr)}]
                notes.append("messages are built dynamically; tests fill them as a single user turn")
            params: dict = {}
            for key, name in (("temperature", "temperature"), ("max_tokens", "max_tokens"), ("maxTokens", "max_tokens"),
                              ("max_completion_tokens", "max_tokens"), ("maxOutputTokens", "max_tokens"),
                              ("max_output_tokens", "max_tokens"), ("top_p", "top_p"), ("seed", "seed")):
                if key in props:
                    raw = src[slice(*props[key])].strip()
                    try:
                        params[name] = float(raw) if "." in raw else int(raw)
                    except ValueError:
                        params[name] = raw
            if "response_format" in props and "json" in src[slice(*props["response_format"])]:
                params["response_format"] = {"type": "json_object"}
            if m.group(0).startswith(("generateObject", "streamObject")) or "schema" in props:
                params["response_format"] = {"type": "json_object"}
                notes.append("structured output via a schema object; tested as JSON")
            if m.group(0).startswith(("streamText", "streamObject")) or "stream" in props:
                params["stream"] = True
            if any(k.startswith("...") for k in props):
                notes.append("some settings come from a spread object")
            variables = sorted({v for msg in msgs for v in re.findall(r"\{\{([^{}]+)\}\}", msg["content"])})
            line = _line(src, m.start())
            snippet = src[m.start():close + 1]
            # receiver: `openai` in openai.chat.completions.create / `this.client` in this.client.messages.create
            recv = re.search(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*$", masked[:m.start()])
            calls.append({
                "id": hashlib.sha1(f"{rel}:{line}:{snippet}".encode()).hexdigest()[:8],
                "file": rel, "line": line, "end_line": _line(src, close), "sdk": sdk,
                "method": m.group(0).rstrip("( ").lstrip("."), "function": _enclosing(spans, m.start()),
                "model": model, "model_expr": src[slice(*props["model"])].strip() if "model" in props else None,
                "model_span": None, "messages": msgs, "variables": variables, "params": params,
                "snippet": snippet[:2000], "context": src[max(0, m.start() - 1500):close + 200][-3000:],
                "docstring": None, "notes": notes, "arg_spans": {}, "call_span": None,
                "receiver_span": None, "awaited": bool(re.search(r"\bawait\s+[\w$.]*$", masked[:m.start()])),
                "skip": None, "lang": "js",
                "js": {"start": m.start(), "end": close + 1, "obj": j, "props": props, "model_span": model_span,
                       "provider": provider, "receiver": recv.group(1) if recv and sdk != "js-ai-sdk" else None,
                       "receiver_start": recv.start(1) if recv else None},
            })
    return calls, clients


def iter_js_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in JS_SKIP and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(JS_EXT) and not fn.endswith((".d.ts", ".min.js")):
                yield Path(dirpath) / fn
