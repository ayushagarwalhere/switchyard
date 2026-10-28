"""Running the migrated code for real, against a local stand-in for Token Factory."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from switchyard import config, llm, pipeline, runcheck
from test_pipeline import FIX, FakeTF

ANSWER = '{"vendor": "ACME", "total": 10, "due_date": null}'


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(body["model"])
        if "FAIL400" in json.dumps(body):
            err = b'{"error": {"message": "bad request", "type": "invalid_request_error"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return
        out = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                          "choices": [{"index": 0, "finish_reason": "stop",
                                       "message": {"role": "assistant", "content": ANSWER}}],
                          "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_token_factory(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TOKEN_FACTORY_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/v1/")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    yield srv
    srv.shutdown()


def test_pipeline_runs_migrated_code(monkeypatch, tmp_path, fake_token_factory):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    state = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=True).run()
    assert state["status"] == "done", state.get("error")
    rt = {r["function"]: r for r in state["patch"]["runtime"]}
    assert set(rt) == {"triage", "extract_invoice", "summarize"}
    for r in rt.values():
        assert r["status"] == "verified", r
    assert rt["triage"]["how"] == rt["summarize"]["how"] == "direct"
    # extract_invoice(text) fills the prompt's {doc}: names differ, so Nemotron writes the call
    assert rt["extract_invoice"]["how"] == "agent"
    # extract_invoice really parsed the JSON answer in the app's own code
    assert rt["extract_invoice"]["returned"] == "dict"
    assert config.MODELS["lightning"].id in fake_token_factory.seen


class AgentTF(FakeTF):
    """First harness attempt is wrong; the retry gets the traceback and fixes it."""
    tries = 0

    def _reply(self, mk, messages, role):
        if role != "harness":
            return super()._reply(mk, messages, role)
        AgentTF.tries += 1
        if "previous attempt failed" not in messages[-1]["content"]:
            return json.dumps({"code": "return m.confirm(inputs['details'])"})  # passes a str, needs an Event
        return json.dumps({"code": "ev = m.Event.model_validate_json(inputs['details'])\nreturn m.confirm(ev)"})


APP = '''
import os
from openai import OpenAI
from pydantic import BaseModel

client = OpenAI(base_url=os.environ["TOKEN_FACTORY_BASE_URL"], api_key=os.environ["NEBIUS_API_KEY"])


class Event(BaseModel):
    name: str


def confirm(event: Event) -> str:
    r = client.chat.completions.create(model=os.environ.get("LLM_MODEL_CONFIRM", "MODEL_ID"),
                                       messages=[{"role": "user", "content": f"Confirm {event.name}"}])
    return r.choices[0].message.content


result = confirm("this would crash if top-level demo code ran")
'''


def test_agent_writes_and_fixes_the_call(tmp_path, fake_token_factory):
    model_id = config.MODELS["super"].id
    (tmp_path / "app.py").write_text(APP.replace("MODEL_ID", model_id), encoding="utf-8")
    site = {"id": "s1", "file": "app.py", "line": 14, "function": "confirm", "variables": ["event_name"]}
    s = {"site": site, "status": "done", "decision": {"action": "migrate", "model": "super"},
         "cases": [{"name": "c1", "kind": "typical", "inputs": {"details": '{"name": "standup"}', "tone": "formal"}}]}
    AgentTF.tries = 0
    r = runcheck.verify(AgentTF(), tmp_path, [s])[0]
    assert r["status"] == "verified", r
    assert r["how"] == "agent" and r["attempts"] == 2 and AgentTF.tries == 2
    assert fake_token_factory.seen == [model_id]   # the top-level demo call never ran


def test_failure_after_model_answer_is_reported(tmp_path, fake_token_factory):
    (tmp_path / "app.py").write_text(APP.replace("return r.choices[0].message.content", "return int(r.choices[0].message.content)")
                                     .replace("MODEL_ID", config.MODELS["super"].id), encoding="utf-8")
    site = {"id": "s1", "file": "app.py", "line": 14, "function": "confirm", "variables": []}
    s = {"site": site, "status": "done", "decision": {"action": "migrate", "model": "super"},
         "cases": [{"name": "c1", "kind": "typical", "inputs": {"event": "x"}}]}

    class Direct(FakeTF):
        def _reply(self, mk, messages, role):
            return json.dumps({"code": "return m.confirm(m.Event(name=inputs['event']))"})

    r = runcheck.verify(Direct(), tmp_path, [s])[0]
    assert r["status"] == "failed" and "ValueError" in r["reason"]


def test_doctor_reports_supported_and_missing_features(fake_token_factory):
    from switchyard import doctor
    import os
    rows = doctor.run(models=[config.MODELS["lightning"]], base_url=os.environ["TOKEN_FACTORY_BASE_URL"], key="k")
    r = rows[0]
    assert r["chat"]["ok"] and r["json_object"]["ok"]          # the stand-in answers chat with JSON
    assert not r["tools"]["ok"] and not r["responses"]["ok"]   # ...but has no tool calls or /responses
    md = doctor.markdown(rows)
    assert "Nemotron-3_5-Lightning" in md and "investigation agent falls back" in md


SWALLOW = '''
import os
import some_module_this_env_lacks
from openai import OpenAI

some_module_this_env_lacks.apply()
client = OpenAI(base_url=os.environ["TOKEN_FACTORY_BASE_URL"], api_key=os.environ["NEBIUS_API_KEY"])


def ask(question):
    try:
        r = client.chat.completions.create(model="MODEL_ID", messages=[{"role": "user", "content": question}])
        return r.choices[0].message.content
    except Exception:
        return None  # apps often swallow API errors
'''


def test_missing_import_is_stubbed_and_swallowed_http_error_is_not_verified(tmp_path, fake_token_factory):
    (tmp_path / "app.py").write_text(SWALLOW.replace("MODEL_ID", config.MODELS["super"].id), encoding="utf-8")
    site = {"id": "s1", "file": "app.py", "line": 11, "function": "ask", "variables": ["question"]}
    mk = lambda q: {"site": site, "status": "done", "decision": {"action": "migrate", "model": "super"},
                    "cases": [{"name": "c1", "kind": "typical", "inputs": {"question": q}}]}
    ok = runcheck.verify(FakeTF(), tmp_path, [mk("hello")])[0]
    assert ok["status"] == "verified", ok
    assert ok["requests"][0]["status"] == 200
    bad = runcheck.verify(FakeTF(), tmp_path, [mk("FAIL400 please")])[0]
    assert bad["status"] == "unverified" and "HTTP 400" in bad["reason"], bad
