"""Pull-request review for AI code: what a PR changes in the app's LLM calls, in one scannable comment.

For the commits between a base and a head (default: the merge-base of origin/main and HEAD):

* the LLM calls the PR adds, changes or removes (Python and JS/TS, via the scanner), each with its
  file:line, function, SDK, model and, when the prompt makes it obvious, what it does;
* what each call would cost at volume on its current model and on NVIDIA Nemotron (Lightning / Super)
  on Nebius Token Factory, from the prompt template's length and the project's price tables;
* which Nemotron tier the task shape suggests, worded as something to test with `switchyard run`,
  or the tested result when a migration run already covered that call;
* security findings (audit + secure) on the lines the PR touched, with the fix.

Fully static: no code from the repo runs and no model is called. The only network use is the OSV
query for dependencies whose lines changed, and that query is injectable (tests stub it).

    from switchyard import prreview
    res = prreview.review(".", base="origin/main")
    print(prreview.markdown(res))
    prreview.fail_level(res)          # "CRITICAL" / "HIGH" / ... / None
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import audit, config, scanner, secure

MARKER = "<!-- switchyard-pr-review -->"
SEVERITY_ORDER = audit.SEVERITY_ORDER
# Token estimate, the same formula pipeline.estimate_cost uses: the template's characters / 4, plus
# ~300 tokens for the runtime values that fill its slots, and ~400 tokens of answer (less when the
# call caps max_tokens lower).
INPUT_PAD = 300
DEFAULT_OUT = 400
# Findings about a whole file count when the PR touches that file at all.
FILE_LEVEL_RULES = {"SE2", "DK1"}
MAX_ROWS = 40                 # keep the comment well under GitHub's 65,536-character limit
MAX_COMMENT = 60_000


# -- git ---------------------------------------------------------------------------------------------

def _git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), "-c", "core.quotepath=off", *args], capture_output=True, timeout=120)
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {err[:400]}")
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else ""


def _show(top: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(top), "show", f"{rev}:{path}"], capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 else None


def _rev(cwd: Path, ref: str) -> str:
    out = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
    if not out:
        raise RuntimeError(f"{ref!r} is not a commit in this repository (in CI, check out with fetch-depth: 0)")
    return out


def _default_base(cwd: Path, head: str) -> tuple[str, str]:
    for ref in ("origin/main", "origin/master", "main", "master"):
        mb = _git(cwd, "merge-base", ref, head, check=False).strip()
        if mb:
            return mb, f"merge-base of {ref} and {head}"
    raise RuntimeError("no base to compare against: pass base= (a branch, tag or commit), "
                       "e.g. the pull request's base commit")


def _path(p: str) -> str | None:
    p = p.rstrip("\t")
    if p == "/dev/null":
        return None
    if len(p) > 1 and p[0] == p[-1] == '"':
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text: str) -> list[dict]:
    """`git diff --unified=0` -> per file: old/new path, status, added (new-side) and removed (old-side) lines."""
    files: list[dict] = []
    cur, in_hunk, old_ln, new_ln = None, False, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "status": "modified", "added": set(), "removed": set(), "text": {}}
            files.append(cur)
            in_hunk = False
            m = re.match(r'diff --git "?a/(.+?)"? "?b/(.+?)"?$', line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            continue
        if cur is None:
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_ln, new_ln, in_hunk = int(m.group(1)), int(m.group(2)), True
            continue
        if not in_hunk:
            if line.startswith("--- "):
                cur["old"] = _path(line[4:])
            elif line.startswith("+++ "):
                cur["new"] = _path(line[4:])
            elif line.startswith("new file mode"):
                cur["status"] = "added"
            elif line.startswith("deleted file mode"):
                cur["status"] = "deleted"
            elif line.startswith("rename from "):
                cur["old"], cur["status"] = line[len("rename from "):], "renamed"
            elif line.startswith("rename to "):
                cur["new"], cur["status"] = line[len("rename to "):], "renamed"
            continue
        if line.startswith("+"):
            cur["added"].add(new_ln)
            cur["text"][new_ln] = line[1:]
            new_ln += 1
        elif line.startswith("-"):
            cur["removed"].add(old_ln)
            old_ln += 1
        elif line.startswith(" "):
            old_ln += 1
            new_ln += 1
    for f in files:
        if f["status"] == "added":
            f["old"] = None
        elif f["status"] == "deleted":
            f["new"] = None
    return files


def _blobs(top: Path, rev: str, paths: list[str]) -> dict[str, bytes]:
    """Contents of `paths` (repo-relative) at `rev`, read by one `git cat-file --batch` process."""
    if not paths:
        return {}
    req = "".join(f"{rev}:{p}\n" for p in paths).encode("utf-8")
    r = subprocess.run(["git", "-C", str(top), "cat-file", "--batch"], input=req, capture_output=True, timeout=300)
    out, data, i = {}, r.stdout, 0
    for p in paths:
        nl = data.find(b"\n", i)
        if nl < 0:
            break
        header = data[i:nl].split()
        i = nl + 1
        if len(header) != 3 or header[1] in (b"missing", b"ambiguous"):
            continue
        size = int(header[2])
        if header[1] == b"blob":
            out[p] = data[i:i + size]
        i += size + 1                    # content, then a newline
    return out


def _materialize(top: Path, rev: str, prefix: str, paths: list[str], dest: Path) -> list[str]:
    """Write `paths` (relative to the reviewed folder) as they are at `rev` under `dest`."""
    written = []
    blobs = _blobs(top, rev, [prefix + p for p in paths])
    for p in paths:
        data = blobs.get(prefix + p)
        if data is None or len(data) > 1_000_000:
            continue
        out = dest / p
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        except OSError:
            continue
        written.append(p)
    return written


# -- call sites --------------------------------------------------------------------------------------

def _sig(c: dict) -> tuple:
    return (c.get("model") or c.get("model_expr"), json.dumps(c.get("messages"), sort_keys=True, default=str),
            json.dumps(c.get("params"), sort_keys=True, default=str))


def _touched(c: dict, added: set[int]) -> bool:
    return any(n in added for n in range(c["line"], (c.get("end_line") or c["line"]) + 1))


def _changes(old: dict, new: dict) -> list[str]:
    out = []
    om, nm = old.get("model") or old.get("model_expr"), new.get("model") or new.get("model_expr")
    if om != nm:
        out.append(f"model {om or '?'} → {nm or '?'}")
    if json.dumps(old.get("messages"), sort_keys=True) != json.dumps(new.get("messages"), sort_keys=True):
        out.append("prompt changed")
    op, np_ = old.get("params") or {}, new.get("params") or {}
    keys = sorted(k for k in set(op) | set(np_) if json.dumps(op.get(k), default=str) != json.dumps(np_.get(k), default=str))
    if keys:
        out.append("settings changed: " + ", ".join(keys))
    return out


def match_calls(base_calls: list[dict], head_calls: list[dict], files: list[dict]) -> tuple[list[dict], list[dict]]:
    """(added or changed head calls, each with status/changes; removed base calls)."""
    renamed = {f["old"]: f["new"] for f in files if f["old"] and f["new"]}
    added_lines = {f["new"]: f["added"] for f in files if f["new"]}

    def key(c: dict, file: str) -> tuple:
        return (file, c.get("function"), c.get("method"))

    pool: dict[tuple, list[dict]] = {}
    for b in base_calls:
        pool.setdefault(key(b, renamed.get(b["file"], b["file"])), []).append(b)
    changed, unmatched = [], []
    used: set[int] = set()
    for h in head_calls:
        cands = pool.get(key(h, h["file"])) or []
        b = cands.pop(0) if cands else None
        touched = _touched(h, added_lines.get(h["file"], set()))
        if b is None:
            unmatched.append((h, touched))
            continue
        used.add(id(b))
        diffs = _changes(b, h)
        if diffs or touched:
            changed.append(h | {"status": "changed", "changes": diffs or ["call edited"],
                                "previous": {"file": b["file"], "line": b["line"], "model": b.get("model") or b.get("model_expr")}})
    leftovers = [b for b in base_calls if id(b) not in used]
    for h, touched in unmatched:
        # same call, same prompt, only its function was renamed or it moved: not a change to review
        twin = next((b for b in leftovers if renamed.get(b["file"], b["file"]) == h["file"] and _sig(b) == _sig(h)), None)
        if twin is not None and not touched:
            leftovers.remove(twin)
            continue
        changed.append(h | {"status": "added", "changes": []})
    changed.sort(key=lambda c: (c["file"], c["line"]))
    removed = [{"file": b["file"], "line": b["line"], "function": b.get("function"), "sdk": b["sdk"],
                "model": b.get("model") or b.get("model_expr")} for b in leftovers]
    return changed, removed


# -- cost --------------------------------------------------------------------------------------------

def tokens(call: dict) -> tuple[int, int]:
    """(input, output) tokens per call, estimated from the prompt template (see INPUT_PAD / DEFAULT_OUT)."""
    tin = sum(len(str(m.get("content", ""))) for m in call.get("messages") or []) // 4 + INPUT_PAD
    p = call.get("params") or {}
    cap = next((p[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if isinstance(p.get(k), int)), None)
    tout = min(cap, DEFAULT_OUT) if cap and cap > 0 else DEFAULT_OUT
    return tin, tout


def current_model(call: dict) -> tuple[config.Model, str, bool]:
    """(price model, label, already on Nemotron?) for the model a call uses today."""
    name = call.get("model")
    low = (name or "").lower()
    for m in config.CANDIDATES:
        if low and (low == m.id.lower() or low.endswith("/" + m.id.lower().split("/")[-1])):
            return m, m.label, True
    if "nemotron" in low:
        m = next((c for c in config.CANDIDATES if c.key in low), config.MODELS["super"])
        return m, name, True
    pin, pout, matched = config.source_price(name)
    if not name:
        expr = call.get("model_expr") or "chosen at runtime"
        label = f"{_short(expr, 40)} (priced as gpt-4o)"
    elif matched.startswith("default"):
        label = f"{name} (no list price; priced as gpt-4o)"
    else:
        label = name
    return config.Model("source", name or "unknown", 0, pin, pout, label), label, False


def cost(call: dict, volume: int) -> dict:
    tin, tout = tokens(call)
    now, label, on_nemotron = current_model(call)
    out = {"tokens_in": tin, "tokens_out": tout, "on_nemotron": on_nemotron,
           "current": {"model": label, "price": [now.price_in, now.price_out],
                       "per_1k": now.cost(tin, tout) * 1000, "monthly": now.cost(tin, tout) * volume}}
    for m in config.CANDIDATES:
        out[m.key] = {"model": m.label, "price": [m.price_in, m.price_out],
                      "per_1k": m.cost(tin, tout) * 1000, "monthly": m.cost(tin, tout) * volume}
    return out


# -- what it does and which tier fits ---------------------------------------------------------------

_TASKS = [  # (task, tier, pattern) checked in order on the prompt text
    ("classification", "lightning", r"\b(classif\w*|categori[sz]\w*|label(?:s|ing)?|sentiment|intent|moderat\w*|"
                                    r"spam|toxic\w*|yes or no|true or false|one word|which (?:one|of)|triage)\b"),
    ("extraction", "lightning", r"\b(extract\w*|parse|parsing|entities|fields?|key ?words?|structured data|"
                                r"pull out|identify the)\b"),
    ("routing", "lightning", r"\b(route|routing|router|dispatch|decide which|select the (?:best )?(?:tool|agent|handler))\b"),
    ("short rewrite", "lightning", r"\b(rephrase|rewrite|paraphrase|translate|fix (?:the )?grammar|proofread|"
                                   r"title|headline|tagline|slug)\b"),
    ("reasoning", "super", r"\b(step[- ]by[- ]step|reason(?:ing)?|think (?:through|carefully)|prove|proof|math\w*|"
                           r"calculat\w*|solve|diagnos\w*|legal|debug|write (?:the )?code|implement)\b"),
    ("long-form writing", "super", r"\b(essay|article|blog|story|stories|report|newsletter|cover letter|proposal|"
                                   r"chapter|in detail|detailed|comprehensive|long-form|write an?|draft an?|compose)\b"),
]
_GENERIC = re.compile(r"^you are (?:a |an )?(?:helpful|friendly|smart|useful|ai|an ai)[\w ,]*assistant\W*$", re.I)


def _prompt_text(call: dict) -> str:
    return "\n".join(scanner.PLACEHOLDER.sub(" ", str(m.get("content", ""))) for m in call.get("messages") or []
                     if m.get("role") in ("system", "user", "developer"))


def _sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\s*\n\s*", text) if s.strip()]


def what_it_does(call: dict) -> str | None:
    """One line, only when the code says it plainly: the function's docstring, else the prompt's opening."""
    doc = (call.get("docstring") or "").strip().splitlines()
    if doc and doc[0].strip():
        return _short(doc[0].strip(), 110)
    for role in ("system", "developer", "user"):
        for m in call.get("messages") or []:
            if m.get("role") != role:
                continue
            text = scanner.PLACEHOLDER.sub("…", str(m.get("content", "")))
            for s in _sentences(text):
                if len(re.sub(r"[^A-Za-z]", "", s)) < 12 or _GENERIC.match(s):
                    continue
                y = re.match(r"^you are (an? |the )?(.+?)[.!]?$", s, re.I)
                return _short(f"acts as {y.group(1) or ''}{y.group(2)}" if y else s, 110)
    return None


def suggest(call: dict) -> dict:
    """Which Nemotron tier the task shape points to. A starting point for `switchyard run`, never a verdict."""
    if call.get("skip"):
        return {"model": None, "task": "agent loop", "why": call["skip"]}
    text = _prompt_text(call) + "\n" + (call.get("docstring") or "") + "\n" + (call.get("function") or "").replace("_", " ")
    p = call.get("params") or {}
    cap = next((p[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if isinstance(p.get(k), int)), None)
    structured = bool(p.get("response_format") or p.get("output_schema") or p.get("text_format")
                      or p.get("response_model") or re.search(r"\bjson\b", text, re.I))
    hits = {task: (tier, len(re.findall(rx, text, re.I))) for task, tier, rx in _TASKS}
    short = max(((t, n) for t, (tier, n) in hits.items() if tier == "lightning"), key=lambda x: x[1])
    long = max(((t, n) for t, (tier, n) in hits.items() if tier != "lightning"), key=lambda x: x[1])
    if short[1] and (short[1] >= long[1] or (cap and cap <= 300)):
        why = f"{short[0]}-shaped prompt" + (", structured output" if structured else "") + \
              (f", answers capped at {cap} tokens" if cap and cap <= 300 else "")
        return {"model": "lightning", "task": short[0], "why": why}
    if (cap and cap <= 200) or (structured and not long[1]):
        why = f"short answers (max_tokens={cap})" if cap and cap <= 200 else "structured output"
        return {"model": "lightning", "task": "short structured answer", "why": why}
    if long[1]:
        return {"model": "super", "alt": "ultra", "task": long[0],
                "why": f"{long[0]} prompt; try Ultra if Super falls short"}
    return {"model": "super", "task": "unclear", "why": "the task isn't clear from the prompt alone; Super is a safe "
            "start, and `switchyard run` also tries Lightning"}


# -- previous migration runs ------------------------------------------------------------------------

def _norm_url(u: str) -> str:
    u = u.strip().lower()
    u = re.sub(r"^(?:https?://|ssh://)?(?:[^@/]+@)?", "", u)
    u = u.replace(":", "/", 1) if re.match(r"^[\w.\-]+:(?!\d)", u) else u
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"/tree/.*$", "", u)
    return re.sub(r"(?:\.git)?/*$", "", u)


def _load_runs(top: Path, prefix: str, head: str, runs_dir: Path | None) -> list[tuple[dict, str, str]]:
    """[(run state, folder its site paths are relative to (repo-relative), label)] for this repository."""
    out = []
    sub = prefix.rstrip("/")
    for rel in ([f".switchyard/{sub}/run.json"] if sub else []) + [".switchyard/run.json"]:
        data = _show(top, head, rel)
        if data:
            try:
                st = json.loads(data.decode("utf-8", errors="replace"))
                out.append((st, sub, f"saved run `{rel}`" + (f" ({st['id']})" if st.get("id") else "")))
                break
            except ValueError:
                pass
    remote = _norm_url(_git(top, "remote", "get-url", "origin", check=False))
    rdir = runs_dir if runs_dir is not None else config.runs_dir()
    for p in sorted(Path(rdir).glob("*/run.json")) if Path(rdir).is_dir() else []:
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        co = st.get("checkout") or {}
        src = str(st.get("source") or "")
        if co.get("root"):
            try:
                same = Path(co["root"]).resolve() == top
            except OSError:
                same = False
            if same:
                out.append((st, (co.get("subdir") or "").strip("/"), f"run {st.get('id', p.parent.name)}"))
        elif remote and re.match(r"^(https?://|git@|github\.com/)", src) and _norm_url(src) == remote:
            out.append((st, (st.get("source_subdir") or "").strip("/"), f"run {st.get('id', p.parent.name)}"))
    return out


def tested(call: dict, runs: list[tuple[dict, str, str]], prefix: str) -> dict | None:
    """The latest finished decision for this call (same file + function) from a previous migration run."""
    want = prefix + call["file"]
    best = None
    for st, folder, label in runs:
        for s in st.get("sites") or []:
            site, d = s.get("site") or {}, s.get("decision") or {}
            path = (folder + "/" if folder else "") + str(site.get("file", ""))
            if path != want or site.get("function") != call.get("function") or d.get("action") not in ("migrate", "keep"):
                continue
            when = st.get("finished") or st.get("started") or 0
            if best is None or when > best[0]:
                best = (when, st, s, label)
    if best is None:
        return None
    _, st, s, label = best
    d, sav = s["decision"], s.get("savings") or {}
    m = config.MODELS.get(d.get("model") or "")
    out = {"run": label, "action": d["action"], "model": d.get("model"), "label": m.label if m else None,
           "pass_rate": d.get("pass_rate"), "tests": len(s.get("cases") or []) or None, "reason": d.get("reason"),
           "repaired": bool(d.get("hint")), "saving_pct": sav.get("saving_pct"),
           "stale": json.dumps(s["site"].get("messages"), sort_keys=True) != json.dumps(call.get("messages"), sort_keys=True)}
    return out


# -- security ---------------------------------------------------------------------------------------

def _npm_lines(text: str, file: str) -> dict[str, list[int]]:
    """package name -> the lines that describe it (package.json entries, or a lockfile's package block)."""
    out: dict[str, list[int]] = {}
    lines = text.splitlines()
    if file.endswith("package-lock.json"):
        cur, depth0 = None, None
        for i, ln in enumerate(lines, 1):
            m = re.match(r'^(\s*)"node_modules/(.+?)"\s*:\s*\{', ln)
            if m:
                cur, depth0 = m.group(2), len(m.group(1))
                if "/node_modules/" in cur:
                    cur = None
                    continue
            if cur is not None:
                out.setdefault(cur, []).append(i)
                if re.match(r"^\s*\}", ln) and len(ln) - len(ln.lstrip()) == depth0:
                    cur = None
        return out
    for i, ln in enumerate(lines, 1):
        m = re.match(r'^\s*"([^"]+)"\s*:\s*"', ln)
        if m:
            out.setdefault(m.group(1), []).append(i)
    return out


def _pinned(root: Path) -> set[tuple]:
    if not root.is_dir():
        return set()
    return {(eco, name.lower(), ver, file) for eco, name, ver, _line, file in secure._python_deps(root) + secure._node_deps(root)}


def dep_findings(head_dir: Path, base_dir: Path, changed: dict[str, set[int]], query) -> tuple[list[dict], list[str]]:
    """secure.scan_deps on each folder whose requirements*.txt / package.json / package-lock.json changed,
    querying OSV only for packages this PR adds or re-pins (name + version not in the base manifest)."""
    manifests = [p for p in changed if re.fullmatch(r"requirements[^/]*\.txt", Path(p).name)
                 or Path(p).name in ("package.json", "package-lock.json")]
    findings, notes = [], []
    for folder in sorted({Path(p).parent.as_posix() for p in manifests}):
        pre = "" if folder == "." else folder + "/"
        root = head_dir / pre if pre else head_dir
        before = _pinned(base_dir / pre if pre else base_dir)
        npm = {}
        for name in ("package-lock.json", "package.json"):
            if (root / name).exists() and pre + name in changed:
                npm[name] = _npm_lines((root / name).read_text(encoding="utf-8", errors="replace"), name)
        real_line: dict[tuple, int] = {}

        def wanted(dep: tuple) -> bool:
            eco, name, ver, line, file = dep
            added = changed.get(pre + file, set())
            if (eco, name.lower(), ver, file) in before:
                return False                       # same pin as before the PR (the line moved or got a comma)
            if eco == "PyPI":
                return line in added
            lines = (npm.get(file) or {}).get(name) or []
            if any(n in added for n in lines):
                real_line[(name, ver, file)] = next(n for n in lines if n in added)
                return True
            return False

        def picky(deps: list[tuple]) -> list[list[dict]]:
            idx = [i for i, d in enumerate(deps) if wanted(d)]
            res: list[list[dict]] = [[] for _ in deps]
            if idx:
                for i, vulns in zip(idx, query([deps[i] for i in idx])):
                    res[i] = vulns
            return res

        found, note = secure.scan_deps(root, picky)
        if note:
            notes.append(note)
        for f in found:
            name, ver = f["detail"].split(":", 1)[0].rsplit(" ", 1)
            f["line"] = real_line.get((name, ver, f["file"]), f["line"])
            f["file"] = pre + f["file"]
            findings.append(f)
    return findings, notes


def security(head_dir: Path, base_dir: Path, scan: dict, changed: dict[str, set[int]], calls: list[dict],
             query) -> tuple[list[dict], list[str]]:
    tracked = {p.relative_to(head_dir).as_posix() for p in head_dir.rglob("*") if p.is_file()}
    llm = audit.static(head_dir, scan)
    found = secure.scan_secrets(head_dir, tracked) + secure.scan_config(head_dir)
    notes: list[str] = []
    if query is not False:
        deps, notes = dep_findings(head_dir, base_dir, changed, query or secure.osv_query)
        found += deps
    seen = {(f["file"], f["line"]) for f in found if f["rule"] == "SE1"}
    llm = [f for f in llm if not (f["rule"] == "AU1" and (f["file"], f["line"]) in seen)]
    # a finding reported on a call's first line counts when the PR touched any line of that call
    call_lines = {(c["file"], c["line"]) for c in calls if _touched(c, changed.get(c["file"], set()))}
    keep = []
    for f in llm + found:
        added = changed.get(f["file"])
        if added is None:
            continue
        if f["rule"] in FILE_LEVEL_RULES or f["line"] in added or (f["file"], f["line"]) in call_lines:
            keep.append(f)
    return audit.sort(keep), notes


# -- the review -------------------------------------------------------------------------------------

def review(repo: str | Path = ".", base: str | None = None, head: str = "HEAD", volume: int = 100_000,
           osv_query=None, *, runs_dir: str | Path | None = None) -> dict:
    """Review what changed between `base` and `head` in the app at `repo` (a git checkout or a folder in one).

    `osv_query(deps) -> [[vuln, ...], ...]` checks dependencies (default: the OSV database; False skips it).
    `runs_dir` is where previous migration runs live (default: Switchyard's runs folder)."""
    path = Path(repo).resolve()
    top_s = _git(path, "rev-parse", "--show-toplevel", check=False).strip()
    if not top_s:
        raise RuntimeError(f"{path} is not inside a git repository")
    top = Path(top_s).resolve()
    sub = path.relative_to(top).as_posix() if path != top else ""
    prefix = f"{sub}/" if sub else ""
    head_sha = _rev(top, head)
    if base:
        base_sha = _rev(top, base)
        mb = _git(top, "merge-base", base_sha, head_sha, check=False).strip()
        name = "the base commit" if re.fullmatch(r"[0-9a-f]{7,40}", base) else base
        how = f"merge-base of {name} and {head}" if mb and mb != base_sha else name
        base_sha = mb or base_sha       # a shallow clone has no merge-base: diff against base itself
    else:
        base_sha, how = _default_base(top, head_sha)
    diff = _git(path, "diff", "--no-color", "--no-ext-diff", "--unified=0", "-M", "--relative",
                base_sha, head_sha, "--", ".")
    files = parse_diff(diff)
    changed = {f["new"]: f["added"] for f in files if f["new"]}
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="swy-review-") as tmp:
        base_dir, head_dir = Path(tmp) / "base", Path(tmp) / "head"
        base_dir.mkdir()
        head_dir.mkdir()
        _materialize(top, base_sha, prefix, [f["old"] for f in files if f["old"]], base_dir)
        _materialize(top, head_sha, prefix, list(changed), head_dir)
        base_scan = scanner.scan_repo(base_dir)
        head_scan = scanner.scan_repo(head_dir)
        calls, removed = match_calls(base_scan["calls"], head_scan["calls"], files)
        findings, dep_notes = security(head_dir, base_dir, head_scan, changed, head_scan["calls"], osv_query)
        notes += dep_notes

    runs = _load_runs(top, prefix, head_sha, Path(runs_dir) if runs_dir else None)
    out_calls = []
    for c in calls:
        est = cost(c, volume)
        sug = suggest(c)
        t = tested(c, runs, prefix)
        target = None
        if t and t["action"] == "migrate":
            target = t["model"]
        elif not t and sug.get("model") and not est["on_nemotron"]:
            target = sug["model"]
        projected = est["current"]["monthly"]
        if target and est[target]["monthly"] < projected:
            projected = est[target]["monthly"]
        out_calls.append({
            "status": c["status"], "changes": c["changes"], "previous": c.get("previous"),
            "file": c["file"], "line": c["line"], "end_line": c.get("end_line"), "function": c.get("function"),
            "sdk": c["sdk"], "method": c.get("method"), "model": c.get("model"), "model_expr": c.get("model_expr"),
            "variables": c.get("variables") or [], "skip": c.get("skip"), "notes": c.get("notes") or [],
            "messages": c.get("messages") or [], "what": what_it_does(c), "cost": est, "suggestion": sug,
            "tested": t, "target": target, "projected_monthly": projected,
            "findings": [f for f in findings if f["file"] == c["file"]
                         and c["line"] <= f["line"] <= (c.get("end_line") or c["line"])],
        })
    by_sev = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITY_ORDER}
    totals = {"calls": len(out_calls), "added": sum(1 for c in out_calls if c["status"] == "added"),
              "changed": sum(1 for c in out_calls if c["status"] == "changed"), "removed": len(removed),
              "monthly_now": sum(c["cost"]["current"]["monthly"] for c in out_calls),
              "monthly_projected": sum(c["projected_monthly"] for c in out_calls), "by_severity": by_sev}
    return {"repo": str(top), "subdir": sub, "base": base_sha, "head": head_sha, "base_how": how,
            "volume": volume, "files": [{"path": f["new"] or f["old"], "old": f["old"], "status": f["status"],
                                          "added": len(f["added"]), "removed": len(f["removed"])} for f in files],
            "calls": out_calls, "removed": removed, "findings": findings, "notes": notes, "totals": totals}


