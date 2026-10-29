"""Switchyard: move LLM calls to NVIDIA Nemotron on Nebius Token Factory, with proof.

Quick start (inside your app's folder):
  switchyard setup                         paste your Token Factory key once
  switchyard run                           test every LLM call on Nemotron, then apply or open a PR

All commands:
  switchyard scan   [path]                 list LLM call sites (no API calls, no key needed)
  switchyard run    [path | git url]       test every call site on Nemotron and write a patch
  switchyard apply  [run] [checkout]       apply the latest patch on a new branch (no commit)
  switchyard pr     [run] [checkout]       apply + commit + push + open a GitHub PR (gh CLI)
  switchyard check  [run] --repo .         CI guard: re-run saved tests, fail on drift
  switchyard show   [run]                  what a run switched, kept, spent and verified
  switchyard audit  [path] [--probes]      security/reliability findings per LLM call (SARIF for GitHub)
  switchyard mcp                           MCP server for Cursor, Claude Desktop and other agents
  switchyard ci     [path]                 add GitHub workflows (audit on PRs; check after a migration)
  switchyard traces --langsmith <project>  export recorded calls to use as references
  switchyard review <run>                  human check of the grader (Toloka-ready TSV)
  switchyard publish <run>                 feature a run in the web app + write README results
  switchyard serve                         web app
  switchyard models                        check Token Factory access
  switchyard doctor                        probe which API features each Nemotron model supports
  switchyard setup                         save your keys to ~/.switchyard/.env
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, scanner


def _print_event(ev: dict) -> None:
    k = ev["kind"]
    t = f"[{ev['t']:6.1f}s]"
    if k == "step":
        print(f"{t} {ev['message']}")
    elif k == "scan":
        print(f"{t} {ev['calls']} LLM call(s) in {ev['files']} files ({ev['loc']} lines)")
        for s in ev["sites"]:
            print(f"         - {s['file']}:{s['line']} {s['function'] or ''}  [{s['model']}]")
    elif k == "analysis":
        a = ev["analysis"]
        print(f"{t} {ev['id']} is a {a.get('task_type')} task: {a.get('summary')}")
    elif k == "sources":
        print(f"{t} {ev['id']} Tavily found {len(ev['sources'])} real inputs for '{ev['query']}'")
    elif k == "cases":
        print(f"{t} {ev['id']} wrote {len(ev['cases'])} tests")
    elif k == "model":
        print(f"{t} {ev['id']} {ev['model']:<16} pass {ev['pass_rate']:.0%}  score {ev['score']:.2f}  p50 {ev['latency_p50']:.2f}s")
    elif k == "decision":
        d, s = ev["decision"], ev["savings"]
        if d["action"] == "migrate":
            print(f"{t} {ev['id']} -> {d['choice']}  ({d['reason']}; {s.get('saving_pct', 0)}% cheaper than {s.get('source_model')})")
        else:
            print(f"{t} {ev['id']} -> keep original ({d['reason']})")
    elif k == "site_error":
        print(f"{t} {ev['id']} error: {ev['error']}")
    elif k == "patch":
        print(f"{t} patch ({ev.get('mode')}): {len(ev['changed'])} file(s), compile ok={ev['ok']}")
    elif k == "runtime":
        icon = {"verified": "ok  ", "failed": "FAIL", "unverified": "??  "}.get(ev["status"], "skip")
        print(f"{t} [{icon}] ran {ev['file']}:{ev['line']} {ev.get('function') or ''}: {ev.get('reason', '')}")
    elif k == "probe":
        print(f"{t} {ev['id']} probed on {ev['model']}: {ev['attacks']} injection/leak/exfiltration attacks, "
              f"{ev['hits']} got through")
    elif k == "audit":
        f = ev["findings"]
        counts = {sev: sum(1 for x in f if x["severity"] == sev) for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
        print(f"{t} audit: " + (", ".join(f"{n} {sev.lower()}" for sev, n in counts.items() if n) or "no findings")
              + "  (details: switchyard audit)")
    elif k == "done":
        print(f"{t} {ev['status']}" + (f": {ev['error']}" if ev.get("error") else ""))
        if ev.get("usage"):
            print(f"         Token Factory spend for this run: ${ev['usage']['cost']:.4f} over {ev['usage']['calls']} calls")


QUICKSTART = """Switchyard moves your app's LLM calls to NVIDIA Nemotron on Nebius Token Factory,
and proves each switch with tests before changing any code.

  1. switchyard setup      paste your Token Factory key (once per machine)
  2. switchyard run        in your app's folder: find calls, test them, write a patch
  3. switchyard apply      put the patch on a new git branch (or: switchyard run --pr)

