"""Find every LLM call in a Python codebase and turn it into a testable template.

Pure static analysis (Python `ast`), no code from the scanned repo is executed.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "site-packages", "build", "dist", ".tox", ".mypy_cache"}

# Attribute chains that are LLM calls, and which SDK they belong to.
CALL_PATTERNS: list[tuple[tuple[str, ...], str]] = [  # longest first: receiver = whatever precedes the match
    (("beta", "chat", "completions", "parse"), "openai"),
    (("chat", "completions", "create"), "openai"),
    (("chat", "completions", "parse"), "openai"),
    (("ChatCompletion", "create"), "openai-legacy"),
    (("responses", "create"), "openai-responses"),
    (("responses", "parse"), "openai-responses"),
    (("messages", "create"), "anthropic"),
]
# LangChain chat models: the model is fixed where the object is built; prompts arrive via
# `prompt | llm` chains or `llm.invoke(...)`, which the scanner follows by name.
LANGCHAIN_CTORS = {"ChatOpenAI": "langchain-openai", "AzureChatOpenAI": "langchain-openai",
                   "ChatAnthropic": "langchain-anthropic", "init_chat_model": "langchain-init"}
LC_ROLES = {"human": "user", "user": "user", "ai": "assistant", "assistant": "assistant",
            "system": "system", "placeholder": "history"}
LC_PROMPT_TAKERS = {"LLMChain", "create_stuff_documents_chain", "load_summarize_chain", "create_extraction_chain"}
LC_INVOKE = {"invoke", "ainvoke", "stream", "astream", "batch", "abatch", "predict"}
CLIENT_CTORS = {"OpenAI": "openai", "AsyncOpenAI": "openai", "AzureOpenAI": "openai-azure",
                "AsyncAzureOpenAI": "openai-azure", "Anthropic": "anthropic", "AsyncAnthropic": "anthropic"}
LLM_HINT = re.compile(r"openai|anthropic|litellm|completions|messages\.create|responses\.(?:create|parse)|"
                      r"ChatOpenAI|ChatAnthropic|init_chat_model|claude", re.I)
PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


@dataclass
class ClientSite:
    file: str
    line: int
    end_line: int
    col: int
    end_col: int
    ctor: str
    sdk: str
    source: str
    kwargs: list = field(default_factory=list)


@dataclass
class CallSite:
    id: str
    file: str
    line: int
    end_line: int
    sdk: str
    method: str
    function: str | None
    model: str | None                 # resolved literal, if any
    model_expr: str | None            # source of the model= argument
    model_span: tuple[int, int, int, int] | None  # (line, col, end_line, end_col) of a literal we can rewrite
    messages: list[dict]              # [{"role":..., "content": "text with {{var}} slots"}]
    variables: list[str]
    params: dict                      # temperature, max_tokens, response_format, tools...
    snippet: str                      # the call's source
    context: str                      # enclosing function source (trimmed)
    docstring: str | None = None
    notes: list[str] = field(default_factory=list)
    arg_spans: dict = field(default_factory=dict)   # kwarg -> (line, col, end_line, end_col), col in utf-8 bytes
    call_span: tuple[int, int, int, int] | None = None
    receiver_span: tuple[int, int, int, int] | None = None  # e.g. `self.client` in self.client.chat.completions.create
    awaited: bool = False
    skip: str | None = None           # why this site can't be tested with single prompts (e.g. an agent loop)

    def to_dict(self) -> dict:
        return asdict(self)


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append("()")
    return list(reversed(parts))


def _litellm_names(nodes: list[ast.AST]) -> tuple[frozenset, frozenset]:
    """(functions imported from litellm, names the litellm module is bound to)."""
    funcs, mods = set(), set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "litellm":
            funcs.update(a.asname or a.name for a in node.names if a.name in ("completion", "acompletion"))
        elif isinstance(node, ast.Import):
            mods.update(a.asname or a.name for a in node.names if a.name == "litellm")
    return frozenset(funcs), frozenset(mods)


def _match_call(node: ast.Call, anthropic_names: frozenset = frozenset(),
                litellm: tuple[frozenset, frozenset] = (frozenset(), frozenset())) -> tuple[str, str] | None:
    chain = _attr_chain(node.func)
    if (len(chain) == 1 and chain[0] in litellm[0]) or \
            (len(chain) == 2 and chain[0] in litellm[1] and chain[1] in ("completion", "acompletion")):
        return "litellm", "completion"
    for pattern, sdk in CALL_PATTERNS:
        if tuple(chain[-len(pattern):]) == pattern:
            # `messages.create` is common in non-LLM code; require an anthropic-ish receiver
            if sdk == "anthropic" and not (_looks_anthropic(chain) or ".".join(chain[:-2]) in anthropic_names):
                continue
            return sdk, ".".join(pattern)
    return None


def _looks_anthropic(chain: list[str]) -> bool:
    head = ".".join(chain[:-2]).lower()
    return any(k in head for k in ("anthropic", "claude", "client", "llm", "beta", "haiku", "sonnet", "opus"))


def _anthropic_names(nodes: list[ast.AST]) -> frozenset:
    """Names assigned from Anthropic()/AsyncAnthropic() anywhere in the file (`haiku = Anthropic()`, `self.ai = ...`)."""
    names = set()
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
            ctor = _attr_chain(node.value.func)
            if ctor and ctor[-1] in ("Anthropic", "AsyncAnthropic", "AnthropicBedrock", "AnthropicVertex"):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(".".join(_attr_chain(tg)) for tg in targets if isinstance(tg, (ast.Name, ast.Attribute)))
    return frozenset(names)


class _Resolver:
    """Resolves simple names to string templates within a module/function scope."""

    def __init__(self, src: str, module: ast.Module):
        self.src = src
        self.module_consts = self._collect(module.body)

    def _collect(self, body: list[ast.stmt]) -> dict[str, ast.AST]:
        out: dict[str, ast.AST] = {}
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                out[stmt.targets[0].id] = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                out[stmt.target.id] = stmt.value
        return out

    def scope_for(self, func: ast.AST | None) -> dict[str, ast.AST]:
        scope = dict(self.module_consts)
        if func is not None:
            # a parameter shadows a module-level name: `def f(user_input)` must not resolve to the demo
            # code's `user_input = "..."` at the bottom of the file
            a = getattr(func, "args", None)
            if a is not None:
                for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                    if p is not None:
                        scope.pop(p.arg, None)
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    scope[node.targets[0].id] = node.value
        return scope

    def text(self, node: ast.AST | None, scope: dict[str, ast.AST], depth: int = 0) -> str | None:
        """Turn an expression into a template string with {{var}} slots, or None if opaque."""
        if node is None or depth > 6:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.replace("{{", "{ {").replace("}}", "} }")
        if isinstance(node, ast.JoinedStr):
            out = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    out.append("{{" + _slot_name(ast.get_source_segment(self.src, v.value) or "value") + "}}")
            return "".join(out)
        if isinstance(node, ast.Name):
            if node.id in scope and scope[node.id] is not node:
                resolved = self.text(scope[node.id], scope, depth + 1)
                if resolved is not None:
                    return resolved
            return "{{" + _slot_name(node.id) + "}}"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.text(node.left, scope, depth + 1), self.text(node.right, scope, depth + 1)
            if left is not None and right is not None:
                return left + right
        if isinstance(node, ast.Call):
            # "template".format(a=..., b=...)  or  TEMPLATE.format(...)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                base = self.text(node.func.value, scope, depth + 1)
                if base is not None:
                    def repl(m: re.Match) -> str:
                        return "{{" + _slot_name(m.group(1).split(":")[0].split("!")[0] or "value") + "}}"
                    return re.sub(r"(?<!\{)\{([^{}]*)\}(?!\})", repl, base)
            # textwrap.dedent("...") / dedent(...)
            name = _attr_chain(node.func)[-1] if _attr_chain(node.func) else ""
            if name in {"dedent", "strip", "cleandoc"} and node.args:
                inner = self.text(node.args[0], scope, depth + 1)
                if inner is not None:
                    import textwrap
                    return textwrap.dedent(inner).strip() if name != "strip" else inner.strip()
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"strip", "lstrip", "rstrip"}:
                inner = self.text(node.func.value, scope, depth + 1)
                if inner is not None:
                    return getattr(inner, node.func.attr)()
        if isinstance(node, ast.Attribute):
            return "{{" + _slot_name(ast.get_source_segment(self.src, node) or "value") + "}}"
        if isinstance(node, ast.Subscript):
            return "{{" + _slot_name(ast.get_source_segment(self.src, node) or "value") + "}}"
        expr = ast.get_source_segment(self.src, node)
        return "{{" + _slot_name(expr or "value") + "}}" if expr else None

    def messages(self, node: ast.AST | None, scope: dict[str, ast.AST], depth: int = 0) -> list[dict] | None:
        if node is None or depth > 4:
            return None
        if isinstance(node, ast.Name) and node.id in scope:
            return self.messages(scope[node.id], scope, depth + 1)
        # [system] + history,  list(base) + [...]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = []
            for side in (node.left, node.right):
                m = self.messages(side, scope, depth + 1)
                if m is None:
                    m = [{"role": "history", "content": "{{" + _slot_name(ast.get_source_segment(self.src, side) or "history") + "}}"}]
                parts += m
            return parts
        if isinstance(node, ast.Call) and _attr_chain(node.func)[-1:] in (["list"], ["tuple"]) and len(node.args) == 1:
            return self.messages(node.args[0], scope, depth + 1)
        if isinstance(node, (ast.List, ast.Tuple)):
            msgs = []
            for elt in node.elts:
                if isinstance(elt, ast.Name) and isinstance(scope.get(elt.id), ast.Dict):
                    elt = scope[elt.id]          # SYSTEM = {"role": "system", "content": "..."}
                lc = self._lc_message(elt, scope)
                if lc is not None:
                    msgs.append(lc)
                    continue
                if isinstance(elt, ast.Starred):
                    inner = self.messages(elt.value, scope, depth + 1)
                    if inner is not None:
                        msgs += inner
                        continue
                if isinstance(elt, ast.Dict):
                    d: dict = {}
                    for k, v in zip(elt.keys, elt.values):
                        if isinstance(k, ast.Constant) and k.value in ("role", "content"):
                            if k.value == "role":
                                d["role"] = v.value if isinstance(v, ast.Constant) else (ast.get_source_segment(self.src, v) or "user")
                            else:
                                d["content"] = self._content(v, scope)
                    if "content" in d:
                        d.setdefault("role", "user")
                        msgs.append(d)
                elif isinstance(elt, ast.Starred):
                    msgs.append({"role": "history", "content": "{{" + _slot_name(ast.get_source_segment(self.src, elt.value) or "history") + "}}"})
                else:
                    msgs.append({"role": "user", "content": "{{" + _slot_name(ast.get_source_segment(self.src, elt) or "message") + "}}"})
            return msgs
        return None

    # -- LangChain ------------------------------------------------------------
    def lc_template(self, node: ast.AST, scope: dict[str, ast.AST]) -> str:
        """A LangChain prompt template string: `{var}` slots become `{{var}}`."""
        t = self.text(node, scope)
        if t is None:
            return "{{" + _slot_name(ast.get_source_segment(self.src, node) or "input") + "}}"
        t = re.sub(r"(?<!\{)\{(\w+)\}(?!\})", r"{{\1}}", t)
        return t.replace("{ {", "{").replace("} }", "}")      # LangChain's {{ }} escapes are literal braces

    def _lc_message(self, elt: ast.AST, scope: dict[str, ast.AST]) -> dict | None:
        """("system", "..."), SystemMessage(content=...), MessagesPlaceholder("history"), ...PromptTemplate.from_template(...)"""
        if isinstance(elt, ast.Tuple) and len(elt.elts) == 2 and isinstance(elt.elts[0], ast.Constant) \
                and str(elt.elts[0].value) in LC_ROLES:
            role = LC_ROLES[str(elt.elts[0].value)]
            if role == "history":
                return {"role": "history", "content": self.lc_template(elt.elts[1], scope)}
            return {"role": role, "content": self.lc_template(elt.elts[1], scope)}
        if not isinstance(elt, ast.Call):
            return None
        name = (_attr_chain(elt.func) or [""])[-1]
        arg = elt.args[0] if elt.args else next((k.value for k in elt.keywords if k.arg in ("content", "template", "variable_name")), None)
        if name in ("SystemMessage", "HumanMessage", "AIMessage") and arg is not None:
            role = {"SystemMessage": "system", "HumanMessage": "user", "AIMessage": "assistant"}[name]
            return {"role": role, "content": self._content(arg, scope)}
        if name == "MessagesPlaceholder":
            var = arg.value if isinstance(arg, ast.Constant) else "history"
            return {"role": "history", "content": "{{" + _slot_name(str(var)) + "}}"}
        if name == "from_template" and len(_attr_chain(elt.func)) >= 2 and arg is not None:
            owner = _attr_chain(elt.func)[-2]
            role = {"SystemMessagePromptTemplate": "system", "AIMessagePromptTemplate": "assistant"}.get(owner, "user")
            return {"role": role, "content": self.lc_template(arg, scope)}
        return None

    def lc_prompt(self, node: ast.AST | None, scope: dict[str, ast.AST], depth: int = 0) -> list[dict] | None:
        """Messages for whatever feeds a chat model: a prompt template, a message list, or a plain string."""
        if node is None or depth > 4:
            return None
        if isinstance(node, ast.Name) and node.id in scope and scope[node.id] is not node:
            return self.lc_prompt(scope[node.id], scope, depth + 1)
        if isinstance(node, ast.Call):
            chain = _attr_chain(node.func)
            name = chain[-1] if chain else ""
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ("messages", "template")), None)
            if name in ("from_messages", "ChatPromptTemplate") and arg is not None:
                return self.messages(arg, scope)
            if name in ("from_template", "PromptTemplate") and arg is not None:
                return [{"role": "user", "content": self.lc_template(arg, scope)}]
            if name in ("format_messages", "format_prompt", "invoke", "to_messages") and isinstance(node.func, ast.Attribute):
                return self.lc_prompt(node.func.value, scope, depth + 1)   # prompt.format_messages(...)
            if name == "format" and isinstance(node.func, ast.Attribute):
                inner = self.lc_prompt(node.func.value, scope, depth + 1)
                return inner if inner is not None else [{"role": "user", "content": self._content(node, scope)}]
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            return self.messages(node, scope)
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) or isinstance(node, ast.Name):
            return [{"role": "user", "content": self._content(node, scope)}]
        return None

    def _content(self, v: ast.AST, scope: dict[str, ast.AST]) -> str:
        if isinstance(v, (ast.List, ast.Tuple)):  # multimodal content parts
            parts = []
            for p in v.elts:
                if isinstance(p, ast.Dict):
                    for k, val in zip(p.keys, p.values):
                        if isinstance(k, ast.Constant) and k.value == "text":
                            parts.append(self.text(val, scope) or "")
                        elif isinstance(k, ast.Constant) and k.value == "image_url":
                            parts.append("{{image}}")
            return "\n".join(parts)
        t = self.text(v, scope)
        return t if t is not None else "{{" + _slot_name(ast.get_source_segment(self.src, v) or "content") + "}}"


def _env_default(node: ast.AST | None, scope: dict, depth: int = 0) -> str | None:
    """The string default of os.environ.get(...) / os.getenv(...), following one level of names."""
    if isinstance(node, ast.Name) and depth < 2 and node.id in scope and scope[node.id] is not node:
        return _env_default(scope[node.id], scope, depth + 1)
    if not isinstance(node, ast.Call):
        return None
    chain = _attr_chain(node.func)
    if chain[-2:] not in (["environ", "get"], ["os", "getenv"]) and chain != ["getenv"]:
        return None
    default = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "default"), None)
    if isinstance(default, ast.Constant) and isinstance(default.value, str):
        return default.value
    return None


def _slot_name(expr: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", expr.strip()).strip("_")
    return s[:40] or "value"


def _literal_or_src(src: str, node: ast.AST):
    try:
        return ast.literal_eval(node)
    except Exception:
        return ast.get_source_segment(src, node)


def scan_file(path: Path, root: Path) -> tuple[list[CallSite], list[ClientSite]]:
    try:
        src = path.read_text(encoding="utf-8-sig", errors="replace")   # -sig: files saved with a BOM
    except OSError:
        return [], []
    if not LLM_HINT.search(src):
        return [], []           # most files never mention an LLM library: skip parsing them
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # invalid escapes etc. in the scanned code aren't our business
            tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return [], []
    rel = path.relative_to(root).as_posix()
    resolver = _Resolver(src, tree)
    nodes = list(ast.walk(tree))     # walk once, reuse everywhere below
    parents: dict[ast.AST, ast.AST] = {}
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing_func(node: ast.AST) -> ast.AST | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    calls: list[CallSite] = []
    clients: list[ClientSite] = []
    anthropic_names = _anthropic_names(nodes)
    litellm = _litellm_names(nodes)

    def langchain_site(node: ast.Call, ctor: str) -> CallSite:
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        notes: list[str] = []
        # the model
        mnode = kw.get("model") or kw.get("model_name") or (node.args[0] if ctor == "init_chat_model" and node.args else None)
        func = enclosing_func(node)
        scope = resolver.scope_for(func)
        model = None
        if isinstance(mnode, ast.Constant) and isinstance(mnode.value, str):
            model = mnode.value.split(":", 1)[-1] if ":" in mnode.value else mnode.value
        elif isinstance(mnode, ast.Name) and isinstance(scope.get(mnode.id), ast.Constant):
            model = scope[mnode.id].value if isinstance(scope[mnode.id].value, str) else None
        elif _env_default(mnode, scope):
            model = _env_default(mnode, scope)
        # what the model object is called, so its uses can be found
        target, parent = None, parents.get(node)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            tg = parent.targets[0] if isinstance(parent, ast.Assign) else parent.target
            target = ".".join(_attr_chain(tg)) or None
        aliases = {target} if target else set()
        # a model held in a local variable is only visible inside its own function
        region = list(ast.walk(func)) if func is not None and target and "." not in target else nodes
        for n in region:   # structured = llm.with_structured_output(X) / llm.bind(...)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) \
                    and n.value.func.attr in ("with_structured_output", "bind", "bind_tools", "with_retry") \
                    and ".".join(_attr_chain(n.value.func.value)) in aliases:
                aliases.add(".".join(_attr_chain(n.targets[0])))
        msgs, used_in, schema_name, uses = None, None, None, 0

        def is_llm(e: ast.AST) -> bool:
            return e is node or (bool(aliases) and ".".join(_attr_chain(e)) in aliases) or \
                (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and is_llm(e.func.value))

        for n in region:
            found = None
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) and not isinstance(parents.get(n), ast.BinOp):
                ops, cur = [], n          # flatten  a | b | c
                while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.BitOr):
                    ops.insert(0, cur.right)
                    cur = cur.left
                ops.insert(0, cur)
                for i, op in enumerate(ops):
                    if i and is_llm(op):
                        found = ops[i - 1]
                        break
            elif isinstance(n, ast.Call) and (_attr_chain(n.func) or [""])[-1] in LC_PROMPT_TAKERS:
                # LLMChain(llm=llm, prompt=P), create_stuff_documents_chain(llm, P)
                args = list(n.args) + [k.value for k in n.keywords if k.arg in ("llm", "model")]
                if any(is_llm(a) for a in args):
                    found = next((k.value for k in n.keywords if k.arg == "prompt"), n.args[1] if len(n.args) > 1 else None)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in LC_INVOKE and is_llm(n.func.value) and n.args:
                    found = n.args[0]
                elif n.func.attr == "with_structured_output" and is_llm(n.func.value) and n.args \
                        and isinstance(n.args[0], ast.Name):
                    schema_name = n.args[0].id
            if found is not None:
                uses += 1
                if msgs is None:
                    where = enclosing_func(n)
                    msgs = resolver.lc_prompt(found, resolver.scope_for(where))
                    if msgs is not None:
                        used_in = where
        if uses > 1:
            notes.append(f"model object used in {uses} places; tested with the first prompt found")
        skip = None
        if msgs is None:
            # handed to an agent framework (create_react_agent(llm, ...), Agent(llm=llm), ...)?
            for n in region:
                if isinstance(n, ast.Call) and n is not node and not (
                        isinstance(n.func, ast.Attribute) and n.func.attr in LC_INVOKE | {"with_structured_output", "bind", "bind_tools"}):
                    if any(is_llm(a) for a in list(n.args) + [k.value for k in n.keywords]):
                        callee = ".".join(_attr_chain(n.func)) or "another object"
                        skip = (f"passed to {callee}() on line {n.lineno}, which drives it in an agent loop; "
                                "single-prompt tests can't judge that, so it's left as is")
                        break
            msgs = [{"role": "user", "content": "{{input}}"}]
            notes.append(skip or "LangChain prompt not found statically; tests fill a single user turn")
        params = {k: _literal_or_src(src, kw[k]) for k in ("temperature", "max_tokens", "max_completion_tokens",
                                                            "top_p", "seed", "stop") if k in kw}
        if schema_name:
            schema = pydantic_schema(tree, schema_name)
            if schema:
                params["output_schema"] = {"name": schema_name, "schema": schema}
                notes.append(f"structured output: Pydantic model {schema_name}")
        home = used_in or func
        variables = sorted({m for msg in msgs for m in PLACEHOLDER.findall(msg.get("content", ""))})
        snippet = ast.get_source_segment(src, node) or ""
        return CallSite(
            id=hashlib.sha1(f"{rel}:{node.lineno}:{snippet}".encode()).hexdigest()[:8], file=rel, line=node.lineno,
            end_line=node.end_lineno or node.lineno, sdk=LANGCHAIN_CTORS[ctor], method=ctor,
            function=home.name if home is not None else None, model=model,
            model_expr=ast.get_source_segment(src, mnode) if mnode is not None else None, model_span=None,
            messages=msgs, variables=variables, params=params, snippet=snippet,
            context=_trim(ast.get_source_segment(src, home) if home is not None else snippet, 120),
            notes=notes, arg_spans={k.arg: (k.value.lineno, k.value.col_offset, k.value.end_lineno, k.value.end_col_offset)
                                    for k in node.keywords if k.arg},
            call_span=(node.lineno, node.col_offset, node.end_lineno, node.end_col_offset),
            docstring=ast.get_docstring(home) if isinstance(home, (ast.FunctionDef, ast.AsyncFunctionDef)) else None,
            skip=skip)

    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        chain = _attr_chain(node.func)
        if chain and chain[-1] in LANGCHAIN_CTORS:
            calls.append(langchain_site(node, chain[-1]))
            continue
        if chain and chain[-1] in CLIENT_CTORS:
            clients.append(ClientSite(rel, node.lineno, node.end_lineno or node.lineno, node.col_offset,
                                      node.end_col_offset or 0, chain[-1], CLIENT_CTORS[chain[-1]],
                                      ast.get_source_segment(src, node) or "",
                                      [k.arg for k in node.keywords]))
            continue
        match = _match_call(node, anthropic_names, litellm)
        if not match:
            continue
        sdk, method = match
        recv = node.func
        for _ in method.split("."):
            recv = recv.value if isinstance(recv, ast.Attribute) else recv
        receiver_span = (recv.lineno, recv.col_offset, recv.end_lineno, recv.end_col_offset) if recv is not node.func else None
        awaited = isinstance(parents.get(node), ast.Await)
        func = enclosing_func(node)
        scope = resolver.scope_for(func)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        notes: list[str] = []

        # model
        model_node = kw.get("model")
        model, model_span = None, None
        if isinstance(model_node, ast.Constant) and isinstance(model_node.value, str):
            model = model_node.value
            model_span = (model_node.lineno, model_node.col_offset, model_node.end_lineno, model_node.end_col_offset)
        elif isinstance(model_node, ast.Name) and isinstance(scope.get(model_node.id), ast.Constant):
            const = scope[model_node.id]
            model = const.value if isinstance(const.value, str) else None
            # rewrite the constant itself only if it's module level in this file
            if model_node.id in resolver.module_consts and resolver.module_consts[model_node.id] is const:
                model_span = (const.lineno, const.col_offset, const.end_lineno, const.end_col_offset)
                notes.append(f"model comes from constant {model_node.id}")
        elif _env_default(model_node, scope) is not None:
            # os.environ.get("OPENAI_MODEL", "gpt-4o") / os.getenv(...): the default is what runs today
            model = _env_default(model_node, scope)
            notes.append("model comes from an env var; its default is what was tested")
        elif model_node is not None:
            notes.append("model is chosen at runtime; Switchyard will route it via an env var")

        # messages / prompt
        if sdk == "openai-responses":
            msgs = resolver.messages(kw.get("input"), scope)
            if msgs is None and "input" in kw:
                msgs = [{"role": "user", "content": resolver._content(kw["input"], scope)}]
            if "instructions" in kw:
                msgs = [{"role": "system", "content": resolver._content(kw["instructions"], scope)}] + (msgs or [])
        else:
            msgs = resolver.messages(kw.get("messages"), scope)
            if sdk == "anthropic" and "system" in kw:
                msgs = [{"role": "system", "content": resolver._content(kw["system"], scope)}] + (msgs or [])
        if msgs is None:
            expr = ast.get_source_segment(src, kw["messages"]) if "messages" in kw else "messages"
            msgs = [{"role": "user", "content": "{{" + _slot_name(expr or "messages") + "}}"}]
            notes.append("messages are built dynamically; tests fill them as a single user turn")

        params = {}
        for name in ("temperature", "max_tokens", "max_completion_tokens", "max_output_tokens", "top_p", "response_format",
                     "tools", "tool_choice", "stream", "n", "seed", "stop", "text_format", "response_model"):
            if name in kw:
                params[name] = _literal_or_src(src, kw[name])
        if params.get("stream") is True:
            notes.append("streaming call; tested non-streamed, behaviour is identical on Token Factory")
        rf = kw.get("response_format") or kw.get("text_format") or kw.get("response_model")
        if isinstance(rf, ast.Name):
            schema = pydantic_schema(tree, rf.id)
            if schema:
                params["output_schema"] = {"name": rf.id, "schema": schema}
                notes.append(f"structured output: Pydantic model {rf.id}")

        variables = sorted({m for msg in msgs for m in PLACEHOLDER.findall(msg.get("content", ""))})
        context = ast.get_source_segment(src, func) if func is not None else ast.get_source_segment(src, node)
        context = _trim(context or "", 120)
        snippet = ast.get_source_segment(src, node) or ""
        func_name = func.name if func is not None else None
        sid = hashlib.sha1(f"{rel}:{node.lineno}:{snippet}".encode()).hexdigest()[:8]
        calls.append(CallSite(
            id=sid, file=rel, line=node.lineno, end_line=node.end_lineno or node.lineno, sdk=sdk, method=method,
            function=func_name, model=model,
            model_expr=ast.get_source_segment(src, model_node) if model_node is not None else None,
            model_span=model_span, messages=msgs, variables=variables, params=params,
            snippet=snippet, context=context,
            arg_spans={k.arg: (k.value.lineno, k.value.col_offset, k.value.end_lineno, k.value.end_col_offset)
                       for k in node.keywords if k.arg},
            call_span=(node.lineno, node.col_offset, node.end_lineno, node.end_col_offset),
            receiver_span=receiver_span, awaited=awaited,
            docstring=ast.get_docstring(func) if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)) else None,
            notes=notes,
        ))
    return calls, clients


_PRIMS = {"str": {"type": "string"}, "int": {"type": "integer"}, "float": {"type": "number"},
          "bool": {"type": "boolean"}, "datetime": {"type": "string", "format": "date-time"},
          "date": {"type": "string", "format": "date"}, "Any": {}, "dict": {"type": "object"}, "Dict": {"type": "object"}}


def pydantic_schema(tree: ast.Module, name: str, _seen: frozenset = frozenset()) -> dict | None:
    """JSON schema for a Pydantic model defined in this module, built from the AST alone."""
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    cls = classes.get(name)
    if cls is None or name in _seen:
        return None
    seen = _seen | {name}
    bases = {ast.unparse(b) for b in cls.bases}
    if any(b in classes for b in bases):  # inherits from another local model
        parent = next(b for b in bases if b in classes)
        base_schema = pydantic_schema(tree, parent, seen) or {"properties": {}, "required": []}
    else:
        if not any("BaseModel" in b for b in bases):
            if any(b in ("Enum", "str, Enum", "StrEnum") or "Enum" in b for b in bases):
                vals = [s.value.value for s in cls.body if isinstance(s, ast.Assign) and isinstance(s.value, ast.Constant)]
                return {"type": "string", "enum": vals}
            return None
        base_schema = {"properties": {}, "required": []}

    def conv(ann: ast.AST) -> dict:
        if isinstance(ann, ast.Constant) and ann.value is None:
            return {"type": "null"}
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):   # forward ref
            return conv(ast.parse(ann.value, mode="eval").body)
        if isinstance(ann, ast.Name) or isinstance(ann, ast.Attribute):
            n = ann.id if isinstance(ann, ast.Name) else ann.attr
            if n in _PRIMS:
                return dict(_PRIMS[n])
            if n in ("list", "List"):
                return {"type": "array"}
            sub = pydantic_schema(tree, n, seen)
            return sub if sub else {"description": n}
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            return {"anyOf": [conv(ann.left), conv(ann.right)]}
        if isinstance(ann, ast.Subscript):
            head = ann.value.id if isinstance(ann.value, ast.Name) else getattr(ann.value, "attr", "")
            inner = ann.slice
            args = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
            if head in ("list", "List", "Sequence", "set", "Set", "tuple", "Tuple"):
                return {"type": "array", "items": conv(args[0])}
            if head == "Optional":
                return {"anyOf": [conv(args[0]), {"type": "null"}]}
            if head == "Union":
                return {"anyOf": [conv(a) for a in args]}
            if head == "Literal":
                return {"enum": [a.value for a in args if isinstance(a, ast.Constant)]}
            if head in ("dict", "Dict"):
                return {"type": "object"}
            if head == "Annotated":
                return conv(args[0])
        return {}

    props = dict(base_schema.get("properties", {}))
    required = list(base_schema.get("required", []))
    for stmt in cls.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
            continue
        field_name = stmt.target.id
        sch = conv(stmt.annotation)
        has_default = stmt.value is not None
        if isinstance(stmt.value, ast.Call) and _attr_chain(stmt.value.func)[-1:] == ["Field"]:
            kwd = {k.arg: k.value for k in stmt.value.keywords}
            desc = kwd.get("description")
            if isinstance(desc, ast.Constant):
                sch["description"] = desc.value
            positional_default = stmt.value.args and not (isinstance(stmt.value.args[0], ast.Constant) and stmt.value.args[0].value is Ellipsis)
            has_default = bool(positional_default) or "default" in kwd or "default_factory" in kwd
        props[field_name] = sch
        if not has_default and field_name not in required:
            required.append(field_name)
    out = {"type": "object", "properties": props, "required": required}
    doc = ast.get_docstring(cls)
    if doc:
        out["description"] = doc
    return out


def _trim(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + [f"# ... {len(lines) - max_lines} more lines"])


def iter_python_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def scan_repo(root: str | Path) -> dict:
    root = Path(root).resolve()
    calls: list[CallSite] = []
    clients: list[ClientSite] = []
    files = 0
    loc = 0
    for path in iter_python_files(root):
        files += 1
        try:
            loc += sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        c, cl = scan_file(path, root)
        calls.extend(c)
        clients.extend(cl)
    out_calls = [c.to_dict() for c in calls]
    out_clients = [asdict(c) for c in clients]
    # JavaScript / TypeScript (Next.js, Node, Vercel AI SDK)
    from . import jsscan
    for path in jsscan.iter_js_files(root):
        files += 1
        try:
            loc += sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        c, cl = jsscan.scan_js_file(path, root)
        out_calls.extend(c)
        out_clients.extend(cl)
    out_calls.sort(key=lambda c: (c["file"], c["line"]))
    return {"root": str(root), "files": files, "loc": loc, "calls": out_calls, "clients": out_clients}


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(scan_repo(sys.argv[1] if len(sys.argv) > 1 else "."), indent=2)[:20000])