def fail_level(result: dict) -> str | None:
    """The worst severity among the findings on changed lines (None when there are none)."""
    sev = {f["severity"] for f in result.get("findings") or []}
    return next((s for s in SEVERITY_ORDER if s in sev), None)


def should_fail(result: dict, fail_on: str = "high") -> bool:
    """True when a finding is at least as severe as `fail_on` (critical/high/medium/low; never = False)."""
    level, fail_on = fail_level(result), (fail_on or "never").upper()
    if level is None or fail_on not in SEVERITY_ORDER:
        return False
    return SEVERITY_ORDER.index(level) <= SEVERITY_ORDER.index(fail_on)


# -- markdown ---------------------------------------------------------------------------------------

def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _cell(s) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def _usd(x: float) -> str:
    if x <= 0:
        return "$0"
    if x < 0.01:
        return "<$0.01"
    if x < 10:
        return f"${x:.2f}"
    return f"${x:,.0f}"


def _volume(v: int) -> str:
    if v >= 1_000_000 and v % 100_000 == 0:
        return f"{v / 1_000_000:g}M"
    if v >= 1000 and v % 100 == 0:
        return f"{v / 1000:g}k"
    return f"{v:,}"


def _where(c: dict) -> str:
    fn = f"`{c['function']}()`" if c.get("function") else "module level"
    return f"`{c['file']}:{c['line']}` {fn}"