Free first look, no key needed:  switchyard run --dry-run
Is it safe to ship? (free):      switchyard audit
All commands:                    switchyard --help"""


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    ap = argparse.ArgumentParser(prog="switchyard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"switchyard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    au = sub.add_parser("audit", help="security and reliability audit of every LLM call (static part is free)")
    au.add_argument("repo", nargs="?", default=".")
    au.add_argument("--probes", action="store_true",
                    help="also attack each prompt with injection / leak / exfiltration probes on Nemotron (a few cents)")
    au.add_argument("--model", default="lightning", choices=[m.key for m in config.CANDIDATES],
                    help="model to probe when a call hasn't been migrated (default lightning)")
    au.add_argument("--format", choices=["text", "md", "json", "sarif"], default="text")
    au.add_argument("--out", help="write the report to this file (SARIF works with GitHub code scanning)")
    au.add_argument("--fail-on", choices=["critical", "high", "medium", "low", "never"], default="high",
                    help="exit 1 when a finding is at least this severe (default high)")
    au.add_argument("--budget", type=float, default=0.5)
    au.add_argument("-y", "--yes", action="store_true", help="don't ask before running probes")

    ci = sub.add_parser("ci", help="add GitHub workflows: audit on every PR (+ the check guard after a migration)")
    ci.add_argument("path", nargs="?", default=".")

    sub.add_parser("mcp", help="run as an MCP server (stdio) for Cursor, Claude Desktop and other MCP clients")

    sh = sub.add_parser("show", help="results of a run: what switched, what stayed, cost, verification")
    sh.add_argument("run", nargs="?", default="latest", help="run id or folder (default: this checkout's latest)")
    sh.add_argument("--json", action="store_true", help="machine-readable summary")

    s = sub.add_parser("scan", help="list LLM call sites (no API calls)")
    s.add_argument("repo", nargs="?", default=".")
    s.add_argument("--json", action="store_true", help="full machine-readable output")

    r = sub.add_parser("run", help="full migration run")
    r.add_argument("repo", nargs="?", default=".",
                   help="local path (default: this folder), git URL, or GitHub folder URL (.../tree/<branch>/<dir>)")
    r.add_argument("--tests", type=int, default=8, help="tests per call site (default 8)")
    r.add_argument("--target", type=float, default=0.85, help="pass rate a model must reach (default 0.85)")
    r.add_argument("--budget", type=float, default=2.0, help="max Token Factory spend in USD (default 2)")
    r.add_argument("--max-sites", type=int, default=12)
    r.add_argument("--no-tavily", action="store_true", help="don't fetch real-world inputs with Tavily")
    r.add_argument("--include", action="append", default=[], metavar="GLOB", help="only scan matching files (repeatable)")
    r.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip matching files (repeatable)")
    r.add_argument("--traces", metavar="JSONL", help="recorded production calls to use as reference answers")
    r.add_argument("--run-tests", action="store_true", help="run the project's pytest suite on the migrated copy")
    r.add_argument("--no-exec", action="store_true",
                   help="don't run the migrated code (by default each switched function is called once against Token Factory)")
    r.add_argument("-y", "--yes", action="store_true", help="don't ask before spending")
    r.add_argument("--apply", action="store_true", help="when done, apply the patch on a new branch of your checkout")
    r.add_argument("--pr", action="store_true", help="when done, apply, push and open a pull request (gh CLI)")
    r.add_argument("--dry-run", action="store_true", help="scan and estimate cost, then stop (no key needed)")
    r.add_argument("--no-ci", action="store_true", help="with --apply/--pr: don't add the CI workflow file")
    r.add_argument("--no-audit", action="store_true", help="skip the security audit and injection probes")

    c = sub.add_parser("check", help="re-run a migration's saved tests (CI regression guard)")
    c.add_argument("run", nargs="?", help="run id, run folder or run.json (default: .switchyard/run.json, else latest)")
    c.add_argument("--repo", help="re-scan this checkout so prompt edits are tested too")
    c.add_argument("--target", type=float)
    c.add_argument("--budget", type=float, default=1.0)

    a = sub.add_parser("apply", help="apply a run's patch on a new branch in your checkout (no commit)")
    a.add_argument("run", nargs="?", default="latest", help="run id or folder (default: latest)")
    a.add_argument("repo", nargs="?", help="your checkout (default: the one the run scanned)")
    a.add_argument("--branch")
    a.add_argument("--subdir", help="folder the run scanned, relative to the checkout root (usually detected)")
    a.add_argument("--no-ci", action="store_true", help="don't add .github/workflows/switchyard-check.yml")

    pr = sub.add_parser("pr", help="apply + commit + push + open a pull request with the report")
    pr.add_argument("run", nargs="?", default="latest")
    pr.add_argument("repo", nargs="?")
    pr.add_argument("--base")
    pr.add_argument("--subdir")
    pr.add_argument("--no-ci", action="store_true")

    tr = sub.add_parser("traces", help="export recorded LLM calls for --traces")
    tr.add_argument("--langsmith", required=True, metavar="PROJECT")
    tr.add_argument("--out", default="traces.jsonl")
    tr.add_argument("--limit", type=int, default=500)

    rv = sub.add_parser("review", help="export graded answers for human raters, or import their verdicts")
    rv.add_argument("run")
    rv.add_argument("--out", default="review.tsv")
    rv.add_argument("--per-site", type=int, default=4)
    rv.add_argument("--import", dest="import_path", metavar="FILE", help="rater results (INPUT:item_id, OUTPUT:verdict)")

    pb = sub.add_parser("publish", help="feature a finished run in the web app and write its results into the README")
    pb.add_argument("run")
    pb.add_argument("--no-readme", action="store_true", help="don't touch README.md")
    pb.add_argument("--allow-simulated", action="store_true", help=argparse.SUPPRESS)

    dr = sub.add_parser("doctor", help="probe chat / JSON / schema / tools / thinking / Responses API per model")
    dr.add_argument("--out", help="also write the Markdown table to this file")

    v = sub.add_parser("serve", help="start the web app")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)

    sub.add_parser("models", help="check Token Factory access and list Nemotron models")

    su = sub.add_parser("setup", aliases=["login"], help="save your Token Factory key (and optional Tavily/OpenAI keys)")
    su.add_argument("--key", help="NEBIUS_API_KEY (prompted for if omitted)")
    su.add_argument("--tavily", help="TAVILY_API_KEY")
    su.add_argument("--openai", help="OPENAI_API_KEY, to score the app's current model on the same tests")
    su.add_argument("--no-check", action="store_true", help="save without calling Token Factory")
    return ap


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; never crash on a ✓ or → in output
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(QUICKSTART)
        return 0
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _dispatch(args) -> int:
    if args.cmd == "scan":
        res = scanner.scan_repo(args.repo)
        if args.json:
            print(json.dumps(res, indent=2, default=str))
            return 0
        print(f"{len(res['calls'])} LLM call site(s) in {res['files']} source files ({res['loc']} lines)\n")
        for c in res["calls"]:
            schema = (c["params"].get("output_schema") or {}).get("name")
            extra = f"  -> {schema}" if schema else ""
            print(f"  {c['file']}:{c['line']:<5} {c['function'] or '(module)':<28} {c['model'] or c['model_expr'] or '?':<14}"
                  f" vars={','.join(c['variables']) or '-'}{extra}")
        return 0

    if args.cmd in ("setup", "login"):
        from . import onboard
        onboard.setup(args.key, args.tavily, args.openai, check=not args.no_check)
        return 0

    if args.cmd == "run":
        return _run(args)

    if args.cmd == "show":
        return _show(args)

    if args.cmd == "audit":
        return _audit(args)

    if args.cmd == "ci":
        from .gitops import add_ci
        written = add_ci(args.path)
        if not written:
            print("Workflows already present; nothing to add.")
            return 0
        for w in written:
            print(f"added {w}")
        if any("switchyard-check" in w for w in written):
            print("The check workflow needs a NEBIUS_API_KEY repository secret (Settings -> Secrets -> Actions).")
        print("Commit them to turn them on.")
        return 0

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0

    if args.cmd == "check":
        from .check import check
        ref = args.run or _saved_run_file()
        res = check(ref, repo=args.repo, target=args.target, budget=args.budget)
        bad = sum(1 for r in res["results"] if r["status"] != "ok")
        _step_summary(_check_markdown(res))
        print(f"\n{len(res['results']) - bad}/{len(res['results'])} switched call sites still pass "
              f"(bar {res['target']:.0%}); spent ${res['usage']['cost']:.4f}")
        return 0 if res["ok"] else 1

    if args.cmd == "apply":
        from .gitops import apply
        res = apply(args.run, args.repo, branch=args.branch, subdir=args.subdir, ci=not args.no_ci)
        _print_applied(res)
        return 0

    if args.cmd == "pr":
        from .gitops import open_pr
        print(open_pr(args.run, args.repo, base=args.base, subdir=args.subdir, ci=not args.no_ci))
        return 0

    if args.cmd == "traces":
        from .traces import from_langsmith
        n = from_langsmith(args.langsmith, args.out, limit=args.limit)
        print(f"Wrote {n} LLM calls to {args.out}. Use: switchyard run <repo> --traces {args.out}")
        return 0

    if args.cmd == "review":
        from . import review
        if args.import_path:
            res = review.import_results(args.run, args.import_path)
            if res["agreement"] is None:
                print("no matching verdicts found")
                return 1
            print(f"Grader agrees with human raters on {res['agreement']:.0%} of {res['items']} items")
            return 0
        n = review.export(args.run, args.out, per_site=args.per_site)
        print(f"Wrote {n} items to {args.out} (TSV with INPUT:/GOLDEN: columns; upload to Toloka or any rating tool).")
        print(f"Raters answer OUTPUT:verdict yes/no. Then: switchyard review {args.run} --import results.tsv")
        return 0

    if args.cmd == "publish":
        from .publish import publish
        res = publish(args.run, write_readme=not args.no_readme, allow_simulated=args.allow_simulated)
        h = res["headline"]
        print(f"Featured in the web app: {res['dest']}")
        if res["readme"]:
            print(f"README Results section updated: {res['readme']}")
        print(f"\nHeadline: {h['migrated']}/{h['sites']} calls switched, {h['saving_pct']}% cheaper per request, "
              f"{h['repaired']} repaired, analysis cost ${h['run_cost']:.2f} over {h['run_calls']} Token Factory calls.\n")
        print(res["markdown"])
        return 0

    if args.cmd == "doctor":
        from . import doctor
        print(f"Probing {len(config.CANDIDATES)} models on {config.DEFAULT_BASE_URL} ...")
        md = doctor.markdown(doctor.run())
        print(md)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md + "\n")
        return 0

    if args.cmd == "serve":
        import uvicorn
        print(f"Switchyard on http://{args.host}:{args.port}")
        uvicorn.run("switchyard.server:app", host=args.host, port=args.port)
        return 0

    if args.cmd == "models":
        from openai import OpenAI
        key = config.api_key()
        if not key:
            print("NEBIUS_API_KEY is not set (add it to .env)")
            return 1
        client = OpenAI(base_url=config.DEFAULT_BASE_URL, api_key=key)
        ids = sorted(m.id for m in client.models.list().data)
        nemo = [i for i in ids if "nemotron" in i.lower()]
        print(f"{len(ids)} models on {config.DEFAULT_BASE_URL}; Nemotron:")
        used = {m.id for m in config.CANDIDATES}
        for i in nemo:
            print(f"  {i}{'  <- used' if i in used else ''}")
        missing = [m.id for m in config.CANDIDATES if m.id not in ids]
        if missing:
            print("Missing (set MODEL_LIGHTNING / MODEL_SUPER / MODEL_ULTRA):", missing)
            return 1
        return 0
    return 1


def _saved_run_file() -> str:
    """The tests `apply` saved for the app in this folder, else this checkout's latest run."""
    from pathlib import Path

    from .gitops import _latest_for, ci_paths, toplevel
    top = toplevel(".")
    if top:
        rel = Path(".").resolve().relative_to(top).as_posix()
        for sub in ([] if rel == "." else [rel]) + [""]:
            p = top / ci_paths(sub)[0]
            if p.exists():
                return str(p)
    elif Path(".switchyard/run.json").exists():
        return ".switchyard/run.json"
    return _latest_for(Path("."))


