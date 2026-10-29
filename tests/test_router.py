"""The runtime router, offline: an httpx MockTransport stands in for Token Factory."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from switchyard import config, router

LIGHTNING, SUPER, ULTRA = (config.MODELS[k].id for k in ("lightning", "super", "ultra"))
DEMO_RUN = Path(__file__).resolve().parent.parent / "runs" / "demo" / "20261029-142754-6b1f" / "run.json"
AUTH = {"Authorization": "Bearer test-router-key"}


class FakeUpstream:
    """Answers per model: "ok" (default), an int status, "timeout", "empty", or "stream"."""

    def __init__(self, behavior=None, usage=(100, 20)):
        self.behavior = behavior or {}
        self.usage = usage
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake-tf-key"   # the router's own upstream key
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": LIGHTNING, "object": "model"}]})
        body = json.loads(request.content)
        self.calls.append(body)
        what = self.behavior.get(body["model"], "ok")
        if what == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(what, int):
            return httpx.Response(what, json={"error": {"message": "boom"}})
        if body.get("stream"):
            return httpx.Response(200, content=sse(body["model"], self.usage), headers={"content-type": "text/event-stream"})
        content = "" if what == "empty" else f"answer from {body['model']}"
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]}})


def sse(model, usage) -> bytes:
    chunks = [{"choices": [{"delta": {"content": "Hel"}}], "model": model},
              {"choices": [{"delta": {"content": "lo"}}], "model": model},
              {"choices": [], "model": model, "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}]
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-tf-key")
    monkeypatch.setenv("SWITCHYARD_ROUTER_KEY", "test-router-key")
    monkeypatch.setattr(router, "_meter", None)


def make(routes=None, **behavior):
    fake = FakeUpstream(behavior.pop("behavior", None), **behavior)
    app = router.create_app(routes=routes or {}, upstream="http://upstream.test/v1", transport=httpx.MockTransport(fake))
    return TestClient(app), fake


def chat(client, model, headers=None, **extra):
    return client.post("/v1/chat/completions", headers={**AUTH, **(headers or {})},
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra})


def test_routes_by_header_and_by_model_alias():
    c, fake = make({"summarize": {"model": "lightning"}})
    with c:
        r = chat(c, "gpt-4o", headers={"X-Switchyard-Feature": "summarize"})
        assert r.status_code == 200 and r.headers["x-switchyard-model"] == LIGHTNING
        assert fake.calls[-1]["model"] == LIGHTNING
        for alias in ("switchyard/summarize", "switchyard:summarize", "switchyard/SUMMARIZE"):
            r = chat(c, alias)
            assert r.status_code == 200 and r.headers["x-switchyard-feature"] == "summarize", alias
            assert fake.calls[-1]["model"] == LIGHTNING
        # the tiers are routes out of the box
        assert chat(c, "switchyard/ultra").json()["model"] == ULTRA
        assert chat(c, "switchyard/nope").status_code == 404
    assert router.stats_summary()["features"]["summarize"]["calls"] == 4


def test_plain_model_passes_through_and_is_metered():
    c, fake = make()
    with c:
        r = chat(c, "meta-llama/Llama-3.3-70B-Instruct", temperature=0.3)
        assert r.status_code == 200
        assert fake.calls[-1]["model"] == "meta-llama/Llama-3.3-70B-Instruct" and fake.calls[-1]["temperature"] == 0.3
    s = router.stats_summary()["features"][router.DIRECT]
    assert s["calls"] == 1 and s["tokens_in"] == 100 and s["models"] == {"meta-llama/Llama-3.3-70B-Instruct": 1}


@pytest.mark.parametrize("failure", [500, 503, 429, "timeout", "empty"])
def test_falls_back_to_the_next_model(failure):
    c, fake = make({"tag": {"model": "lightning"}}, behavior={LIGHTNING: failure})
    with c:
        r = chat(c, "switchyard/tag")
        assert r.status_code == 200
        assert r.headers["x-switchyard-model"] == SUPER and r.headers["x-switchyard-fallbacks"] == "1"
        assert [b["model"] for b in fake.calls] == [LIGHTNING, SUPER]
    s = router.stats_summary()
    assert s["features"]["tag"]["fallbacks"] == 1 and s["features"]["tag"]["models"] == {SUPER: 1}
    assert s["recent"][-1]["fallbacks"][0]["from"] == LIGHTNING


def test_whole_chain_failing_returns_the_last_error():
    c, fake = make({"tag": {"model": "lightning"}}, behavior={LIGHTNING: 500, SUPER: "timeout", ULTRA: 502})
    with c:
        r = chat(c, "switchyard/tag")
        assert r.status_code == 502 and r.headers["x-switchyard-fallbacks"] == "2"
        assert [b["model"] for b in fake.calls] == [LIGHTNING, SUPER, ULTRA]
    f = router.stats_summary()["features"]["tag"]
    assert f["errors"] == 1 and f["fallbacks"] == 2 and f["cost"] == 0
    # a 4xx is the request's fault: no fallback
    c2, fake2 = make({"tag": {"model": "lightning"}}, behavior={LIGHTNING: 400})
    with c2:
        assert chat(c2, "switchyard/tag").status_code == 400 and len(fake2.calls) == 1


def test_hint_is_prepended_as_a_system_message():
    c, fake = make({"classify": {"model": "lightning", "hint": "Answer with one word."}})
    with c:
        chat(c, "switchyard/classify")
        assert fake.calls[-1]["messages"] == [{"role": "system", "content": "Answer with one word."},
                                              {"role": "user", "content": "hi"}]
        # an app already patched by the migration carries the hint: don't add it twice
        c.post("/v1/chat/completions", headers=AUTH, json={"model": "switchyard/classify", "messages": [
            {"role": "system", "content": "Answer with one word."}, {"role": "user", "content": "hi"}]})
        assert len(fake.calls[-1]["messages"]) == 2


def test_auth():
    c, fake = make()
    with c:
        assert c.get("/healthz").status_code == 200
        body = {"model": "switchyard/lightning", "messages": []}
        assert c.post("/v1/chat/completions", json=body).status_code == 401
        r = c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401 and "router key" in r.json()["error"]["message"]
        # the Token Factory key is not the router key
        assert c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer fake-tf-key"}).status_code == 401
        assert c.get("/stats").status_code == 401 and c.get("/routes").status_code == 401
    assert fake.calls == []


def test_router_key_is_generated_once(monkeypatch):
    monkeypatch.delenv("SWITCHYARD_ROUTER_KEY")
    key = router.router_key()
    assert key.startswith("sy-") and len(key) > 30
    assert router.router_key() == key
    assert (config.HOME / "router.key").read_text().strip() == key


def test_routes_from_run_on_the_demo_run():
    routes = router.routes_from_run(DEMO_RUN)
    assert len(routes) == 11
    migrated = {k: v for k, v in routes.items() if v["model"] != router.KEEP}
    assert len(migrated) == 7 and all(v["model"] == LIGHTNING for v in migrated.values())
    assert all(v["fallback"] == [SUPER, ULTRA] for v in migrated.values())
    assert routes["extract_event_info"]["hint"].startswith("Classify as calendar event")
    assert routes["extract_event_info"]["source_model"] == "gpt-4o"
    assert routes["parse_event_details"]["hint"] == ""
    kept = {k for k, v in routes.items() if v["model"] == router.KEEP}
    assert kept == {"generate_confirmation", "get_plan", "write_section", "review_post"}
    assert router.routes_from_run(DEMO_RUN.parent) == routes     # the run folder works too


def test_run_routes_drive_requests_and_baseline_cost():
    routes = router.routes_from_run(DEMO_RUN)
    c, fake = make(routes, usage=(1000, 500))
    with c:
        chat(c, "switchyard/check_security")
        chat(c, "switchyard/check_security")
        assert fake.calls[-1]["model"] == LIGHTNING
        # a kept site only works with the app's real model named in `model`
        assert chat(c, "switchyard/get_plan").status_code == 400
        chat(c, "nvidia/some-model", headers={"X-Switchyard-Feature": "get_plan"})
        assert fake.calls[-1]["model"] == "nvidia/some-model"
    f = router.stats_summary()["features"]["check_security"]
    lightning, gpt4o = config.MODELS["lightning"], config.model("source:gpt-4o")
    assert f["calls"] == 2 and f["tokens_in"] == 2000 and f["tokens_out"] == 1000
    assert f["cost"] == pytest.approx(2 * lightning.cost(1000, 500))
    assert f["baseline_model"] == "gpt-4o" and f["baseline_cost"] == pytest.approx(2 * gpt4o.cost(1000, 500))
    assert f["saved"] == pytest.approx(f["baseline_cost"] - f["cost"]) and f["saving_pct"] > 90
    assert f["latency_p50_ms"] is not None and f["latency_p95_ms"] >= f["latency_p50_ms"]


def test_streaming_passes_bytes_through():
    c, fake = make({"chat": {"model": "lightning"}}, behavior={LIGHTNING: 500}, usage=(70, 9))
    with c:
        with c.stream("POST", "/v1/chat/completions", headers=AUTH,
                      json={"model": "switchyard/chat", "messages": [], "stream": True}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["x-switchyard-model"] == SUPER
            got = r.read()
        assert got == sse(SUPER, (70, 9))
    f = router.stats_summary()["features"]["chat"]
    assert f["streams"] == 1 and f["fallbacks"] == 1 and (f["tokens_in"], f["tokens_out"]) == (70, 9)


def test_models_lists_upstream_and_aliases():
    c, _ = make({"summarize": "super"})
    with c:
        ids = [m["id"] for m in c.get("/v1/models", headers=AUTH).json()["data"]]
    assert LIGHTNING in ids and "switchyard/summarize" in ids and "switchyard/ultra" in ids


def test_routes_file_round_trip_and_stats_file():
    path = router.save_routes({"summarize": "lightning", "lightning": "lightning"})
    saved = json.loads(path.read_text())["routes"]
    assert list(saved) == ["summarize"]           # defaults are not frozen into the file
    routes = router.load_routes()
    assert routes["summarize"]["model"] == LIGHTNING and routes["summarize"]["fallback"] == [SUPER, ULTRA]
    assert routes["ultra"]["model"] == ULTRA and routes["ultra"]["fallback"] == []
    c, _ = make(routes)
    with c:
        assert c.get("/routes", headers=AUTH).json()["routes"]["summarize"]["model"] == LIGHTNING
        chat(c, "switchyard/summarize")
    # the stats file is written on shutdown (and every few seconds while running)
    stats = json.loads((config.HOME / "router_stats.json").read_text())
    assert stats["features"]["summarize"]["calls"] == 1 and "recent" not in stats


def test_snippet():
    s = router.snippet(9000)
    assert len(s.splitlines()) == 3
    assert 'base_url="http://127.0.0.1:9000/v1"' in s and "SWITCHYARD_ROUTER_KEY" in s and 'model="switchyard/' in s
