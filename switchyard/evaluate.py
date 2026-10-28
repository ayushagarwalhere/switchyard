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
    if not output.strip():
        return {"score": 0.0, "checks": [{"name": "non-empty", "ok": False}]}

    if schema:
        data = extract_json(output)
        checks.append({"name": "valid JSON", "ok": data is not None})
        errs = schema_errors(data, schema) if data is not None else ["unparseable"]
        checks.append({"name": "matches Pydantic schema", "ok": not errs, "detail": "; ".join(errs[:3])})
        for k, v in (expect.get("fields") or {}).items():
            got = data.get(k) if isinstance(data, dict) else None
            ok = _norm(got) == _norm(v) or (_is_num(got) and _is_num(v) and abs(float(got) - float(v)) < 1e-6)
            checks.append({"name": f"field {k}", "ok": ok, "detail": f"expected {v!r}, got {got!r}" if not ok else ""})
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
                ok = _norm(got) == _norm(v) or (_is_num(got) and _is_num(v) and abs(float(got) - float(v)) < 1e-6)
                checks.append({"name": f"field {k}", "ok": ok, "detail": f"expected {v!r}, got {got!r}" if not ok else ""})
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
    max_tokens = max_tokens if isinstance(max_tokens, int) else None
    json_mode = isinstance(p.get("response_format"), dict) and p["response_format"].get("type") in ("json_object", "json_schema")
    schema = (p.get("output_schema") or {}).get("schema")
    rf = None
    if schema:  # same structured-output request the OpenAI SDK's .parse() sends
        rf = {"type": "json_schema", "json_schema": {"name": p["output_schema"]["name"], "schema": schema}}
    elif isinstance(p.get("response_format"), dict) and p["response_format"].get("type") == "json_schema":
        rf = p["response_format"]
    c = tf.complete(model_key, messages, role=f"candidate:{model_key}", temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode, reasoning=False, response_format=rf)
    output = c.text if not c.tool_calls else json.dumps(c.tool_calls)
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
    passed = verdict["pass"] and score >= 0.7 and not hard_fail
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


def summarize(model_key: str, results: list[dict], hint: str = "") -> dict:
    n = len(results) or 1
    ok = [r for r in results if not r.get("error")]
    lat = sorted(r["latency"] for r in ok) or [0]
    return {
        "model": model_key,
        "model_id": config.MODELS[model_key].id,
        "hint": hint,
        "pass_rate": sum(r["passed"] for r in results) / n,
        "score": sum(r["score"] for r in results) / n,
        "latency_p50": lat[len(lat) // 2],
        "tokens_in": sum(r["tokens_in"] for r in results),
        "tokens_out": sum(r["tokens_out"] for r in results),
        "cost": sum(r["cost"] for r in results),
        "errors": sum(1 for r in results if r.get("error")),
        "results": sorted(results, key=lambda r: r["case"]),
    }