def _model_name(c: dict) -> str:
    return c.get("model") or (f"`{_short(c['model_expr'], 40)}`" if c.get("model_expr") else "default")


def _issues(by_sev: dict) -> str:
    total = sum(by_sev.values())
    if not total:
        return "no security issues on changed lines"
    parts = [f"{n} {s.lower()}" for s, n in by_sev.items() if n]
    if total == 1:
        return f"1 {parts[0].split(' ', 1)[1]}-severity issue"
    return f"{total} issues ({', '.join(parts)})"


def verdict(result: dict) -> str:
    t, vol = result["totals"], _volume(result["volume"])
    if t["calls"]:
        head = f"{t['calls']} AI call{'s' if t['calls'] != 1 else ''} changed"
        if t["added"] and t["changed"]:
            head += f" ({t['added']} new)"
        elif t["added"] == t["calls"]:
            head = f"{t['calls']} AI call{'s' if t['calls'] != 1 else ''} added"
    else:
        head = "No AI calls changed"
    parts = [head + (f", {t['removed']} removed" if t["removed"] else "")]
    if t["calls"]:
        now, new = t["monthly_now"], t["monthly_projected"]
        if new < now * 0.95:
            parts.append(f"~{_usd(now)}/month at {vol} calls each could drop to ~{_usd(new)} on Nemotron (estimate)")
        else:
            parts.append(f"~{_usd(now)}/month at {vol} calls each (estimate)")
    parts.append(_issues(t["by_severity"]))
    return " · ".join(parts)


