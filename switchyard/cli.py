"""Switchyard: move LLM calls to NVIDIA Nemotron on Nebius Token Factory, with proof.

  switchyard scan   <repo>                 list LLM call sites (no API calls)
  switchyard run    <repo | git url>       test every call site on Nemotron and write a patch
  switchyard check  <run> --repo .         CI guard: re-run saved tests, fail on drift
  switchyard apply  <run> <checkout>       apply the patch on a new branch in your repo
  switchyard pr     <run> <checkout>       apply + commit + push + open a GitHub PR (gh CLI)
  switchyard traces --langsmith <project>  export recorded calls to use as references
  switchyard review <run>                  human check of the grader (Toloka-ready TSV)
  switchyard publish <run>                 feature a run in the web app + write README results
  switchyard serve                         web app
  switchyard models                        check Token Factory access
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
    elif k == "done":
        print(f"{t} {ev['status']}" + (f": {ev['error']}" if ev.get("error") else ""))
        if ev.get("usage"):
            print(f"         Token Factory spend for this run: ${ev['usage']['cost']:.4f} over {ev['usage']['calls']} calls")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="switchyard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="list LLM call sites (no API calls)")
    s.add_argument("repo")
    s.add_argument("--json", action="store_true", help="full machine-readable output")

    r = sub.add_parser("run", help="full migration run")
    r.add_argument("repo", help="local path, git URL, or GitHub folder URL (.../tree/<branch>/<dir>)")
    r.add_argument("--tests", type=int, default=8, help="tests per call site (default 8)")
    r.add_argument("--target", type=float, default=0.85, help="pass rate a model must reach (default 0.85)")
    r.add_argument("--budget", type=float, default=2.0, help="max Token Factory spend in USD (default 2)")
    r.add_argument("--max-sites", type=int, default=12)
    r.add_argument("--no-tavily", action="store_true", help="don't fetch real-world inputs with Tavily")
    r.add_argument("--include", action="append", default=[], metavar="GLOB", help="only scan matching files (repeatable)")
    r.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip matching files (repeatable)")
    r.add_argument("--traces", metavar="JSONL", help="recorded production calls to use as reference answers")
    r.add_argument("--run-tests", action="store_true", help="run the project's pytest suite on the migrated copy")

    c = sub.add_parser("check", help="re-run a migration's saved tests (CI regression guard)")
    c.add_argument("run", help="run id or runs/<id> directory")
    c.add_argument("--repo", help="re-scan this checkout so prompt edits are tested too")
    c.add_argument("--target", type=float)
    c.add_argument("--budget", type=float, default=1.0)

    a = sub.add_parser("apply", help="apply a run's patch on a new branch in your checkout (no commit)")
    a.add_argument("run")
    a.add_argument("repo")
    a.add_argument("--branch")
    a.add_argument("--subdir", default="", help="folder the run scanned, relative to the checkout root")

    pr = sub.add_parser("pr", help="apply + commit + push + open a pull request with the report")
    pr.add_argument("run")
    pr.add_argument("repo")
    pr.add_argument("--base")
    pr.add_argument("--subdir", default="")

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

    v = sub.add_parser("serve", help="start the web app")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)

    sub.add_parser("models", help="check Token Factory access and list Nemotron models")
    return ap


def main(argv: list[str] | None = None) -> int:
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
        print(f"{len(res['calls'])} LLM call site(s) in {res['files']} Python files ({res['loc']} lines)\n")
        for c in res["calls"]:
            schema = (c["params"].get("output_schema") or {}).get("name")
            extra = f"  -> {schema}" if schema else ""
            print(f"  {c['file']}:{c['line']:<5} {c['function'] or '(module)':<28} {c['model'] or c['model_expr'] or '?':<14}"
                  f" vars={','.join(c['variables']) or '-'}{extra}")
        return 0

    if args.cmd == "run":
        from .pipeline import Run
        run = Run(args.repo, tests=args.tests, target=args.target, budget=args.budget,
                  use_tavily=not args.no_tavily, max_sites=args.max_sites, emit=_print_event,
                  include=args.include, exclude=args.exclude, traces_path=args.traces, run_tests=args.run_tests)
        state = run.run()
        print(f"\nRun saved to {run.dir}")
        if state.get("patch") and state["patch"].get("changed"):
            print(f"Patch:   {run.dir / 'migration.patch'}")
            print(f"Report:  {run.dir / 'PR.md'}")
            print(f"Next:    switchyard apply {run.id} <your checkout>    (or `switchyard pr ...`)")
        return 0 if state["status"] == "done" else 1

    if args.cmd == "check":
        from .check import check
        res = check(args.run, repo=args.repo, target=args.target, budget=args.budget)
        bad = sum(1 for r in res["results"] if r["status"] != "ok")
        print(f"\n{len(res['results']) - bad}/{len(res['results'])} switched call sites still pass "
              f"(bar {res['target']:.0%}); spent ${res['usage']['cost']:.4f}")
        return 0 if res["ok"] else 1

    if args.cmd == "apply":
        from .gitops import apply
        res = apply(args.run, args.repo, branch=args.branch, subdir=args.subdir)
        print(f"Applied on branch {res['branch']}: {', '.join(res['changed'])}")
        print(f"Review with `git diff`, copy the env vars from .env.switchyard.example, then commit.\nPR text: {res['pr_body']}")
        return 0

    if args.cmd == "pr":
        from .gitops import open_pr
        print(open_pr(args.run, args.repo, base=args.base, subdir=args.subdir))
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


if __name__ == "__main__":
    sys.exit(main())
