"""The Nemotron-powered steps: understand a call site, write tests, grade outputs, repair prompts."""
from __future__ import annotations

import json
import re

from . import config
from .llm import TokenFactory

PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def _site_brief(site: dict, context: str = "") -> str:
    msgs = "\n".join(f"[{m['role']}]\n{m['content']}" for m in site["messages"])
    return (f"File: {site['file']}:{site['line']}  function: {site.get('function')}\n"
            f"SDK call: {site['method']}  original model: {site.get('model') or site.get('model_expr')}\n"
            f"Params: {json.dumps(site.get('params', {}), default=str)}\n"
            f"Prompt template ({{{{var}}}} = runtime value):\n{msgs}\n\n"
            f"Enclosing code:\n```python\n{site['context'][:6000]}\n```"
            + (f"\n\nWhat an investigation of the rest of the codebase found:\n{context}" if context else ""))


ANALYZE_SYS = """You are Switchyard's code analyst. You read one LLM call site from an application and describe the task it performs precisely enough that another engineer could write tests for it.
Reply with JSON only:
{
  "task_type": "classification|extraction|summarization|rewrite|generation|routing|qa|code|chat|other",
  "summary": "one sentence: what this call does for the app",
  "inputs": [{"name": "<exact {{var}} name from the template>", "description": "what real values look like", "example": "short realistic example"}],
  "output": {"format": "label|json|text|list|code|number", "labels": ["only for label outputs"], "json_keys": ["only for json outputs"], "constraints": ["hard rules the downstream code relies on, e.g. 'must be valid JSON', 'one word', 'max 3 bullets'"]},
  "parsing": "how the calling code consumes the output (e.g. json.loads, .strip() compared to a set, displayed to user)",
  "quality_criteria": ["3-5 specific things a good answer must get right"],
  "difficulty": "easy|medium|hard",
  "realistic_input_search": "a web search query that would find realistic raw input text for this task, or empty string if the inputs are short/synthetic"
}"""


def analyze(tf: TokenFactory, site: dict, context: str = "") -> dict:
    data, _ = tf.json(config.ROLE_ANALYZE, [
        {"role": "system", "content": ANALYZE_SYS},
        {"role": "user", "content": _site_brief(site, context)},
    ], role="analyze", temperature=0.1, reasoning=False)
    if isinstance(data, list):  # some models wrap the object in a list
        data = next((x for x in data if isinstance(x, dict)), {})
    data.setdefault("inputs", [])
    data.setdefault("output", {})
    names = {i.get("name") for i in data["inputs"]}
    for v in site["variables"]:
        if v not in names:
            data["inputs"].append({"name": v, "description": "runtime value", "example": ""})
    return data


TESTGEN_SYS = """You are Switchyard's test author. You write a small but sharp test set for one LLM call site so that cheaper models can be compared against the best answer.
Rules:
- Each case fills EVERY template variable with a realistic value (real-looking text, not lorem ipsum). Long-form inputs should be 80-300 words.
- Mix: about half typical cases, the rest tricky ones (ambiguous, edge cases, noisy input, distractors, multilingual or messy formatting if plausible).
- "reference" is the ideal output exactly as the app expects it (same format, same label set, same JSON keys).
- "expect" holds machine-checkable facts when the task has them: {"label": "..."} for label tasks, {"fields": {"key": value}} for JSON extraction (only fields with one unambiguous correct value), {} otherwise.
- If the call has an output_schema (structured output), "reference" must be one JSON object valid against that schema, and "expect.fields" should hold the fields with a single correct value (enums, booleans, request types, flags); never put free text or confidence scores in expect.
- If source material is provided, base inputs on it.
- If the codebase investigation found real inputs, include them (or close variants), and cover the fields the app logic depends on in "expect".
Reply with JSON only: {"cases": [{"name": "short case name", "kind": "typical|tricky", "inputs": {"var": "value"}, "reference": "...", "expect": {}, "why": "what this case probes"}]}"""


