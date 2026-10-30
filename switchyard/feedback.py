"""`switchyard doctor --feedback`: write FEEDBACK.md from live measurements on Token Factory.

Each finding is measured again on every run, so anyone with a Token Factory key can check it:

  ids         the Nemotron model IDs in /v1/models, and how their naming differs
  latency     the same short request to each Nemotron model, repeated: median and p90 seconds
  schema      json_schema with and without the schema copied into the prompt (field descriptions)
  runaway     a long structured plan with a 4,096-token cap: how often the JSON is cut off
  embeddings  which embedding models exist, and single-text latency of the only one

The Nebius CLI findings can't be measured with an API key, so they are written as observed,
with the date and CLI version. A full run costs a few cents.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime, timezone

from openai import OpenAI

from . import config

NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}

# --- json_schema descriptions (ai-cookbook's parse_event_details, cut down to two fields) ---
EVENT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "date"], "properties": {
    "name": {"type": "string", "description": "Name of the event"},
    "date": {"type": "string", "description": "Date and time of the event. Use ISO 8601 to format this value."}}}
EVENT_SYS = "Today is Thursday, October 29, 2026. Extract detailed event information."
EVENT_USER = "Let's schedule a 1h team meeting next Tuesday at 2pm with Alice and Bob."
SCHEMA_IN_PROMPT = "\nReply with JSON matching this schema (follow each field's description):\n" + json.dumps(EVENT_SCHEMA)

# --- long structured output (a blog plan, like ai-cookbook's get_plan) ---
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title", "sections"], "properties": {
    "title": {"type": "string"},
    "sections": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                            "required": ["heading", "key_points", "word_count"], "properties": {
            "heading": {"type": "string"}, "key_points": {"type": "array", "items": {"type": "string"}},
            "word_count": {"type": "integer"}}}}}}
PLAN_USER = ("Plan a detailed long-form blog post about migrating a production app from GPT-4o to open models. "
             "Cover every section a senior engineer would expect, with specific key points for each.")
CAP = 4096

LATENCY_MSG = [{"role": "user", "content": "In two sentences, explain what a database index is."}]
EMBED_TEXT = "Switchyard moves an app's LLM calls to Nemotron after testing them."


def _client() -> OpenAI:
    key = config.api_key()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is not set (add it to .env)")
    return OpenAI(base_url=config.DEFAULT_BASE_URL, api_key=key, max_retries=0, timeout=180)


class _Meter:
    """Adds up token usage so the report can say what it cost."""

    def __init__(self):
        self.cost = 0.0
        self.calls = 0

    def add(self, model_id: str, usage) -> None:
        self.calls += 1
        m = next((m for m in config.CANDIDATES if m.id == model_id), None)
        if m and usage:
            self.cost += (usage.prompt_tokens * m.price_in + (usage.completion_tokens or 0) * m.price_out) / 1e6


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 2)


def measure_ids(client: OpenAI) -> dict:
    ids = sorted(m.id for m in client.models.list().data)
    nem = [i for i in ids if "nemotron" in i.lower()]
    styles = []
    for i in nem:
        name = i.split("/", 1)[-1]
        styles.append({"id": i,
                       "case": "lower" if name == name.lower() else "mixed",
                       "vendor_prefix": name.lower().startswith("nvidia-"),
                       "version": "underscore" if re.search(r"\d_\d", name) else "plain"})
    embed = [i for i in ids if "embed" in i.lower()]
    return {"all": ids, "nemotron": styles, "embedding": embed}


def measure_latency(client: OpenAI, meter: _Meter, n: int) -> list[dict]:
    rows = []
    for m in config.CANDIDATES:
        secs, toks, errs = [], [], 0
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = client.chat.completions.create(model=m.id, messages=LATENCY_MSG, max_tokens=200, temperature=0,
                                                   extra_body=NO_THINK)
                secs.append(time.perf_counter() - t0)
                toks.append(r.usage.completion_tokens if r.usage else 0)
                meter.add(m.id, r.usage)
            except Exception:  # noqa: BLE001 - a failed sample is reported, not fatal
                errs += 1
        rows.append({"model": m.key, "id": m.id, "n": len(secs), "errors": errs,
                     "median_s": _pct(secs, 0.5) if secs else None, "p90_s": _pct(secs, 0.9) if secs else None,
                     "median_tokens": int(statistics.median(toks)) if toks else None,
                     "tok_per_s": round(sum(toks) / sum(secs), 1) if secs and sum(secs) else None})
    return rows


ISO_DT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")


def _event_ok(content: str) -> dict:
    try:
        d = json.loads(content)
    except ValueError:
        return {"valid": False, "name_ok": False, "time_ok": False, "raw": content[:120]}
    name, date = str(d.get("name", "")), str(d.get("date", ""))
    return {"valid": True, "raw": json.dumps(d)[:160],
            "name_ok": bool(name) and "_" not in name and "meeting" in name.lower(),   # not a function-like label
            "time_ok": bool(ISO_DT.fullmatch(date.strip()))}                              # only an ISO 8601 date-time


def measure_schema(client: OpenAI, meter: _Meter, n: int) -> list[dict]:
    rows = []
    for m in config.CANDIDATES:
        for label, extra in (("schema only in response_format", ""), ("schema also in the prompt", SCHEMA_IN_PROMPT)):
            samples = []
            for _ in range(n):
                try:
                    r = client.chat.completions.create(
                        model=m.id, temperature=0, max_tokens=400, extra_body=NO_THINK,
                        messages=[{"role": "system", "content": EVENT_SYS + extra}, {"role": "user", "content": EVENT_USER}],
                        response_format={"type": "json_schema", "json_schema": {"name": "EventDetails", "strict": True,
                                                                                "schema": EVENT_SCHEMA}})
                    meter.add(m.id, r.usage)
                    samples.append(_event_ok(r.choices[0].message.content or ""))
                except Exception as e:  # noqa: BLE001
                    samples.append({"valid": False, "name_ok": False, "time_ok": False, "raw": f"{type(e).__name__}"})
            rows.append({"model": m.key, "id": m.id, "variant": label, "n": len(samples),
                         "both_ok": sum(s["name_ok"] and s["time_ok"] for s in samples),
                         "name_ok": sum(s["name_ok"] for s in samples), "time_ok": sum(s["time_ok"] for s in samples),
                         "example": next((s["raw"] for s in samples if not (s["name_ok"] and s["time_ok"])),
                                         samples[0]["raw"] if samples else "")})
    return rows


def measure_runaway(client: OpenAI, meter: _Meter, n: int) -> list[dict]:
    rows = []
    for m in config.CANDIDATES:
        cut, invalid, toks, errs = 0, 0, [], 0
        for _ in range(n):
            try:
                r = client.chat.completions.create(
                    model=m.id, max_tokens=CAP, extra_body=NO_THINK, temperature=0.7,
                    messages=[{"role": "user", "content": PLAN_USER}],
                    response_format={"type": "json_schema", "json_schema": {"name": "Plan", "schema": PLAN_SCHEMA}})
                meter.add(m.id, r.usage)
                toks.append(r.usage.completion_tokens if r.usage else 0)
                cut += r.choices[0].finish_reason == "length"
                try:
                    json.loads(r.choices[0].message.content or "")
                except ValueError:
                    invalid += 1
            except Exception:  # noqa: BLE001
                errs += 1
        rows.append({"model": m.key, "id": m.id, "n": n - errs, "errors": errs, "hit_cap": cut, "invalid_json": invalid,
                     "median_tokens": int(statistics.median(toks)) if toks else None, "max_tokens": max(toks, default=None)})
    return rows


def measure_embeddings(client: OpenAI, models: list[str], n: int) -> list[dict]:
    rows = []
    for mid in models:
        secs, errs, dim = [], 0, None
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                r = client.embeddings.create(model=mid, input=EMBED_TEXT)
                secs.append(time.perf_counter() - t0)
                dim = len(r.data[0].embedding)
            except Exception:  # noqa: BLE001
                errs += 1
        rows.append({"id": mid, "n": len(secs), "errors": errs, "dim": dim,
                     "min_s": round(min(secs), 2) if secs else None, "median_s": _pct(secs, 0.5) if secs else None,
                     "max_s": round(max(secs), 2) if secs else None})
    return rows


def _expects_json(site: dict) -> bool:
    p = site.get("params") or {}
    rf = p.get("response_format")
    return bool(p.get("output_schema")) or (isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"))


def recorded(runs_dir: str = "runs") -> dict:
    """What the recorded migration runs saw: per-model latency, and structured answers cut off at the token cap."""
    from pathlib import Path
    seen, lat, calls, cut = set(), {}, {}, []
    for f in sorted(Path(runs_dir).glob("**/run.json")):
        if f.parent.name in seen:
            continue                                   # runs/demo holds copies of local runs
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen.add(f.parent.name)
        for s in d.get("sites") or []:
            site = s.get("site") or {}
            for mk, ev in (s.get("evals") or {}).items():
                if mk not in config.MODELS:
                    continue                           # skip prompt-repair re-runs and the original model
                for r in ev.get("results") or []:
                    if isinstance(r.get("latency"), (int, float)) and not r.get("error"):
                        lat.setdefault(mk, []).append(r["latency"])
                    if not _expects_json(site):
                        continue
                    calls[mk] = calls.get(mk, 0) + 1
                    out = r.get("output") or ""
                    try:
                        json.loads(out)
                        continue
                    except ValueError:
                        pass
                    if r.get("tokens_out") in (4096, 8192):
                        cut.append({"run": f.parent.name, "function": site.get("function"), "model": mk,
                                    "case": r.get("case"), "tokens_out": r["tokens_out"], "visible_chars": len(out),
                                    "tail": out[-80:]})
    by_model = {mk: {"structured_calls": calls.get(mk, 0), "cut_off": sum(c["model"] == mk for c in cut)}
                for mk in config.MODELS}
    runs_with = sorted({c["run"] for c in cut})
    return {"runs": len(seen), "calls": sum(len(v) for v in lat.values()),
            "latency": {mk: {"n": len(v), "median_s": _pct(v, 0.5), "p90_s": _pct(v, 0.9)} for mk, v in lat.items() if v},
            "cutoffs": by_model, "cutoff_runs": runs_with, "cutoff_cases": cut}


def run(n: int = 5, skip: tuple[str, ...] = (), log=print, runs_dir: str = "runs") -> dict:
    client, meter = _client(), _Meter()
    out: dict = {"measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "base_url": config.DEFAULT_BASE_URL, "samples": n, "recorded": recorded(runs_dir)}
    log("model IDs ...")
    out["ids"] = measure_ids(client)
    if "latency" not in skip:
        log(f"latency ({n} requests per model) ...")
        out["latency"] = measure_latency(client, meter, n)
    if "schema" not in skip:
        log(f"json_schema descriptions ({n} per model and variant) ...")
        out["schema"] = measure_schema(client, meter, n)
    if "runaway" not in skip:
        k = max(2, n // 2 + 1)
        log(f"long structured output, {CAP}-token cap ({k} per model) ...")
        out["runaway"] = measure_runaway(client, meter, k)
    if "embeddings" not in skip:
        log(f"embeddings ({2 * n} single-text requests per model) ...")
        out["embeddings"] = measure_embeddings(client, out["ids"]["embedding"], 2 * n)
    out["cost_usd"], out["calls"] = round(meter.cost, 4), meter.calls
    return out


# ---------------------------------------------------------------- report

def _lat(rows, key):
    return next((r for r in rows if r["model"] == key), {})


def markdown(m: dict) -> str:
    L: list[str] = []
    a = L.append
    a("# Feedback on Nebius Token Factory and the Nebius CLI")
    a("")
    a("From building [Switchyard](README.md), which moves an app's LLM calls to Nemotron on Token Factory after testing "
      "them. We ported three real projects (ai-cookbook, microsoft/generative-ai-for-beginners, WhyChain) and deployed with the Nebius CLI.")
    a("")
    a(f"**The API numbers below were measured live on {m['measured_at']}** by `switchyard doctor --feedback` "
      f"({m['calls']} requests, about ${m['cost_usd']:.2f}). Rerun it with your own key to check them: results vary "
      f"between runs, so each figure is over {m['samples']} or more samples. The CLI findings can't be measured with an API key; "
      "they are what we observed, with the date and version.")
    a("")
    a("| # | Issue | Impact |")
    a("|---|---|---|")
    rows = [("json_schema field descriptions never reach the model", "Wrong values in valid JSON after migrating from OpenAI `.parse()`"),
            ("Model IDs use four different naming styles", "Ports from NVIDIA NIM / build.nvidia.com and config typos fail with 404"),
            ("The smallest Nemotron is the slowest", "Teams choose Lightning for speed and get the slowest replies"),
            ("Structured answers sometimes burn the whole token budget unseen", "Unfinished JSON, a crash in the caller, and paid tokens nobody sees"),
            ("No Nemotron embedding model; the only one is slow", "RAG apps can't run on Nemotron alone"),
            ("Nebius CLI: confusing auth failures", "Deploys blocked with errors that don't say what to do")]
    for i, (t, imp) in enumerate(rows, 1):
        a(f"| {i} | {t} | {imp} |")
    a("")

    # 1 schema
    a("## 1. `json_schema` field descriptions never reach the model")
    a("")
    a("**What happened.** Token Factory enforces the *shape* of a `json_schema` response, but the model doesn't see the "
      "`description` of each field. OpenAI's `client.beta.chat.completions.parse()` users put their instructions there "
      "(\"use ISO 8601\"), so after a port the JSON stays valid but the values are wrong. Copying the schema into the system "
      "prompt fixes it, which points at the descriptions as the missing piece. Super is hit hardest; in this measurement "
      "Lightning and Ultra guessed right without them, but a port can't rely on guessing.")
    a("")
    a("**Repro** (ai-cookbook's `parse_event_details`, cut to two fields):")
    a("```python")
    a("import json, os; from openai import OpenAI")
    a('tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])')
    a('s = {"type": "object", "additionalProperties": False, "required": ["name", "date"], "properties": {')
    a('     "name": {"type": "string", "description": "Name of the event"},')
    a('     "date": {"type": "string", "description": "Date and time of the event. Use ISO 8601 to format this value."}}}')
    a('sys = "Today is Thursday, October 29, 2026. Extract detailed event information."')
    a('for extra in ("", "\\nReply with JSON matching this schema:\\n" + json.dumps(s)):')
    a('    r = tf.chat.completions.create(model="nvidia/nemotron-3-super-120b-a12b", temperature=0, messages=[{"role": "system", "content": sys + extra},')
    a('        {"role": "user", "content": "Schedule a 1h team meeting next Tuesday at 2pm with Alice and Bob."}],')
    a('        response_format={"type": "json_schema", "json_schema": {"name": "EventDetails", "strict": True, "schema": s}})')
    a('    print(bool(extra), r.choices[0].message.content)')
    a("```")
    a("")
    if m.get("schema"):
        a("**Measured.** A response counts as correct when the name is a readable event name (not a label like "
          "`create_calendar_event`) and the date is only an ISO 8601 date-time, as the description asks (`2026-11-03T14:00:00`).")
        a("")
        a("| Model | Schema only in `response_format` | Schema also in the prompt | Example of a miss |")
        a("|---|---|---|---|")
        for key in [c.key for c in config.CANDIDATES]:
            rs = [r for r in m["schema"] if r["model"] == key]
            if len(rs) < 2:
                continue
            only, both = rs
            a(f"| `{only['id']}` | {only['both_ok']}/{only['n']} correct | {both['both_ok']}/{both['n']} correct | "
              + (f"`{(only['example'] or '').replace('|', '/')[:110]}` |" if only["both_ok"] < only["n"] else "none |"))
        a("")
    a("**Impact.** Every team porting `.parse()` code to Super hits this silently: no error, just wrong values that pass "
      "schema validation and break the code downstream (a calendar API rejects a date without a time).")
    a("")
    a("**Suggested fix.** Pass field descriptions into the prompt the way OpenAI does (the chat template can render the schema), "
      "or document that they're ignored and recommend copying the schema into the system prompt. Switchyard does the copy "
      "in every migrated `.parse()` call as a workaround.")
    a("")

    # 2 ids
    ids = m["ids"]
    a("## 2. Model IDs use four different naming styles")
    a("")
    a("**What happened.** The four Nemotron models in `/v1/models` are named four ways: lower-case, mixed case, an underscore for "
      "the version dot, and a repeated `NVIDIA-` vendor prefix. IDs are case-sensitive, so a guessed or ported ID fails with 404.")
    a("")
    a("**Repro**")
    a("```bash")
    a('curl -s https://api.tokenfactory.us-central1.nebius.com/v1/models -H "Authorization: Bearer $NEBIUS_API_KEY" \\')
    a("  | python -c \"import json,sys; print('\\n'.join(m['id'] for m in json.load(sys.stdin)['data'] if 'emotron' in m['id']))\"")
    a("```")
    a("")
    a(f"**Measured** ({len(ids['all'])} models listed):")
    a("")
    a("| ID | Case | `NVIDIA-` prefix in the name | Version written as |")
    a("|---|---|---|---|")
    for s in ids["nemotron"]:
        a(f"| `{s['id']}` | {s['case']} | {'yes' if s['vendor_prefix'] else 'no'} | "
          f"{'`3_5` (underscore)' if s['version'] == 'underscore' else 'digits only'} |")
    a("")
    a("**Impact.** Code ported from NVIDIA's own catalog (build.nvidia.com / NIM uses lower-case hyphenated IDs such as "
      "`nvidia/llama-3.3-nemotron-super-49b-v1`) or written from the model's marketing name (\"Nemotron 3.5 Lightning\") "
      "doesn't resolve. We had to copy each ID by hand from the listing.")
    a("")
    a("**Suggested fix.** One scheme (lower-case, hyphens, `3.5` or `3-5`, no repeated vendor), with case-insensitive "
      "matching and the old IDs kept as aliases. A 404 that suggests the closest ID would also help.")
    a("")

    # 3 latency
    lat = m.get("latency") or []
    a("## 3. The smallest Nemotron is the slowest")
    a("")
    a("**What happened.** Lightning is the cheapest and smallest model, so it's the natural choice for latency-sensitive routes. "
      "It was the slowest of the three both in our migration runs and in a short repeated request.")
    a("")
    rec = m.get("recorded") or {}
    if rec.get("latency"):
        a(f"**Recorded** in our {rec['runs']} migration runs on 2026-10-29/30 (every test call, real app prompts):")
        a("")
        a("| Model | Calls | Median | p90 |")
        a("|---|---|---|---|")
        for mk in [c.key for c in config.CANDIDATES]:
            r = rec["latency"].get(mk)
            if r:
                a(f"| `{config.MODELS[mk].id}` | {r['n']} | {r['median_s']} s | {r['p90_s']} s |")
        a("")
    a("**Repro**")
    a("```python")
    a("import os, time; from openai import OpenAI")
    a('tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])')
    a('for mid in ("nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"):')
    a("    t = []")
    a("    for _ in range(5):")
    a("        t0 = time.perf_counter()")
    a('        tf.chat.completions.create(model=mid, max_tokens=200, temperature=0, extra_body={"chat_template_kwargs": {"enable_thinking": False}},')
    a('                                   messages=[{"role": "user", "content": "In two sentences, explain what a database index is."}])')
    a("        t.append(time.perf_counter() - t0)")
    a("    print(mid, sorted(t)[2])")
    a("```")
    a("")
    if lat:
        a("**Measured** (same short request, thinking off):")
        a("")
        a("| Model | Median | p90 | Output tokens (median) | Tokens/s | Samples |")
        a("|---|---|---|---|---|---|")
        for r in lat:
            a(f"| `{r['id']}` | {r['median_s']} s | {r['p90_s']} s | {r['median_tokens']} | {r['tok_per_s']} | "
              f"{r['n']}{f' ({r['errors']} failed)' if r['errors'] else ''} |")
        a("")
        li, su = _lat(lat, "lightning"), _lat(lat, "super")
        if li.get("median_s") and su.get("median_s"):
            a(f"In this run Lightning's median was {li['median_s']} s against Super's {su['median_s']} s"
              + (" (Lightning slower)." if li["median_s"] > su["median_s"] else
                 ". On short requests the gap can close; the slowdown showed on our longer test calls."))
            a("")
    a("**Impact.** Choosing a model by size gives the wrong answer for latency. Switchyard's router records p50/p95 latency "
      "per route so teams see it, but nothing tells them before they switch.")
    a("")
    a("**Suggested fix.** Publish median latency and throughput per model (or show them on the model page), and check "
      "Lightning's serving capacity.")
    a("")

    # 4 runaway
    rw = m.get("runaway") or []
    rec = m.get("recorded") or {}
    co = rec.get("cutoffs") or {}
    cases = rec.get("cutoff_cases") or []
    a("## 4. Structured answers sometimes burn the whole token budget on output nobody sees (intermittent)")
    a("")
    if cases:
        total = sum((co.get(k) or {}).get("structured_calls", 0) for k in co)
        med = sorted(c["visible_chars"] for c in cases)[len(cases) // 2]
        a(f"**What happened.** In our recorded runs, {len(cases)} of {total:,} `json_schema` answers used the whole output budget "
          "(4,096 or 8,192 tokens, all billed) and returned unfinished JSON. Most held far less text than the tokens billed: "
          f"the median was {med:,} visible characters, where 4,096 tokens of text is roughly 15,000, and some were empty. "
          "So most of the budget went on output the caller never receives, most likely whitespace inside the JSON, which a "
          "schema grammar allows without limit. The caller's `json.loads` then fails.")
        a("")
        a("| Model | Structured answers recorded | Used the whole budget, JSON unfinished | Visible characters in those | Runs |")
        a("|---|---|---|---|---|")
        for mk in [c.key for c in config.CANDIDATES]:
            r = co.get(mk) or {}
            mine = [c for c in cases if c["model"] == mk]
            ch = sorted(c["visible_chars"] for c in mine)
            runs = sorted({c["run"] for c in mine})
            a(f"| `{config.MODELS[mk].id}` | {r.get('structured_calls', 0)} | {r.get('cut_off', 0)} | "
              f"{f'{ch[0]:,}–{ch[-1]:,}' if ch else '–'} | {', '.join('`' + x[:13] + '`' for x in runs) or '–'} |")
        a("")
        a("Every cut-off case (run, function, case, tokens billed, visible characters, last characters) is in "
          "[docs/feedback.json](docs/feedback.json).")
        a("")
    a("**Rechecked on 2026-10-30.** We replayed four of the recorded failing `get_plan` inputs on Ultra twice each, with and "
      "without the schema in the prompt: 0 of 16 were cut off (every answer finished in 537–1,520 tokens). "
      + ("The generic repro below also finished every time ("
         + ", ".join(f"{r['model']} {r['hit_cap']}/{r['n']} cut off" for r in rw) + "). " if rw else "")
      + "So it was intermittent or has been fixed on the serving side, with no notice either way.")
    a("")
    a("**Repro** (it no longer triggers for us; `finish_reason == \"length\"` with short content is the symptom):")
    a("```python")
    a("import os; from openai import OpenAI")
    a('tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])')
    a('schema = {"type": "object", "properties": {"title": {"type": "string"}, "sections": {"type": "array", "items": {"type": "object",')
    a('          "properties": {"heading": {"type": "string"}, "key_points": {"type": "array", "items": {"type": "string"}}}}}}}')
    a('r = tf.chat.completions.create(model="nvidia/Nemotron-3-Ultra-550b-a55b", max_tokens=4096, extra_body={"chat_template_kwargs": {"enable_thinking": False}},')
    a('    messages=[{"role": "user", "content": "Plan a detailed long-form blog post about migrating a production app from GPT-4o to open models."}],')
    a('    response_format={"type": "json_schema", "json_schema": {"name": "Plan", "schema": schema}})')
    a("c = r.choices[0].message.content; print(r.choices[0].finish_reason, r.usage.completion_tokens, len(c), len(c.strip()))")
    a("```")
    a("")
    a("**Impact.** A cut-off structured answer is an exception in the caller, not a worse answer, so one runaway response fails "
      "the request, and the caller still pays for the full budget. During a migration it made models look worse than they "
      "are, and a re-run can give a different verdict.")
    a("")
    a("**Suggested fix.** Limit consecutive whitespace in `json_schema` mode (or end generation once the top-level object "
      "closes), and post serving changes in a changelog so users know when behaviour like this changes.")
    a("")

    # 5 embeddings
    em = m.get("embeddings") or []
    a("## 5. No Nemotron embedding model, and the only embedding model is slow")
    a("")
    a(f"**What happened.** `/v1/models` lists {len(ids['embedding'])} embedding model"
      f"{'' if len(ids['embedding']) == 1 else 's'}: {', '.join(f'`{e}`' for e in ids['embedding']) or 'none'}. "
      "So an app that embeds (any RAG app) can't run fully on Nemotron, and porting WhyChain we found single-text embeddings "
      "taking 1–12 s.")
    a("")
    a("**Repro**")
    a("```python")
    a("import os, time; from openai import OpenAI")
    a('tf = OpenAI(base_url="https://api.tokenfactory.us-central1.nebius.com/v1/", api_key=os.environ["NEBIUS_API_KEY"])')
    a("for _ in range(10):")
    a("    t0 = time.perf_counter()")
    a('    tf.embeddings.create(model="Qwen/Qwen3-Embedding-8B", input="Switchyard moves an app\'s LLM calls to Nemotron.")')
    a('    print(round(time.perf_counter() - t0, 2), "s")')
    a("```")
    a("")
    if em:
        a("**Measured** (one short text per request):")
        a("")
        a("| Model | Min | Median | Max | Dimensions | Samples |")
        a("|---|---|---|---|---|---|")
        for r in em:
            a(f"| `{r['id']}` | {r['min_s']} s | {r['median_s']} s | {r['max_s']} s | {r['dim']} | "
              f"{r['n']}{f' ({r['errors']} failed)' if r['errors'] else ''} |")
        a("")
    a("**Impact.** Retrieval latency adds to every RAG answer, and teams that want a single-vendor Nemotron stack need a second provider.")
    a("")
    a("**Suggested fix.** Serve NVIDIA's own embedding models (the NV-Embed / Llama-Nemotron embedding family on NIM), and look "
      "at the tail latency of single-text requests.")
    a("")

    # 6 CLI
    a("## 6. Nebius CLI: auth failures that don't say what to do")
    a("")
    a("**What happened** (observed 2026-10-29/30, Nebius CLI 0.12.298 in WSL Ubuntu, while setting up `switchyard deploy`):")
    a("")
    a("- With a working profile (`nebius iam whoami` succeeds), `nebius registry list` fails with "
      "`rpc error: code = PermissionDenied desc = unknown subject`, and `nebius vpc subnet list` is also denied. "
      "Rechecked on 2026-10-30 with the same CLI: `registry list` now returns `{}`. So the error came from the account's "
      "state at the time, and nothing in the message said which state or how to fix it.")
    a("- `nebius ai endpoint list` opens a browser login, even with that profile configured and other commands working.")
    a("- A new tenant and its projects stay **SUSPENDED until billing is activated**. Reads succeed, so nothing looks wrong "
      "until the first create fails. The `unknown subject` error above is likely this, but it doesn't say so.")
    a("- `nebius config set region` fails with `no such config field \"region\"`.")
    a("- `nebius ai endpoint create` defaults to a GPU platform (`gpu-h100-sxm`); a CPU web app needs `--platform cpu-d3`.")
    a("- The installer puts the binary in `~/.nebius/bin`, which isn't on `PATH` until the shell reloads.")
    a("- `nebius profile delete` refuses to delete the active default profile; a second login silently reuses the browser's Google account.")
    a("")
    a("**Repro**")
    a("```bash")
    a("curl -sSL https://storage.eu-north1.nebius.cloud/cli/install.sh | bash && exec -l $SHELL")
    a("nebius profile create            # log in to a new tenant, pick its project")
    a("nebius iam whoami                # works")
    a("nebius registry list             # PermissionDenied: unknown subject")
    a("nebius ai endpoint list          # opens a browser login despite the profile")
    a("```")
    a("")
    a("**Impact.** We didn't reach a live Serverless endpoint: every error pointed at auth, while the cause was billing. "
      "Switchyard's demo runs on EC2 instead.")
    a("")
    a("**Suggested fix.** Say \"this tenant is suspended: activate billing at …\" in place of `unknown subject`; make every "
      "command use the active profile's token; default `endpoint create` to CPU or ask; print the `PATH` step at the end of the installer.")
    a("")

    a("## What worked well")
    a("")
    a("- **One OpenAI-compatible endpoint for the whole Nemotron family.** A port is a `base_url` and `model` change, so patches stay small.")
    a("- **Every probed feature works on all three models**: chat, `json_object`, `json_schema`, tools, the thinking switch and "
      "the Responses API ([docs/doctor.md](docs/doctor.md); rerun with `switchyard doctor`).")
    a("- **Price makes testing before switching practical.** A full study of 11 call sites (8 tests each, 3 models, grading, "
      "prompt repair, security probes) cost about $0.55.")
    a("")
    a("---")
    a("Generated by `switchyard doctor --feedback --out FEEDBACK.md`. Raw measurements: `docs/feedback.json`.")
    return "\n".join(L) + "\n"