def _print_applied(res: dict) -> None:
    print(f"\nApplied on new branch {res['branch']} in {res['root']}:")
    for c in res["changed"]:
        print(f"  {c}")
    ci = (f"; CI re-runs them via {res['workflow']} (add a NEBIUS_API_KEY repo secret)" if res.get("workflow")
          else "; re-test any time with `switchyard check`")
    print("\nNext:\n  git diff                      review the change"
          "\n  .env.switchyard.example       the env vars the patched code reads (NEBIUS_API_KEY, model ids)"
          f"\n  {res['run_file']:<29} the saved tests{ci}"
          f"\n  PR description:               {res['pr_body']}")
    if res.get("traces"):
        print(f"\nNote: this run used recorded production calls as tests, so {res['run_file']} contains those\n"
              "inputs. Review it before committing, or leave it out and keep the run locally.")


def _run(args) -> int:
    from . import onboard
    from .pipeline import Run, estimate_cost, git_checkout

    src = args.repo
    local = not src.startswith(("http://", "https://", "git@"))
    if local:
        import fnmatch
        res = scanner.scan_repo(src)
        wanted = [c for c in res["calls"]
                  if (not args.include or any(fnmatch.fnmatch(c["file"], g) for g in args.include))
                  and not any(fnmatch.fnmatch(c["file"], g) for g in args.exclude)]
        testable = [c for c in wanted if not c.get("skip")]
        calls = testable[: args.max_sites]
        if not wanted:
            print(f"No OpenAI, Anthropic, LiteLLM or LangChain calls found in {res['root']} ({res['files']} Python/JS/TS files).")
            return 1
        print(f"Found {len(wanted)} LLM call(s) in {res['files']} source files:")
        for c in calls:
            print(f"  {c['file']}:{c['line']:<5} {c['function'] or '(module)':<28} {c['model'] or c['model_expr'] or '?'}")
        if len(testable) > len(calls):
            print(f"  ... testing the first {len(calls)} (raise with --max-sites)")
        for c in wanted:
            if c.get("skip"):
                print(f"  skip  {c['file']}:{c['line']} {c['function'] or ''}: {c['skip']}")
        if not calls:
            print("\nNothing Switchyard can test with single prompts here.")
            return 1
        est = estimate_cost(calls, args.tests)
        print(f"\nTesting them on Nemotron costs about ${est:.2f} on Token Factory "
              f"({args.tests} tests per call; hard cap ${args.budget:.2f}, set with --budget).")
        co = git_checkout(src)
        if co and co["dirty"]:
            print("Note: this folder has uncommitted changes. The patch is made from them; "
                  "commit before `switchyard apply`.")
        if args.dry_run:
            return 0
    elif args.dry_run:
        print("--dry-run works on a local folder; clone the repository first.")
        return 2
    if not onboard.ensure_key():
        return 2
    if not args.yes:
        if not onboard.interactive():
            # never spend without a yes: piped output, CI, or a console Python can't prompt in
            print("Add --yes to start the run (nothing was spent).")
            return 2
        if not onboard.ask("Start?"):
            return 0

    run = Run(src, tests=args.tests, target=args.target, budget=args.budget,
              use_tavily=not args.no_tavily, max_sites=args.max_sites, emit=_print_event,
              include=args.include, exclude=args.exclude, traces_path=args.traces, run_tests=args.run_tests,
              execute=False if args.no_exec else None, audit=not args.no_audit)
    state = run.run()
    print(f"\nRun saved to {run.dir}")
    if state["status"] != "done":
        return 1
    _print_summary(state)
    if not (state.get("patch") or {}).get("changed"):
        print("Nothing to change: no call site met the bar on a cheaper Nemotron model.")
        return 0
    print(f"Patch:   {run.dir / 'migration.patch'}\nReport:  {run.dir / 'PR.md'}")
    _step_summary(state.get("pr_body", ""))
    from .gitops import apply, open_pr
    if not state.get("checkout"):
        # a URL run: the patch is for whoever has a clone
        print(f"Next:    switchyard apply {run.id} <path to your clone>   (or `switchyard pr ...`)")
        return 0
    ci = not args.no_ci
    if args.pr:
        print(f"\nPull request: {open_pr(run.id, ci=ci)}")
    elif args.apply or (onboard.interactive() and not state["checkout"].get("dirty")
                        and onboard.ask("\nApply the patch on a new branch of your checkout?")):
        _print_applied(apply(run.id, ci=ci))
    else:
        print("Next:    switchyard apply      (or `switchyard pr` to open a pull request)")
    return 0