def generate_tests(tf: TokenFactory, site: dict, analysis: dict, n: int = 8, sources: list[dict] | None = None,
                   context: str = "") -> list[dict]:
    src_txt = ""
    if sources:
        src_txt = "\n\nSource material from the web (use it to make inputs realistic; do not copy URLs into outputs):\n" + \
            "\n---\n".join(f"{s.get('title', '')}\n{(s.get('content') or '')[:1200]}" for s in sources[:5])
    user = (f"{_site_brief(site, context)}\n\nTask analysis:\n{json.dumps(analysis, indent=2)}\n\n"
            f"Template variables: {site['variables']}\nWrite {n} cases.{src_txt}")
    data, _ = tf.json(config.ROLE_TESTGEN, [
        {"role": "system", "content": TESTGEN_SYS},
        {"role": "user", "content": user},
    ], role="testgen", temperature=0.6, reasoning=True, max_tokens=16000)
    cases = data if isinstance(data, list) else data.get("cases") or next((v for v in data.values() if isinstance(v, list)), [])
    cases = [c for c in cases if isinstance(c, dict)]
    out = []
    for i, c in enumerate(cases[:n]):
        inputs = {k: str(v) for k, v in (c.get("inputs") or {}).items()}
        for v in site["variables"]:
            inputs.setdefault(v, "")
        ref = c.get("reference", "")
        if not isinstance(ref, str):
            ref = json.dumps(ref)
        out.append({"id": i + 1, "name": c.get("name", f"case {i + 1}"), "kind": c.get("kind", "typical"),
                    "inputs": inputs, "reference": ref, "expect": c.get("expect") or {}, "why": c.get("why", "")})
    return out


def render(messages: list[dict], inputs: dict) -> list[dict]:
    out = []
    for m in messages:
        content = PLACEHOLDER.sub(lambda mm: inputs.get(mm.group(1), ""), m["content"])
        role = m["role"] if m["role"] in ("system", "user", "assistant", "developer") else "user"
        if role == "developer":
            role = "system"
        out.append({"role": role, "content": content})
    return out


JUDGE_SYS = """You are a strict grader comparing a model's answer to a reference answer for one task inside a real application.
Grade whether the candidate would work as well as the reference *for the application* (format the code relies on, correctness, completeness). Wording differences are fine; wrong labels, missing or wrong facts, broken format, or extra chatter that would break parsing are not.
Reply with JSON only: {"score": 0-10, "pass": true|false, "reason": "one short sentence"}"""


def judge(tf: TokenFactory, analysis: dict, case: dict, rendered: list[dict], candidate: str) -> dict:
    prompt_view = "\n".join(f"[{m['role']}] {m['content'][:1500]}" for m in rendered)
    user = (f"Task: {analysis.get('summary')}\nOutput format: {json.dumps(analysis.get('output', {}))}\n"
            f"How the app parses it: {analysis.get('parsing', '')}\nCriteria: {analysis.get('quality_criteria', [])}\n\n"
            f"Prompt sent:\n{prompt_view}\n\nReference answer:\n{case['reference'][:3000]}\n\nCandidate answer:\n{candidate[:3000]}")
    try:
        data, _ = tf.json(config.ROLE_JUDGE, [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": user}],
                          role="judge", temperature=0.0, reasoning=False, max_tokens=400)
        score = float(data.get("score", 0))
        return {"score": max(0.0, min(10.0, score)) / 10, "pass": bool(data.get("pass", score >= 7)), "reason": str(data.get("reason", ""))[:300]}
    except Exception as e:  # noqa: BLE001
        return {"score": 0.0, "pass": False, "reason": f"judge error: {e}"[:200], "error": True}


REPAIR_SYS = """You are Switchyard's prompt engineer. A smaller, cheaper model almost handles a task but fails some test cases. Write a short addition to the system prompt that fixes the failures without changing what the app expects.
Rules: under 80 words, general (do not mention specific test cases), focus on output format and the specific mistakes seen. Never contradict the original prompt.
Reply with JSON only: {"hint": "text to append to the system prompt", "rationale": "one sentence"}"""


def repair_prompt(tf: TokenFactory, site: dict, analysis: dict, failures: list[dict]) -> dict:
    fails = "\n\n".join(f"Input: {json.dumps(f['inputs'])[:800]}\nExpected: {f['reference'][:500]}\nGot: {f['output'][:500]}\nGrader: {f['reason']}"
                        for f in failures[:5])
    user = f"{_site_brief(site)}\n\nTask analysis: {json.dumps(analysis)}\n\nFailures of the cheaper model:\n{fails}"
    data, _ = tf.json(config.ROLE_REPAIR, [{"role": "system", "content": REPAIR_SYS}, {"role": "user", "content": user}],
                      role="repair", temperature=0.3, reasoning=True, max_tokens=6000)
    return {"hint": str(data.get("hint", "")).strip(), "rationale": str(data.get("rationale", ""))}


def with_hint(messages: list[dict], hint: str) -> list[dict]:
    if not hint:
        return messages
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if m["role"] == "system":
            m["content"] = m["content"].rstrip() + "\n\n" + hint
            return msgs
    return [{"role": "system", "content": hint}] + msgs
