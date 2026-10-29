"""End-to-end migration run: scan -> understand -> test -> evaluate -> decide -> repair -> rewrite."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fnmatch
import os
import threading

from . import agents, config, evaluate, investigate, report, rewrite, runcheck, scanner, traces
from .llm import BudgetExceeded, TokenFactory

RUNS_DIR = config.runs_dir()


def git_checkout(path: str | Path) -> dict | None:
    """If `path` is inside a git work tree: its root, the folder offset, HEAD and whether it's dirty."""
    path = Path(path).resolve()
    git = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, text=True, timeout=30)
    try:
        top = git("rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0 or not top.stdout.strip():
        return None
    root = Path(top.stdout.strip()).resolve()
    try:
        sub = path.relative_to(root).as_posix()
    except ValueError:
        return None
    return {"root": str(root), "subdir": "" if sub == "." else sub,
            "commit": git("rev-parse", "--short", "HEAD").stdout.strip(),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "dirty": bool(git("status", "--porcelain", "--", ".").stdout.strip())}


def _git_files(path: Path) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files under `path`, relative to it."""
    try:
        r = subprocess.run(["git", "-C", str(path), "ls-files", "-co", "--exclude-standard", "-z", "--", "."],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [f for f in r.stdout.split("\0") if f and (path / f).is_file()]


# Rough token counts per call site, from test runs. Used only for the up-front estimate.
_EST = {  # role: (model key, calls per site, tokens in, tokens out)
    "investigate": ("super", 5, 3500, 350),
    "analyze": ("super", 1, 2500, 900),
    "testgen": ("ultra", 1, 3500, 7000),
    "repair": ("ultra", 0.5, 5000, 1500),
}


def estimate_cost(calls: list[dict], tests: int) -> float:
    """A rough upper-middle estimate of Token Factory spend for a run (USD)."""
    m = config.MODELS
    total = 0.0
    for site in calls:
        prompt = sum(len(str(x.get("content", ""))) for x in site.get("messages") or []) // 4 + 300
        for key, n, tin, tout in _EST.values():
            total += n * m[key].cost(tin, tout)
        for cand in config.CANDIDATES:
            total += tests * cand.cost(prompt, 400)
        total += tests * len(config.CANDIDATES) * m[config.ROLE_JUDGE].cost(prompt + 900, 200)
        total += m[config.ROLE_ANALYZE].cost(4000, 600)   # runtime harness, when it's needed
        if [v for v in site.get("variables") or [] if v != "history"]:
            # injection probes on the chosen model: up to 4 attacks x 3 variables, plus re-tests of hits
            total += 16 * m["super"].cost(prompt + 200, 300)
    return round(total, 2)


class Run:
    def __init__(self, source: str, *, tests: int = 8, target: float = 0.85, budget: float = 2.0,
                 use_tavily: bool = True, max_sites: int = 12, emit=None, run_id: str | None = None,
                 include: list[str] | None = None, exclude: list[str] | None = None,
                 traces_path: str | None = None, run_tests: bool = False, execute: bool | None = None,
                 audit: bool = True):
        self.id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.source = source
        self.tests = tests
        self.target = target
        self.budget = budget
        self.use_tavily = use_tavily
        self.max_sites = max_sites
        self.include = include or []
        self.exclude = exclude or []
        self.traces_path = traces_path
        self.run_tests = run_tests
        self.audit = audit
        # run the patched code for real (needs a key; off on the public demo unless allowed)
        self.execute = (runcheck.enabled() and bool(config.api_key())) if execute is None else execute
        self._save_lock = threading.Lock()
        self._last_save = 0.0
        self.dir = RUNS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._emit = emit or (lambda *a, **k: None)
        self.state: dict = {"id": self.id, "source": source, "status": "running", "started": time.time(),
                            "settings": {"tests": tests, "target": target, "budget": budget, "tavily": use_tavily,
                                         "include": self.include, "exclude": self.exclude,
                                         "traces": bool(traces_path), "run_tests": run_tests, "execute": self.execute,
                                         "audit": audit},
                            "sites": [], "events": []}

    # -- events -------------------------------------------------------------
    def emit(self, kind: str, **data):
        ev = {"t": round(time.time() - self.state["started"], 2), "kind": kind, **data}
        with self._save_lock:
            self.state["events"].append(ev)
        self._emit(ev)
        # per-result events are frequent; persist them at most twice a second
        self.save(force=kind not in ("result", "site"))

    def save(self, force: bool = True):
        with self._save_lock:
            now = time.time()
            if not force and now - self._last_save < 0.5:
                return
            self._last_save = now
            data = json.dumps(self.state, indent=1, default=str)
            _write_atomic(self.dir / "run.json", data)

    # -- steps --------------------------------------------------------------
    def fetch(self) -> Path:
        src = self.source.strip()
        dest = self.dir / "repo"
        if re.match(r"^(https?://|git@)", src):
            # https://github.com/owner/repo/tree/<branch>/<sub/dir> -> clone repo, scan only sub/dir
            m = re.match(r"^(https://github\.com/[\w.\-]+/[\w.\-]+?)(?:\.git)?/tree/([\w.\-]+)/?(.*)$", src)
            url, branch, sub = (m.group(1), m.group(2), m.group(3).strip("/")) if m else (src, None, "")
            self.emit("step", step="clone", message=f"Cloning {url}" + (f" ({sub})" if sub else ""))
            clone_dir = self.dir / "clone"
            if clone_dir.exists():
                shutil.rmtree(clone_dir, ignore_errors=True)
            cmd = ["git", "clone", "--depth", "1", "--quiet"] + (["--branch", branch] if branch else []) + [url, str(clone_dir)]
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
            commit = subprocess.run(["git", "-C", str(clone_dir), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
            self.state["commit"] = commit
            if sub:
                self.state["source_subdir"] = sub
            base = (clone_dir / sub).resolve() if sub else clone_dir
            if not str(base).startswith(str(clone_dir.resolve())) or not base.is_dir():
                raise FileNotFoundError(f"{sub} not found in {url}")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(base, dest, ignore=shutil.ignore_patterns(".git"))
            shutil.rmtree(clone_dir, ignore_errors=True)
        else:
            path = Path(src).expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(f"{src} is not a directory or git URL")
            self.emit("step", step="copy", message=f"Copying {path.name}")
            co = git_checkout(path)
            if co:
                # remembered so `switchyard apply` needs no arguments
                self.state["checkout"] = co
                self.state["commit"] = co["commit"]
            files = _git_files(path) if co else None
            if files:   # an empty list means the folder itself is gitignored: copy it as a plain folder
                # only what git would commit: no gitignored .env files, data dumps or build output
                for rel in files:
                    target = dest / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(path / rel, target)
                    except OSError:
                        pass
            else:
                shutil.copytree(path, dest, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", ".env"))
        return dest

    def run(self) -> dict:
        tf = None
        try:
            tf = TokenFactory(budget_usd=self.budget, tracer=_tracer())
            repo = self.fetch()
            self.repo_dir = repo
            self.emit("step", step="scan", message="Scanning for LLM calls")
            scan = scanner.scan_repo(repo)
            wanted = [c for c in scan["calls"] if self._wanted(c["file"])]
            self.state["skipped"] = [{"file": c["file"], "line": c["line"], "function": c["function"],
                                      "model": c["model"] or c["model_expr"], "reason": c["skip"]}
                                     for c in wanted if c.get("skip")]
            calls = [c for c in wanted if not c.get("skip")][: self.max_sites]
            prod = traces.load(self.traces_path) if self.traces_path else []
            if prod:
                self.emit("step", step="traces", message=f"Loaded {len(prod)} recorded production calls")
            self.state["scan"] = {"files": scan["files"], "loc": scan["loc"], "calls": len(scan["calls"]),
                                  "clients": scan["clients"]}
            self.emit("scan", files=scan["files"], loc=scan["loc"], calls=len(scan["calls"]),
                      sites=[_site_header(c) for c in calls])
            if not calls:
                skipped = len(self.state["skipped"])
                raise RuntimeError("No testable LLM calls found in this repository" +
                                   (f" ({skipped} skipped: models driven by agent frameworks)" if skipped else ""))
            # sites run in parallel (2 at a time) so a run takes minutes, not tens of minutes
            with ThreadPoolExecutor(max_workers=2) as pool:
                for site_state in pool.map(lambda c: self.process_site(tf, c, prod), calls):
                    self.state["sites"].append(site_state)
            self.state["sites"].sort(key=lambda s: (s["site"]["file"], s["site"]["line"]))
            self.emit("step", step="rewrite", message="Writing the migration patch")
            patch = rewrite.apply(repo, self.dir / "migrated", scan, self.state["sites"], run_tests=self.run_tests)
            self.state["patch"] = patch
            self.emit("patch", **{k: v for k, v in patch.items() if k != "diff"})
            if self.execute and patch.get("changed") and patch.get("ok"):
                self.emit("step", step="runtime", message="Running the migrated code against Token Factory")
                patch["runtime"] = runcheck.verify(tf, self.dir / "migrated", self.state["sites"], patch.get("manual"),
                                                   emit=lambda r: self.emit("runtime", **r))
            if self.audit:
                self.state["audit"] = self.run_audit(tf, repo, scan)
            self.state["summary"] = summarize_run(self.state["sites"])
            self.state["status"] = "done"
            self.state["pr_body"] = report.pr_body(self.state)
            (self.dir / "PR.md").write_text(self.state["pr_body"], encoding="utf-8")
        except BudgetExceeded as e:
            self.state["status"] = "budget"
            self.state["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            self.state["status"] = "error"
            self.state["error"] = f"{type(e).__name__}: {e}"
        finally:
            if tf is not None:
                self.state["usage"] = tf.usage.to_dict()
                if getattr(tf, "baseline_usage", None) and tf.baseline_usage.calls:
                    self.state["baseline_usage"] = tf.baseline_usage.to_dict()
                if tf.fallbacks:
                    self.state["notes"] = tf.fallbacks
            self.state["finished"] = time.time()
            self.emit("done", status=self.state["status"], error=self.state.get("error"),
                      summary=self.state.get("summary"), usage=self.state.get("usage"))
        return self.state

    def run_audit(self, tf: TokenFactory, repo: Path, scan: dict) -> dict:
        """Static audit of the whole app (free), then injection probes on each switched call's new model."""
        from . import audit
        self.emit("step", step="audit", message="Auditing the LLM calls (keys, unsafe output use, prompt injection)")
        findings = [f for f in audit.static(repo, scan) if self._wanted(f["file"])]
        migrated = [s["site"] for s in self.state["sites"]
                    if (s.get("decision") or {}).get("action") == "migrate"
                    and [v for v in s["site"].get("variables") or [] if v != "history"]]
        probed, notes = 0, []
        if migrated:
            done = []
            findings += audit.probes(tf, {"calls": migrated}, run_state=self.state, notes=notes,
                                     emit=lambda site, model, rs: (done.append(site["id"]), self.emit(
                                         "probe", id=site["id"], model=model, attacks=len(rs),
                                         hits=sum(1 for r in rs if r["hit"]))))
            probed = len(done)
        note = "; ".join(notes) or None
        findings = audit.sort(findings)
        self.emit("audit", findings=findings, probed=probed)
        return {"findings": findings, "probed": probed, "note": note}

    def _wanted(self, rel: str) -> bool:
        if self.include and not any(fnmatch.fnmatch(rel, g) for g in self.include):
            return False
        return not any(fnmatch.fnmatch(rel, g) for g in self.exclude)

    def process_site(self, tf: TokenFactory, site: dict, prod: list | None = None) -> dict:
        sid = site["id"]
        out: dict = {"site": site, "status": "running"}
        try:
            self.emit("site", id=sid, phase="investigate")
            inv = investigate.investigate(tf, self.repo_dir, site)
            out["investigation"] = inv
            self.emit("investigation", id=sid, mode=inv["mode"], steps=inv["steps"], callers=inv["callers"],
                      downstream=inv["downstream"], critical=inv["critical"], input_examples=inv["input_examples"])
            ctx = investigate.as_context(inv)
            self.emit("site", id=sid, phase="analyze")
            analysis = agents.analyze(tf, site, context=ctx)
            out["analysis"] = analysis
            self.emit("analysis", id=sid, analysis=analysis)

            sources = []
            query = (analysis.get("realistic_input_search") or "").strip()
            if self.use_tavily and query and config.tavily_key():
                sources = tavily_sources(query)
                out["sources"] = [{"title": s.get("title"), "url": s.get("url")} for s in sources]
                self.emit("sources", id=sid, query=query, sources=out["sources"])

            # recorded production calls are the strongest reference: the original model's real answers
            cases = traces.cases_for_site(site, prod or [], limit=self.tests)
            if cases:
                self.emit("step", step="traces", message=f"{sid}: {len(cases)} production calls used as tests")
            if len(cases) < self.tests:
                self.emit("site", id=sid, phase="testgen")
                # a prompt with no runtime values only needs a few samples (it checks consistency)
                want = self.tests - len(cases) if site["variables"] else max(0, min(3, self.tests) - len(cases))
                gen = agents.generate_tests(tf, site, analysis, n=want, sources=sources, context=ctx) if want else []
                if want and len(gen) < (want + 1) // 2:
                    # long inputs (whole documents) can crowd out the rest of the batch: ask once more for the gap
                    gen += agents.generate_tests(tf, site, analysis, n=want - len(gen), sources=sources, context=ctx)
                    gen = gen[:want]
                for c in gen:
                    c["id"] = len(cases) + 1
                    cases.append(c)
            out["cases"] = cases
            self.emit("cases", id=sid, cases=[{k: c[k] for k in ("id", "name", "kind", "why")} for c in cases])

            evals: dict[str, dict] = {}

            def run_model(mk: str):
                self.emit("site", id=sid, phase=f"eval:{mk}")
                res = evaluate.evaluate_model(tf, site, analysis, cases, mk,
                                              on_result=lambda r: self.emit("result", id=sid, model=mk, case=r["case"],
                                                                            passed=r["passed"], score=r["score"]))
                self.emit("model", id=sid, model=mk, **_brief(res))
                return mk, res

            keys = [m.key for m in config.CANDIDATES]
            source_key = baseline_key(tf, site)
            if source_key:
                keys.append(source_key)  # the original model on the same tests: a baseline, never a candidate
            with ThreadPoolExecutor(max_workers=len(keys)) as pool:
                for mk, res in pool.map(run_model, keys):
                    evals[mk] = res
            disputed = evaluate.disputed_cases(evals)
            if disputed:
                evals = {k: evaluate.exclude(v, disputed) for k, v in evals.items()}
                out["disputed"] = disputed
                self.emit("step", step="disputed", message=f"{sid}: {len(disputed)} test(s) set aside: every model "
                          "gave the same answer against the reference, so the reference is likely wrong")
            out["evals"] = evals

            decision = decide(evals, self.target)
            # cheaper model close to the bar: let Ultra adapt the prompt, then re-test
            if decision.get("repair_candidate"):
                mk = decision["repair_candidate"]
                self.emit("site", id=sid, phase=f"repair:{mk}")
                fails = [r | {"inputs": next(c["inputs"] for c in cases if c["id"] == r["case"]),
                              "reference": next(c["reference"] for c in cases if c["id"] == r["case"])}
                         for r in evals[mk]["results"] if not r["passed"]]
                fix = agents.repair_prompt(tf, site, analysis, fails)
                if fix["hint"]:
                    res = evaluate.exclude(evaluate.evaluate_model(tf, site, analysis, cases, mk, hint=fix["hint"]),
                                           disputed)
                    res["rationale"] = fix["rationale"]
                    evals[f"{mk}+hint"] = res
                    self.emit("model", id=sid, model=f"{mk}+hint", **_brief(res))
                    decision = decide(evals, self.target)
            if source_key:
                decision["vs_source"] = head_to_head(evals, decision, source_key)
            out["decision"] = decision
            out["savings"] = savings(site, evals, decision)
            decision, out["savings"] = never_pricier(site, evals, decision, out["savings"])
            out["decision"] = decision
            out["status"] = "done"
            self.emit("decision", id=sid, decision=decision, savings=out["savings"])
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            out["status"] = "error"
            out["error"] = f"{type(e).__name__}: {e}"
            self.emit("site_error", id=sid, error=out["error"])
        return out


def _write_atomic(path: Path, data: str) -> None:
    """Replace `path` without readers ever seeing half a file.

    On Windows the swap fails while another process holds the file open (the web app
    reading it, OneDrive syncing, antivirus), so retry briefly, then write in place.
    Saving progress must never crash a run.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        for delay in (0, 0.05, 0.1, 0.2, 0.4):
            time.sleep(delay)
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                continue
        path.write_text(data, encoding="utf-8")
    except OSError:
        pass  # the in-memory state is still complete; the next save will catch up
    finally:
        tmp.unlink(missing_ok=True)


def baseline_key(tf, site: dict) -> str | None:
    """`source:<model>` when the original model can be run for comparison (OpenAI SDK, literal model, key set)."""
    if getattr(tf, "baseline", None) is None or not site.get("model"):
        return None
    openai_model = re.match(r"^(gpt-|o\d|chatgpt-)", site["model"]) is not None
    if site["sdk"] in ("openai", "openai-responses", "langchain-openai", "js-openai", "js-openai-responses") or             (site["sdk"] in ("litellm", "langchain-init", "js-ai-sdk") and openai_model):
        return f"{config.SOURCE_PREFIX}{site['model']}"
    return None


def head_to_head(evals: dict, decision: dict, source_key: str) -> dict:
    src = evals[source_key]
    out = {"model": source_key[len(config.SOURCE_PREFIX):], "pass_rate": src["pass_rate"], "score": src["score"],
           "errors": src["errors"]}
    # an errored call scores 0: with errors (bad key, unknown model, unsupported param) the
    # comparison would flatter Nemotron, so it isn't reported as a head-to-head
    if decision.get("choice") in evals and not src["errors"]:
        mine = evals[decision["choice"]]
        out.update(chosen_pass_rate=mine["pass_rate"], chosen_score=mine["score"],
                   delta=round(mine["score"] - src["score"], 3))
    return out


def _site_header(c: dict) -> dict:
    return {"id": c["id"], "file": c["file"], "line": c["line"], "function": c["function"],
            "model": c["model"] or c["model_expr"], "sdk": c["sdk"], "variables": c["variables"]}


def _brief(res: dict) -> dict:
    return {k: res[k] for k in ("pass_rate", "score", "latency_p50", "cost", "errors", "hint")}


def decide(evals: dict[str, dict], target: float) -> dict:
    """Cheapest model (optionally with an adapted prompt) that clears the bar.

    The bar: pass rate >= target AND mean score within 0.05 of the best model tested,
    so a cheap model never wins just by squeaking past a low bar. When the app's original
    model was run on the same tests (`source:<id>`), it counts toward "best": a switch must
    be about as good as what the app uses today. The original is never chosen itself.
    """
    order = []
    for m in config.CANDIDATES:
        for variant, key in ((0, m.key), (1, f"{m.key}+hint")):
            if key in evals:
                order.append((m.tier, variant, key, evals[key]))
    order.sort(key=lambda x: (x[0], x[1]))
    # a baseline with a stray failed request (e.g. one 429) still counts; a mostly broken one doesn't
    sources = [e for k, e in evals.items() if k.startswith(config.SOURCE_PREFIX)
               and e.get("errors", 0) <= len(e.get("results") or []) // 4]
    best_score = max([e["score"] for *_, e in order] + [e["score"] for e in sources], default=0)
    tried_repair = any(v == 1 for _t, v, _k, _e in order)
    out = None
    for tier, _variant, key, e in order:
        if e["pass_rate"] >= target and e["score"] >= best_score - 0.05:
            out = {"action": "migrate", "choice": key, "model": key.split("+")[0], "hint": e.get("hint", ""),
                   "pass_rate": e["pass_rate"], "score": e["score"], "tier": tier,
                   "reason": f"cheapest model passing {e['pass_rate']:.0%} of tests (bar {target:.0%})"
                             + (" after Ultra adapted the prompt" if e.get("hint") else "")}
            break
    if out is None:
        out = {"action": "keep", "choice": None, "model": None, "hint": "", "tier": 99,
               "reason": f"no Nemotron model reached {target:.0%} on these tests; keep the original model for this call"}
    # A cheaper model that came close is worth one prompt-repair attempt, even when a
    # pricier model already passes: that's where most of the savings are.
    if not tried_repair:
        for tier, variant, key, e in order:
            if variant == 0 and tier < min(out["tier"], 3) and (e["pass_rate"] >= 0.25 or e["score"] >= 0.4):
                out["repair_candidate"] = key
                break
    out.pop("tier", None)
    return out


def savings(site: dict, evals: dict, decision: dict) -> dict:
    pin, pout, matched = config.source_price(site.get("model"))
    if decision["action"] != "migrate":
        return {"source_model": site.get("model"), "source_price_key": matched, "saving_pct": 0}
    e = evals[decision["choice"]]
    n = max(1, len(e["results"]))
    tin, tout = e["tokens_in"] / n, e["tokens_out"] / n
    src_cost = tin / 1e6 * pin + tout / 1e6 * pout
    new_cost = config.MODELS[decision["model"]].cost(tin, tout)
    return {"source_model": site.get("model"), "source_price_key": matched,
            "avg_tokens_in": round(tin), "avg_tokens_out": round(tout),
            "source_cost_per_call": src_cost, "new_cost_per_call": new_cost,
            "saving_pct": round((1 - new_cost / src_cost) * 100, 1) if src_cost else 0,
            "latency_p50": e["latency_p50"]}


def never_pricier(site: dict, evals: dict, decision: dict, sav: dict) -> tuple[dict, dict]:
    """A switch has to save money: if the cheapest passing Nemotron model costs more than the call's
    current model (say Ultra vs gpt-4o-mini), keep the original and say why."""
    if decision.get("action") != "migrate" or sav.get("saving_pct", 0) >= 0 or not sav.get("source_cost_per_call"):
        return decision, sav
    label = config.MODELS[decision["model"]].label
    kept = {k: v for k, v in decision.items() if k not in ("choice", "model", "hint", "repair_candidate")}
    kept.update(action="keep", passing=decision["choice"],
                reason=f"{label} passes ({decision['pass_rate']:.0%}) but costs more than {site.get('model')} "
                       f"for this call ({abs(sav['saving_pct']):.0f}% more); keeping the original model")
    return kept, savings(site, evals, kept)


def summarize_run(sites: list[dict]) -> dict:
    done = [s for s in sites if s.get("status") == "done"]
    migrated = [s for s in done if s["decision"]["action"] == "migrate"]
    src = sum(s["savings"].get("source_cost_per_call", 0) for s in migrated)
    new = sum(s["savings"].get("new_cost_per_call", 0) for s in migrated)
    by_model: dict[str, int] = {}
    for s in migrated:
        by_model[s["decision"]["model"]] = by_model.get(s["decision"]["model"], 0) + 1
    return {"sites": len(sites), "migrated": len(migrated), "kept": len(done) - len(migrated),
            "errors": len(sites) - len(done), "by_model": by_model,
            "repaired": sum(1 for s in migrated if s["decision"].get("hint")),
            "cost_per_round_source": src, "cost_per_round_new": new,
            "saving_pct": round((1 - new / src) * 100, 1) if src else 0}


def tavily_sources(query: str) -> list[dict]:
    try:
        from tavily import TavilyClient
        res = TavilyClient(api_key=config.tavily_key()).search(query, max_results=5, search_depth="basic")
        return [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content")} for r in res.get("results", [])]
    except Exception:  # noqa: BLE001 - realistic inputs are a bonus, never a blocker
        return []


def _tracer():
    """Wrap the OpenAI client with LangSmith tracing when LANGSMITH_API_KEY is set."""
    import os
    if not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        return None
    try:
        from langsmith.wrappers import wrap_openai
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        return wrap_openai
    except ImportError:
        return None