def _fit(c: dict) -> str:
    t, s = c.get("tested"), c["suggestion"]
    if t:
        if t["action"] == "migrate":
            rate = f"{t['pass_rate']:.0%}" if isinstance(t.get("pass_rate"), (int, float)) else "?"
            n = f" of {t['tests']} tests" if t.get("tests") else " of its tests"
            out = f"**Tested:** {t['label'] or t['model']} passed {rate}{n}" + (" with an adapted prompt" if t["repaired"] else "") + \
                  f" ({t['run']})"
        else:
            out = f"**Tested:** kept on the original model: {_short(t.get('reason') or 'no Nemotron model reached the bar', 120)} ({t['run']})"
        if t["stale"]:
            out += "; the prompt changed since, so re-run `switchyard run` to confirm"
        return out
    if c["cost"]["on_nemotron"]:
        return "already on Nemotron"
    if not s.get("model"):
        return f"not suggested: {_short(s['why'], 100)}"
    label = config.MODELS[s["model"]].label
    alt = f" (or {config.MODELS[s['alt']].label})" if s.get("alt") else ""
    return f"**{label}**{alt} for {s['task']}: worth testing with `switchyard run`"


def _cost_line(c: dict, vol: str) -> str:
    e, now = c["cost"], c["cost"]["current"]
    name = _model_name(c)
    if e["on_nemotron"]:
        return f"~{_usd(now['per_1k'])} per 1k calls on {name}"
    key = c.get("target") or (c["suggestion"].get("model") if not c.get("tested") else None)
    if not key:
        return f"~{_usd(now['per_1k'])} per 1k calls on {name} (~{_usd(now['monthly'])}/month at {vol})"
    tgt = e[key]
    if tgt["per_1k"] >= now["per_1k"]:
        light = e["lightning"]
        return (f"~{_usd(now['per_1k'])} per 1k calls on {name}; {tgt['model']} would cost more "
                f"(~{_usd(tgt['per_1k'])}), Lightning ~{_usd(light['per_1k'])}")
    return (f"~{_usd(now['per_1k'])} per 1k calls on {name} → ~{_usd(tgt['per_1k'])} on {tgt['model']} "
            f"(~{_usd(now['monthly'])} → ~{_usd(tgt['monthly'])}/month at {vol})")


