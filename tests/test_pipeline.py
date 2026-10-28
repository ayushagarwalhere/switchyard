"""Offline end-to-end test: the whole pipeline with a scripted stand-in for Token Factory."""
import json
from pathlib import Path

import pytest

from switchyard import config, llm, pipeline

FIX = Path(__file__).parent / "fixtures" / "sample_app"


class FakeTF:
    """Scripted replies per role; candidates differ so the decision logic is exercised."""

    def __init__(self, *a, **k):
        self.usage = llm.Usage()
        self.fallbacks = []

    def complete(self, model_key, messages, *, role="candidate", **kw):
        text = self._reply(model_key, messages, role)
        self.usage.add(role, config.MODELS[model_key], 300, 60)
        return llm.Completion(text, None, 300, 60, 0.05, 0.0, model_key)

    def json(self, model_key, messages, *, role, **kw):
        c = self.complete(model_key, messages, role=role)
        return json.loads(c.text), c

    def _reply(self, mk, messages, role):
        user = messages[-1]["content"]
        if role == "analyze":
            if "triage" in user:
                return json.dumps({"task_type": "classification", "summary": "Labels support tickets", "inputs": [{"name": "ticket"}],
                                   "output": {"format": "label", "labels": ["billing", "bug", "feature", "other"]}, "quality_criteria": []})
            if "invoice" in user:
                return json.dumps({"task_type": "extraction", "summary": "Extracts invoice fields", "inputs": [{"name": "doc"}],
                                   "output": {"format": "json", "json_keys": ["vendor", "total", "due_date"]}})
            return json.dumps({"task_type": "summarization", "summary": "Summarizes articles", "output": {"format": "list"}})
        if role == "testgen":
            if "triage" in user:
                cases = [{"name": f"t{i}", "inputs": {"ticket": f"I was charged twice {i}"}, "reference": "billing", "expect": {"label": "billing"}} for i in range(4)]
            elif "invoice" in user:
                cases = [{"name": f"i{i}", "inputs": {"doc": "ACME total 10"}, "reference": '{"vendor":"ACME","total":10,"due_date":null}',
                          "expect": {"fields": {"vendor": "ACME"}}} for i in range(4)]
            else:
                cases = [{"name": f"s{i}", "inputs": {"article": "text", "str_n": "3"}, "reference": "- a\n- b\n- c"} for i in range(4)]
            return json.dumps({"cases": cases})
        if role == "judge":
            ref = user.split("Reference answer:\n", 1)[1].split("\n\nCandidate answer:\n", 1)
            good = ref[1].strip().lower().startswith(ref[0].strip().lower()[:4])
            return json.dumps({"score": 9 if good else 3, "pass": good, "reason": "ok" if good else "wrong"})
        if role == "repair":
            return json.dumps({"hint": "Reply with only the JSON object.", "rationale": "format"})
        # candidates
        sys_hint = any("only the JSON" in m["content"] for m in messages if m["role"] == "system")
        if "Ticket" in user:
            return "billing"
        if "invoice" in user:
            if mk == "lightning" and not sys_hint:
                return 'Sure! {"vendor":"ACME","total":10,"due_date":null}'
            return '{"vendor":"ACME","total":10,"due_date":null}'
        return "- a\n- b\n- c" if mk != "lightning" else "nope"


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path)
    r = pipeline.Run(str(FIX), tests=4, use_tavily=False)
    return r.run()


def test_pipeline_end_to_end(run):
    assert run["status"] == "done", run.get("error")
    by_fn = {s["site"]["function"]: s for s in run["sites"]}
    assert by_fn["triage"]["decision"]["choice"] == "lightning"
    # lightning adds chatter -> repaired with a hint, then passes
    assert by_fn["extract_invoice"]["decision"]["choice"] == "lightning+hint"
    assert by_fn["summarize"]["decision"]["model"] == "super"
    assert run["patch"]["ok"]
    assert "nvidia/Nemotron-3_5-Lightning" in run["patch"]["diff"]
    assert run["summary"]["migrated"] == 3 and run["summary"]["saving_pct"] > 50
