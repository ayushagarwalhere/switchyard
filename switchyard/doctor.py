"""`switchyard doctor`: probe what each Nemotron model on Token Factory supports, in about 30 seconds.

Switchyard relies on a handful of API features. Each is checked with one tiny request per
model, so you know before a long run (and Switchyard's fallbacks explain what they'd do):

  chat          plain chat completion
  json_object   response_format={"type": "json_object"}
  json_schema   structured output (what the OpenAI SDK's .parse() sends)
  tools         function calling (used by the investigation agent)
  thinking      chat_template_kwargs.enable_thinking (Nemotron's reasoning switch)
  responses     the Responses API (/v1/responses), used by apps on client.responses.*
"""
from __future__ import annotations

import json
import time

from openai import OpenAI

from . import config

SCHEMA = {"type": "json_schema", "json_schema": {"name": "Answer", "schema": {
    "type": "object", "properties": {"label": {"type": "string", "enum": ["yes", "no"]}}, "required": ["label"],
    "additionalProperties": False}}}
TOOL = [{"type": "function", "function": {"name": "lookup", "description": "Look up a word.",
                                          "parameters": {"type": "object", "properties": {"word": {"type": "string"}},
                                                         "required": ["word"]}}}]
MSG = [{"role": "user", "content": "Is the sky blue on a clear day? Answer yes or no."}]


def _probe(client: OpenAI, model_id: str, name: str) -> dict:
    t0 = time.perf_counter()
    try:
        if name == "chat":
            r = client.chat.completions.create(model=model_id, messages=MSG, max_tokens=300)
            ok, detail = bool(r.choices[0].message.content), (r.choices[0].message.content or "")[:40]
        elif name == "json_object":
            r = client.chat.completions.create(model=model_id, max_tokens=300, response_format={"type": "json_object"},
                                               messages=[{"role": "user", "content": MSG[0]["content"] + ' Reply as JSON: {"label": ...}'}])
            json.loads(r.choices[0].message.content or "")
            ok, detail = True, "valid JSON"
        elif name == "json_schema":
            r = client.chat.completions.create(model=model_id, messages=MSG, max_tokens=300, response_format=SCHEMA)
            data = json.loads(r.choices[0].message.content or "")
            ok, detail = data.get("label") in ("yes", "no"), f"label={data.get('label')}"
        elif name == "tools":
            r = client.chat.completions.create(model=model_id, max_tokens=300, tools=TOOL,
                                               messages=[{"role": "user", "content": "Use the lookup tool for the word 'switchyard'."}])
            calls = r.choices[0].message.tool_calls or []
            ok = bool(calls) and calls[0].function.name == "lookup"
            detail = f"called {calls[0].function.name}" if calls else "answered in text, no tool call"
        elif name == "thinking":
            r = client.chat.completions.create(model=model_id, messages=MSG, max_tokens=300,
                                               extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            ok, detail = True, "accepted"
        elif name == "responses":
            r = client.responses.create(model=model_id, input=MSG[0]["content"], max_output_tokens=300)
            ok, detail = bool(getattr(r, "output_text", "")), (getattr(r, "output_text", "") or "")[:40]
        else:
            raise ValueError(name)
    except Exception as e:  # noqa: BLE001 - every failure is a finding
        ok, detail = False, f"{type(e).__name__}: {e}"[:160]
    return {"ok": ok, "detail": detail, "seconds": round(time.perf_counter() - t0, 2)}


PROBES = ["chat", "json_object", "json_schema", "tools", "thinking", "responses"]
FALLBACK = {
    "json_schema": "Switchyard falls back to json_object, then plain text, and notes it in the run",
    "json_object": "Switchyard falls back to plain text and parses the JSON itself",
    "tools": "the investigation agent falls back to one call over a static search of the code",
    "thinking": "Switchyard stops sending the switch and notes it in the run",
    "responses": "apps using client.responses.* would fail after migration; the runtime check would catch it",
}


def run(models: list[config.Model] | None = None, base_url: str | None = None, key: str | None = None) -> list[dict]:
    key = key or config.api_key()
    if not key:
        raise RuntimeError("NEBIUS_API_KEY is not set (add it to .env)")
    client = OpenAI(base_url=base_url or config.DEFAULT_BASE_URL, api_key=key, max_retries=1, timeout=90)
    rows = []
    for m in models or config.CANDIDATES:
        rows.append({"model": m.key, "id": m.id, **{p: _probe(client, m.id, p) for p in PROBES}})
    return rows


def markdown(rows: list[dict]) -> str:
    out = ["| Model | " + " | ".join(PROBES) + " |", "|---|" + "---|" * len(PROBES)]
    for r in rows:
        out.append(f"| `{r['id']}` | " + " | ".join(("✅" if r[p]["ok"] else "❌") + f" {r[p]['seconds']}s" for p in PROBES) + " |")
    fails = [(r["id"], p, r[p]["detail"]) for r in rows for p in PROBES if not r[p]["ok"]]
    if fails:
        out += ["", "Failures:"] + [f"- `{mid}` {p}: {d}" + (f" ({FALLBACK[p]})" if p in FALLBACK else "") for mid, p, d in fails]
    return "\n".join(out)