def markdown(result: dict) -> str:
    vol = _volume(result["volume"])
    calls, removed, findings = result["calls"], result["removed"], result["findings"]
    lines = [MARKER, "## Switchyard review: AI code in this PR", "", f"**{verdict(result)}**", ""]

    if calls:
        lines += ["### AI calls", "",
                  "| | Call | What it does | Model | Cost estimate | Nemotron fit |", "|---|---|---|---|---|---|"]
        for c in calls[:MAX_ROWS]:
            status = "new" if c["status"] == "added" else "changed"
            if c["status"] == "changed" and c["changes"]:
                status += ": " + "; ".join(c["changes"])
            lines.append(f"| {_cell(status)} | {_cell(_where(c))} ({_cell(c['sdk'])}) | {_cell(c['what'] or '–')} | "
                         f"{_cell(_model_name(c))} | {_cell(_cost_line(c, vol))} | {_cell(_fit(c))} |")
        if len(calls) > MAX_ROWS:
            lines.append(f"\n…and {len(calls) - MAX_ROWS} more calls.")
        lines.append("")
    if removed:
        lines.append("**Removed:** " + ", ".join(f"`{r['file']}:{r['line']}` "
                                                  f"{'`' + r['function'] + '()`' if r.get('function') else 'module level'}"
                                                  f" ({r.get('model') or r['sdk']})" for r in removed[:MAX_ROWS]))
        lines.append("")

    lines += ["### Security on changed lines", ""]
    if findings:
        lines += ["| Severity | Where | Issue | Fix |", "|---|---|---|---|"]
        for f in findings[:MAX_ROWS]:
            lines.append(f"| {f['severity']} | `{_cell(f['file'])}:{f['line']}` | {_cell(f['rule'] + ' ' + f['title'])}: "
                         f"{_cell(f['detail'])} | {_cell(f['fix'])} |")
        if len(findings) > MAX_ROWS:
            lines.append(f"\n…and {len(findings) - MAX_ROWS} more findings.")
    else:
        lines.append("No findings on the lines this PR touches.")
    lines.append("")

    if calls:
        ev = ["<details><summary>Cost estimates per call</summary>", "",
              f"Estimates, not measurements: input tokens = prompt template characters / 4 + {INPUT_PAD} for the "
              f"values filled in at runtime; output = {DEFAULT_OUT} tokens (or the call's lower max_tokens). "
              f"List prices per 1M tokens (input/output) from Switchyard's price table; monthly = {vol} calls per call site.",
              "", "| Call | Tokens in/out | Current | Lightning | Super | Ultra |", "|---|---|---|---|---|---|"]
        for c in calls[:MAX_ROWS]:
            e = c["cost"]
            cols = [f"{_usd(e[k]['per_1k'])}/1k · {_usd(e[k]['monthly'])}/mo" for k in ("current", "lightning", "super", "ultra")]
            cols[0] = f"{_cell(e['current']['model'])} ${e['current']['price'][0]:g}/${e['current']['price'][1]:g}: " + cols[0]
            lines_ = f"| {_cell(_where(c))} | {e['tokens_in']:,}/{e['tokens_out']:,} | " + " | ".join(cols) + " |"
            ev.append(lines_)
        ev += ["", "</details>", ""]
        lines += ev

        ev = ["<details><summary>Prompts and reasoning</summary>", ""]
        for c in calls[:MAX_ROWS]:
            ev.append(f"**{_where(c)}** · {c['sdk']} `{c.get('method') or ''}` · model {_model_name(c)}")
            ev.append("")
            if c["changes"]:
                ev.append(f"- What changed: {'; '.join(c['changes'])}")
            s = c["suggestion"]
            if s.get("model"):
                ev.append(f"- Task shape: {s['task']} ({s['why']}) → {config.MODELS[s['model']].label}. "
                          "A suggestion from the prompt alone; `switchyard run` tests it before anything switches.")
            elif s.get("why"):
                ev.append(f"- {s['why']}")
            if c.get("tested"):
                t = c["tested"]
                ev.append(f"- Previous migration run: {t['run']}, decision `{t['action']}`"
                          + (f" to {t['label']}" if t.get("label") else "") + (f": {t['reason']}" if t.get("reason") else ""))
            if c["variables"]:
                ev.append(f"- Filled at runtime: {', '.join('`' + v + '`' for v in c['variables'][:8])}")
            for n in c["notes"][:3]:
                ev.append(f"- Note: {n}")
            for m in c["messages"][:3]:
                txt = str(m.get("content", ""))
                ev += ["", f"`{m.get('role', 'user')}`:", "```text", _short(txt, 400).replace("```", "'''"), "```"]
            ev.append("")
        ev += ["</details>", ""]
        lines += ev

    if result.get("notes"):
        lines += ["> " + n for n in result["notes"]] + [""]
    lines.append(f"<sub>Static review of `{result['base'][:7]}..{result['head'][:7]}` ({result['base_how']}): no code "
                 "was run and no model was called. Costs are estimates from list prices. Tier suggestions are a "
                 "starting point; `switchyard run` tests each call on every Nemotron tier before switching it.</sub>")
    md = "\n".join(lines)
    if len(md) > MAX_COMMENT:
        cut = md[:MAX_COMMENT]
        if cut.count("<details>") > cut.count("</details>"):
            cut += "\n\n</details>"
        md = cut + "\n\n…(truncated)"
    return md