def _step_summary(md: str) -> None:
    """On GitHub Actions, show results on the job's summary page."""
    import os
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and md:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md[:900_000] + "\n")


def _check_markdown(res: dict) -> str:
    rows = ["## Switchyard check", "",
            f"Bar: {res['target']:.0%} of saved tests per switched call. "
            f"Spent ${res['usage']['cost']:.4f} on Token Factory.", "",
            "| Call | Model | Pass rate | At migration | |", "|---|---|---|---|---|"]
    for r in res["results"]:
        if r["status"] == "missing":
            rows.append(f"| `{r['site']}` {r['function'] or ''} | | | | ❌ call site no longer found |")
            continue
        icon = "✅" if r["status"] == "ok" else "❌"
        note = " prompt changed" if r.get("prompt_changed") else ""
        rows.append(f"| `{r['site']}` {r['function'] or ''} | {r['model']} | {r['pass_rate']:.0%} | {r['was']:.0%} | {icon}{note} |")
        for f in r.get("failures", [])[:3]:
            rows.append(f"| | | | | case {f['case']}: {str(f['reason'])[:120]} |")
    return "\n".join(rows)


def _audit(args) -> int:
    from pathlib import Path

    from . import audit, onboard
    res = scanner.scan_repo(args.repo)
    log = (lambda *a: print(*a, file=sys.stderr)) if args.format != "text" or args.out else print
    log(f"Auditing {len(res['calls'])} LLM call(s) in {res['files']} source files")
    findings = audit.static(args.repo, res)
    if args.probes:
        n = sum(1 for c in res["calls"] if [v for v in c["variables"] if v != "history"])
        if not n:
            log("No call takes user-controlled input; nothing to probe.")
        elif onboard.ensure_key(out=log):
            if not args.yes and not (onboard.interactive() and onboard.ask(
                    f"Probe {n} call(s) with injection, leak and exfiltration attacks on Token Factory "
                    f"(about ${0.002 * n:.2f}, cap ${args.budget:.2f})?")):
                log("Probes skipped (add --yes to run them without asking).")
            else:
                from .llm import TokenFactory
                state = None
                try:
                    from .check import load_run
                    from .gitops import _latest_for
                    state, _ = load_run(_latest_for(Path(args.repo)))
                except (FileNotFoundError, RuntimeError, ValueError):
                    pass
                tf = TokenFactory(budget_usd=args.budget)

                def progress(site, model, rs):
                    hits = sum(1 for r in rs if r["hit"])
                    log(f"  probed {site['file']}:{site['line']} {site.get('function') or ''} on {model}: "
                        f"{len(rs)} attacks, {hits} got through")
                notes: list = []
                findings = audit.sort(findings + audit.probes(tf, res, args.model, state, emit=progress, notes=notes))
                for n_ in notes:
                    log(f"  {n_}")
                log(f"  probes cost ${tf.usage.to_dict()['cost']:.4f}")
    from .gitops import toplevel
    top = toplevel(args.repo)
    prefix = Path(args.repo).resolve().relative_to(top).as_posix() if top else ""
    body = {"text": audit.text, "md": audit.markdown, "sarif": lambda f: audit.sarif(f, prefix),
            "json": lambda f: json.dumps(f, indent=1)}[args.format](findings)
    if args.out:
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        log(f"Wrote {len(findings)} finding(s) to {args.out}")
    else:
        print(body)
    _step_summary(audit.markdown(findings))
    if args.fail_on == "never" or not findings:
        return 0
    limit = audit.SEVERITY_ORDER.index(args.fail_on.upper())
    return 1 if audit.SEVERITY_ORDER.index(audit.worst(findings)) <= limit else 0


