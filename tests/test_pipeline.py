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
        if role == "investigate" and kw.get("tools"):
            self.usage.add(role, config.MODELS[model_key], 300, 60)
            if not any(m["role"] == "tool" for m in messages):   # step 1: look for callers
                fn = messages[-1]["content"].split("in function ", 1)[1].split("\n", 1)[0]
                return llm.Completion("", None, 300, 60, 0.05, 0.0, model_key,
                                      tool_calls=[{"id": "c1", "name": "search", "arguments": json.dumps({"pattern": fn})}])
            found = [m["content"] for m in messages if m["role"] == "tool"][0]
            return llm.Completion(json.dumps({"callers": [], "downstream": f"search found: {found[:80]}",
                                              "critical": ["label must be one of the four"], "input_examples": ["My card was charged twice"]}),
                                  None, 300, 60, 0.05, 0.0, model_key)
        # the original model (baseline) answers like Super
        text = self._reply("super" if model_key.startswith("source:") else model_key, messages, role)
        self.usage.add(role, config.model(model_key), 300, 60)
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
        if role == "harness":   # write the call into the patched code (only reached when names don't line up)
            fn = user.split("Function to call: ", 1)[1].split(" ", 1)[0]
            return json.dumps({"code": f"return m.{fn}(*inputs.values())"})
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


def test_investigation_runs_tools_and_feeds_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    state = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=False).run()
    by_fn = {s["site"]["function"]: s for s in state["sites"]}
    inv = by_fn["triage"]["investigation"]
    assert inv["mode"] == "agent"
    assert inv["steps"][0]["tool"] == "search" and inv["steps"][0]["args"] == {"pattern": "triage"}
    assert "app.py:" in inv["downstream"]          # the tool result really reached the model
    assert inv["input_examples"] == ["My card was charged twice"]


def test_investigation_falls_back_when_tools_are_refused(tmp_path):
    from switchyard import investigate, scanner

    class NoTools(FakeTF):
        def complete(self, model_key, messages, *, role="candidate", **kw):
            if kw.get("tools"):
                return llm.Completion("", None, 0, 0, 0.0, 0.0, model_key, error="BadRequestError: tools not supported")
            return llm.Completion(json.dumps({"downstream": "returned to caller", "critical": []}), None, 1, 1, 0, 0, model_key)

    site = next(c for c in scanner.scan_repo(FIX)["calls"] if c["function"] == "triage")
    inv = investigate.investigate(NoTools(), FIX, site)
    assert inv["mode"] == "static" and inv["downstream"] == "returned to caller" and inv["steps"] == []


def test_repo_tools_stay_inside_the_repo(tmp_path):
    from switchyard.investigate import RepoTools
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text("x = 1\n")
    (tmp_path / "repo-evil").mkdir()
    (tmp_path / "repo-evil" / "secret.py").write_text("KEY = 'nope'\n")
    t = RepoTools(tmp_path / "repo")
    assert "no such file" in t.read_file("../repo-evil/secret.py")
    assert t.read_file("a.py").startswith("1: x = 1")


def test_original_model_baseline(monkeypatch, tmp_path):
    class WithBaseline(FakeTF):
        baseline = object()          # pretend an OpenAI key is set

    monkeypatch.setattr(pipeline, "TokenFactory", WithBaseline)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    state = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=False).run()
    by_fn = {s["site"]["function"]: s for s in state["sites"]}
    assert "source:gpt-4o" in by_fn["extract_invoice"]["evals"]
    vs = by_fn["extract_invoice"]["decision"]["vs_source"]
    assert vs["model"] == "gpt-4o" and vs["chosen_score"] is not None and "delta" in vs
    assert "vs_source" not in by_fn["summarize"]["decision"]      # model picked at runtime: nothing to compare
    assert by_fn["extract_invoice"]["decision"]["choice"] != "source:gpt-4o"


def test_original_model_raises_the_bar():
    def ev(p, s):
        return {"pass_rate": p, "score": s, "errors": 0, "hint": ""}
    evals = {"lightning": ev(1.0, 0.88), "super": ev(1.0, 0.90), "ultra": ev(1.0, 0.90)}
    assert pipeline.decide(evals, 0.85)["choice"] == "lightning"
    evals["source:gpt-4o"] = ev(1.0, 0.97)       # the app's current model is clearly better
    d = pipeline.decide(evals, 0.85)
    assert d["action"] == "keep"


def test_errored_baseline_is_not_reported_as_a_head_to_head():
    evals = {"lightning": {"pass_rate": 1.0, "score": 0.9, "errors": 0, "hint": ""},
             "source:gpt-4o": {"pass_rate": 0.0, "score": 0.0, "errors": 8, "hint": ""}}   # e.g. a dead OpenAI key
    d = pipeline.decide(evals, 0.85)
    assert d["choice"] == "lightning"
    vs = pipeline.head_to_head(evals, d, "source:gpt-4o")
    assert vs["errors"] == 8 and "chosen_score" not in vs


def test_investigation_handles_tool_calls_returned_as_text():
    from switchyard import investigate, scanner

    class TextTools(FakeTF):
        def complete(self, model_key, messages, *, role="candidate", **kw):
            if not any("Tool results" in m["content"] for m in messages if m["role"] == "user"):
                return llm.Completion('<TOOLCALL>[{"name": "search", "arguments": {"pattern": "triage"}}]</TOOLCALL>',
                                      None, 1, 1, 0, 0, model_key)
            return llm.Completion(json.dumps({"callers": ["app.py:30"], "downstream": "routes the ticket",
                                              "critical": "label", "input_examples": "card charged twice"}),
                                  None, 1, 1, 0, 0, model_key)

    site = next(c for c in scanner.scan_repo(FIX)["calls"] if c["function"] == "triage")
    inv = investigate.investigate(TextTools(), FIX, site)
    assert inv["mode"] == "agent" and inv["steps"][0]["tool"] == "search"
    assert inv["callers"] == [{"where": "app.py:30", "how": ""}]
    assert inv["critical"] == ["label"] and inv["input_examples"] == ["card charged twice"]
    assert "app.py:30" in investigate.as_context(inv)