# -- entry point (the GitHub Action runs `python -m switchyard.prreview`) -----------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m switchyard.prreview",
                                 description="Review a pull request's AI code: calls, cost at volume, Nemotron fit, security.")
    ap.add_argument("repo", nargs="?", default=".", help="the app's folder in a git checkout")
    ap.add_argument("--base", help="commit/branch to compare against (default: merge-base of origin/main and HEAD)")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--volume", type=int, default=100_000, help="calls per month per call site, for the cost estimate")
    ap.add_argument("--out", help="write the Markdown comment here instead of printing it")
    ap.add_argument("--json", action="store_true", help="print the structured result instead of Markdown")
    ap.add_argument("--no-deps", action="store_true", help="skip the OSV dependency check")
    ap.add_argument("--fail-on", default="never", choices=["critical", "high", "medium", "low", "never"])
    a = ap.parse_args(argv)
    try:
        res = review(a.repo, base=a.base or None, head=a.head, volume=a.volume, osv_query=False if a.no_deps else None)
    except RuntimeError as e:
        print(f"switchyard review: {e}", file=sys.stderr)
        return 2
    text = json.dumps(res, indent=1, default=str) if a.json else markdown(res)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # → and · on a Windows console
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(verdict(res))
    else:
        print(text)
    if should_fail(res, a.fail_on):
        print(f"switchyard review: {fail_level(res)} finding on changed lines (fail-on: {a.fail_on})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
