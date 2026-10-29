"""Tavily: key aliases, on-disk cache (re-runs don't spend quota) and the per-run cap. No network."""
import json
import sys
import types

from switchyard import config, pipeline
from test_pipeline import FIX, FakeTF


def fake_tavily(monkeypatch, calls):
    class Client:
        def __init__(self, api_key):
            assert api_key == "tv-test"

        def search(self, query, **kw):
            calls.append(query)
            return {"results": [{"title": "t", "url": "https://example.com/a", "content": f"about {query}"}]}

    monkeypatch.setitem(sys.modules, "tavily", types.SimpleNamespace(TavilyClient=Client))


def test_misspelled_key_name_is_accepted(monkeypatch):
    monkeypatch.setenv("TAVLY_API_KEY", "tv-test")
    assert config.tavily_key() == "tv-test"


def test_same_query_is_searched_once(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test")
    calls = []
    fake_tavily(monkeypatch, calls)
    first = pipeline.tavily_sources("Calendar requests  examples")
    again = pipeline.tavily_sources("calendar requests examples")
    assert calls == ["Calendar requests  examples"] and first == again


def test_failed_search_is_not_cached(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test")

    class Broken:
        def __init__(self, api_key):
            pass

        def search(self, *a, **kw):
            raise RuntimeError("quota exceeded")

    monkeypatch.setitem(sys.modules, "tavily", types.SimpleNamespace(TavilyClient=Broken))
    errors = []
    assert pipeline.tavily_sources("q", errors) == []
    assert not pipeline._tavily_cache("q").exists()
    assert errors == ["RuntimeError: quota exceeded"]


def test_missing_package_is_reported(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test")
    monkeypatch.setitem(sys.modules, "tavily", None)      # import fails
    errors = []
    assert pipeline.tavily_sources("q", errors) == [] and "pip install tavily-python" in errors[0]


class SearchingTF(FakeTF):
    """The analysis asks for real-world inputs (a different query per call)."""

    def _reply(self, mk, messages, role):
        text = super()._reply(mk, messages, role)
        if role == "analyze":
            a = json.loads(text)
            a["realistic_input_search"] = a["summary"] + " examples"
            text = json.dumps(a)
        return text


def test_per_run_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", SearchingTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test")
    monkeypatch.setattr(config, "TAVILY_MAX_PER_RUN", 1)
    calls = []
    fake_tavily(monkeypatch, calls)
    state = pipeline.Run(str(FIX), tests=4, execute=False, audit=False).run()
    with_sources = [s for s in state["sites"] if s.get("sources")]
    assert len(state["sites"]) > 1 and len(calls) == 1 and len(with_sources) == 1


def test_langsmith_traces_only_agent_steps_and_stops_at_cap(monkeypatch):
    """LangSmith quota: test and grading calls are never traced, and a run stops tracing at the cap."""
    from switchyard import llm
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setattr(config, "TRACE", "agents")
    monkeypatch.setattr(config, "TRACE_MAX_PER_RUN", 2)
    used = []

    class Client:
        def __init__(self, name):
            self.name = name
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kw):
            used.append(self.name)
            msg = types.SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                         usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    tf = llm.TokenFactory(tracer=lambda c: Client("traced"))
    tf.client = Client("plain")
    for role in ("candidate", "judge", "analyze", "testgen", "investigate", "candidate"):
        tf.complete("lightning", [{"role": "user", "content": "hi"}], role=role)
    assert used == ["plain", "plain", "traced", "traced", "plain", "plain"] and tf.traces_sent == 2


def test_langsmith_off_switch(monkeypatch):
    monkeypatch.setenv("LANGCHAIN_API_KEY", "ls-test")
    monkeypatch.setattr(config, "TRACE", "agents")
    assert config.langsmith_key() == "ls-test"
    monkeypatch.setattr(config, "TRACE", "0")
    assert config.langsmith_key() is None
