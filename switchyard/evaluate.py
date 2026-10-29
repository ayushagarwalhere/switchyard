"""Run candidate models on a test set and score them: hard checks in code, plus a Nemotron grader."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import agents, config
from .llm import TokenFactory, extract_json


def _norm(s) -> str:
    return re.sub(r"[\s\W_]+", " ", str(s)).strip().lower()


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def schema_errors(value, schema: dict, path: str = "$") -> list[str]:
    """Small JSON-schema check: types, required keys, enums, nested objects/arrays."""
    errs: list[str] = []
    if not isinstance(schema, dict) or not schema:
        return errs
    if "anyOf" in schema:
        if all(schema_errors(value, s, path) for s in schema["anyOf"]):
            errs.append(f"{path}: matches none of the allowed types")
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    t = schema.get("type")
    if t == "null":
        return errs if value is None else errs + [f"{path}: expected null"]
    if t in _TYPES:
        ok = isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return errs + [f"{path}: expected {t}, got {type(value).__name__}"]
    if t == "object" or "properties" in schema:
        if not isinstance(value, dict):
            return errs + [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errs.extend(schema_errors(value[k], sub, f"{path}.{k}"))
    if t == "array" and isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value[:50]):
            errs.extend(schema_errors(item, schema["items"], f"{path}[{i}]"))
    return errs


def hard_checks(analysis: dict, case: dict, output: str, schema: dict | None = None) -> dict:
    """Deterministic checks. Returns {"score": 0..1 or None, "checks": [...]}"""
    checks: list[dict] = []
    out_spec = analysis.get("output", {}) or {}
    fmt = (out_spec.get("format") or "").lower()
    expect = case.get("expect") or {}
    inputs_text = json.dumps(case.get("inputs") or {}, ensure_ascii=False)
    if not output.strip():
        return {"score": 0.0, "checks": [{"name": "non-empty", "ok": False}]}

    if schema:
        data = extract_json(output)
        checks.append({"name": "valid JSON", "ok": data is not None})
        errs = schema_errors(data, schema) if data is not None else ["unparseable"]
        checks.append({"name": "matches Pydantic schema", "ok": not errs, "detail": "; ".join(errs[:3])})
        for k, v in (expect.get("fields") or {}).items():
            got = data.get(k) if isinstance(data, dict) else None
            ok = _field_ok(got, v, k, inputs_text)
            if ok is None:
                continue
            checks.append({"name": f"field {k}", "ok": ok, "detail": f"expected {v!r}, got {got!r}" if not ok else "",
                           "got": got})
        return {"score": sum(c["ok"] for c in checks) / len(checks), "checks": checks}
    if fmt == "json" or "fields" in expect:
        data = extract_json(output)
        strict_ok = False
        try:
            json.loads(output.strip())
            strict_ok = True
        except Exception:
            pass
        checks.append({"name": "valid JSON", "ok": data is not None})
        checks.append({"name": "JSON only (parses with json.loads)", "ok": strict_ok})
        keys = out_spec.get("json_keys") or []
        if isinstance(data, dict) and keys:
            missing = [k for k in keys if k not in data]
            checks.append({"name": "required keys", "ok": not missing, "detail": f"missing {missing}" if missing else ""})
        fields = expect.get("fields") or {}
        if isinstance(data, dict):
            for k, v in fields.items():
                got = data.get(k)
                ok = _field_ok(got, v, k, inputs_text)
                if ok is None:
                    continue
                checks.append({"name": f"field {k}", "ok": ok, "detail": f"expected {v!r}, got {got!r}" if not ok else "",
                               "got": got})
    if fmt == "label" or "label" in expect:
        labels = [_norm(x) for x in out_spec.get("labels") or []]
        got = _norm(output)
        if labels:
            checks.append({"name": "label from allowed set", "ok": got in labels, "detail": f"got {output.strip()[:40]!r}" if got not in labels else ""})
        if "label" in expect:
            ok = got == _norm(expect["label"])
            checks.append({"name": "correct label", "ok": ok, "detail": f"expected {expect['label']!r}" if not ok else ""})
    if not checks:
        return {"score": None, "checks": []}
    return {"score": sum(c["ok"] for c in checks) / len(checks), "checks": checks}


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
JUDGEMENT_KEY = re.compile(r"(?i)(score|confidence|rating|probability|likelihood|certainty)")


def _as_dt(x):
    from datetime import datetime
    if not isinstance(x, str) or not DATE_RE.match(x.strip()):
        return None
    try:
        return datetime.fromisoformat(x.strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def _tool_output(calls: list[dict]) -> str:
    """What the app reads from a tool call: the arguments (one call), not the wrapper with the function name."""
    if len(calls) == 1:
        args = calls[0].get("arguments")
        return args if isinstance(args, str) else json.dumps(args)
    return json.dumps([{"name": c.get("name"), "arguments": c.get("arguments")} for c in calls])


def _field_ok(got, want, key: str = "", inputs_text: str = "") -> bool | None:
    """Whether an expected field matches. None: not something a deterministic check should decide."""
    if isinstance(want, bool) or isinstance(got, bool):
        return isinstance(want, bool) and isinstance(got, bool) and got == want   # 1 isn't True
    if _is_num(want) and _is_num(got):
        g, w = float(got), float(want)
        if JUDGEMENT_KEY.search(key or ""):
            return abs(g - w) <= max(0.15, 0.2 * abs(w))    # 7 vs 8 on a review score is a judgement call
        if float(w).is_integer() and float(g).is_integer() and abs(w) > 1:
            return g == w                                   # counts, ids, amounts: exact
        return abs(g - w) <= max(0.15, 0.1 * abs(w))        # confidences between 0 and 1
    wd = _as_dt(want)
    if wd is not None:
        gd = _as_dt(got)
        if gd is None:
            return False                                    # must still be a valid date
        if not re.search(r"\b(19|20)\d{2}\b", inputs_text):
            return True     # "next Friday" with no date in the input: any valid date is a fair reading
        return gd.replace(tzinfo=None) == wd.replace(tzinfo=None)
    if isinstance(want, str) and len(want) > 40:
        return None                                         # prose: the grader compares meaning, not characters
    if isinstance(want, (list, dict)):
        return None
    if isinstance(want, str) and len(want.split()) > 1 and isinstance(got, str):
        a, b = set(re.findall(r"\w+", want.lower())), set(re.findall(r"\w+", got.lower()))
        return bool(a and b) and len(a & b) / len(a | b) >= 0.5   # short free text ("Team meeting" ~ "team sync meeting")
    return _norm(got) == _norm(want)


def _is_num(x) -> bool:
    try:
        float(x)
        return not isinstance(x, bool)
    except (TypeError, ValueError):
        return False


def run_case(tf: TokenFactory, site: dict, analysis: dict, case: dict, model_key: str, hint: str = "") -> dict:
    messages = agents.with_hint(agents.render(site["messages"], case["inputs"]), hint)
    p = site.get("params", {})
    temperature = p.get("temperature") if isinstance(p.get("temperature"), (int, float)) else None
    max_tokens = p.get("max_tokens") or p.get("max_completion_tokens")
    # the original API's default is the model's full output length; without a cap here, long structured
    # answers get cut mid-JSON and a capable model fails for the wrong reason
    max_tokens = max_tokens if isinstance(max_tokens, int) else 4096
    json_mode = isinstance(p.get("response_format"), dict) and p["response_format"].get("type") in ("json_object", "json_schema")
    schema = (p.get("output_schema") or {}).get("schema")
    rf = None
    if schema:  # same structured-output request the OpenAI SDK's .parse() sends
        rf = {"type": "json_schema", "json_schema": {"name": p["output_schema"]["name"], "schema": schema}}
    elif isinstance(p.get("response_format"), dict) and p["response_format"].get("type") == "json_schema":
        rf = p["response_format"]
    c = tf.complete(model_key, messages, role=f"candidate:{model_key}", temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode, reasoning=False, response_format=rf)
    output = c.text if not c.tool_calls else _tool_output(c.tool_calls)
    result = {"case": case["id"], "model": model_key, "output": output, "latency": round(c.latency, 3),
              "tokens_in": c.tokens_in, "tokens_out": c.tokens_out, "cost": c.cost, "error": c.error}
    if c.error:
        result.update(score=0.0, passed=False, reason=c.error, checks=[])
        return result
    hard = hard_checks(analysis, case, output, schema)
    verdict = agents.judge(tf, analysis, case, messages, output)
    if hard["score"] is None:
        score = verdict["score"]
    else:
        score = 0.5 * hard["score"] + 0.5 * verdict["score"]
    hard_fail = any(not ch["ok"] for ch in hard["checks"]
                    if ch["name"] in ("valid JSON", "label from allowed set", "non-empty", "matches Pydantic schema"))
    judged_ok = verdict["pass"] or verdict["score"] >= 0.8
    passed = judged_ok and score >= 0.7 and not hard_fail
    result.update(score=round(score, 3), passed=passed, reason=verdict["reason"], checks=hard["checks"])
    return result


def evaluate_model(tf: TokenFactory, site: dict, analysis: dict, cases: list[dict], model_key: str,
                   hint: str = "", workers: int = 6, on_result=None) -> dict:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_case, tf, site, analysis, c, model_key, hint) for c in cases]
        for f in futures:
            r = f.result()
            results.append(r)
            if on_result:
                on_result(r)
    return summarize(model_key, results, hint)


def disputed_cases(evals: dict) -> list[int]:
    """Cases whose reference is probably wrong: every model failed it, on the same field, with the same value.

    Independent models agreeing with each other against the test writer is a signal about the test, not
    the models. Those cases are dropped from pass rates (and listed in the report), never silently passed.
    """
    runs = [e for k, e in evals.items() if "+" not in k]
    if len(runs) < 2:
        return []
    out = []
    for r0 in runs[0]["results"]:
        cid = r0["case"]
        rows = [next((r for r in e["results"] if r["case"] == cid), None) for e in runs]
        if any(r is None or r["passed"] or r.get("error") for r in rows):
            continue
        fields = None
        for r in rows:
            bad = {c["name"]: json.dumps(c.get("got"), sort_keys=True) for c in r.get("checks", [])
                   if not c["ok"] and c["name"].startswith("field ")}
            fields = bad if fields is None else {k: v for k, v in fields.items() if bad.get(k) == v}
        if fields:
            out.append(cid)
    return out


def exclude(ev: dict, disputed: list[int]) -> dict:
    """Recompute a model's summary without the disputed cases (results stay, marked)."""
    if not disputed:
        return ev
    for r in ev["results"]:
        r["disputed"] = r["case"] in disputed
    return {**summarize(ev["model"], ev["results"], ev.get("hint", "")), **{k: v for k, v in ev.items() if k == "rationale"}}


def summarize(model_key: str, results: list[dict], hint: str = "") -> dict:
    counted = [r for r in results if not r.get("disputed")] or results
    n = len(counted) or 1
    ok = [r for r in results if not r.get("error")]
    lat = sorted(r["latency"] for r in ok) or [0]
    return {
        "model": model_key,
        "model_id": config.model(model_key).id,
        "hint": hint,
        "pass_rate": sum(r["passed"] for r in counted) / n,
        "score": sum(r["score"] for r in counted) / n,
        "disputed": sum(1 for r in results if r.get("disputed")),
        "latency_p50": lat[len(lat) // 2],
        "tokens_in": sum(r["tokens_in"] for r in results),
        "tokens_out": sum(r["tokens_out"] for r in results),
        "cost": sum(r["cost"] for r in results),
        "errors": sum(1 for r in results if r.get("error")),
        "results": sorted(results, key=lambda r: r["case"]),
    }