def _show(args) -> int:
    from pathlib import Path

    from . import Result
    from .check import load_run
    from .gitops import _latest_for
    ref = args.run
    if ref in (None, "", "latest"):
        ref = _latest_for(Path("."))
    state, run_dir = load_run(ref)
    res = Result(state, run_dir)
    runtime = ((state.get("patch") or {}).get("runtime")) or []
    rt = {k: sum(1 for r in runtime if r.get("status") == k) for k in ("verified", "failed", "unverified", "skipped")}
    if args.json:
        print(json.dumps({"id": res.id, "status": state.get("status"), "error": state.get("error"),
                          "summary": res.summary, "decisions": res.decisions, "runtime": rt,
                          "cost_usd": round(res.cost, 4), "patch": str(res.patch_path) if res.patch_path else None,
                          "report": str(run_dir / "PR.md") if (run_dir / "PR.md").exists() else None,
                          "manual": (state.get("patch") or {}).get("manual", [])}, indent=1))
        return 0
    print(f"Run {res.id}: {state.get('status')}" + (f" ({state['error']})" if state.get("error") else "")
          + f", spent ${res.cost:.4f} on Token Factory")
    _print_summary(state)
    if runtime:
        print(f"\nRan the patched code: {rt['verified']} verified, {rt['failed']} failed, {rt['unverified']} unverified")
    for m in (state.get("patch") or {}).get("manual", []):
        print(f"  manual  {m['file']}:{m['line']}  -> {m['model']}  ({m['reason']})")
    if res.patch_path:
        print(f"\nPatch:   {res.patch_path}\nReport:  {run_dir / 'PR.md'}")
    return 0


def _print_summary(state: dict) -> None:
    s = state.get("summary") or {}
    if not s:
        return
    print(f"\n{s.get('migrated', 0)} of {s.get('sites', 0)} call site(s) switched to Nemotron"
          + (f", about {s['saving_pct']}% cheaper per request" if s.get("saving_pct") is not None else ""))
    for rec in state.get("sites", []):
        d, site = rec.get("decision") or {}, rec["site"]
        where = f"{site['file']}:{site['line']} {site.get('function') or ''}"
        if d.get("action") == "migrate":
            print(f"  ok    {where:<42} -> {d['choice']}")
        else:
            print(f"  keep  {where:<42} ({d.get('reason') or rec.get('error') or 'not switched'})")


if __name__ == "__main__":
    sys.exit(main())
