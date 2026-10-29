"""`switchyard code`: the agent loop with a scripted model (no API calls)."""
import json

from switchyard import coder, create, llm


class ScriptTF:
    def __init__(self, script):
        self.script = list(script)
        self.usage = llm.Usage()

    def complete(self, model, messages, **kw):
        step = self.script.pop(0)
        if isinstance(step, str):
            return llm.Completion(step, None, 1, 1, 0.0, 0.0, model)
        calls = [{"id": f"c{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(step)]
        return llm.Completion("", None, 1, 1, 0.0, 0.0, model, tool_calls=calls)


def _app(tmp_path):
    create.scaffold(tmp_path, {"name": "n", "title": "N", "endpoints": [], "ai_features": []})
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "static" / "index.html").write_text("<html></html>\n")
    return tmp_path


def test_agent_writes_runs_tests_and_summarises(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(coder, "run_tests", lambda root: (True, "1 passed"))
    tf = ScriptTF([
        [("read_file", {"path": "app/main.py"})],
        [("write_file", {"path": "app/extra.py", "content": "X = 1"})],
        [("run_tests", {})],
        json.dumps({"summary": "Added extra.py", "files": ["app/extra.py"]}),
    ])
    res = coder.code("add extra", app, tf=tf, emit=lambda *_: None)
    assert res["summary"] == "Added extra.py" and res["files"] == ["app/extra.py"] and res["tests_ok"]
    assert (app / "app" / "extra.py").read_text() == "X = 1\n"


def test_agent_cannot_write_outside_the_app_or_env(tmp_path):
    tools = coder.RepoTools(_app(tmp_path))
    assert tools.call("write_file", {"path": "../evil.py", "content": "x"}).startswith("error")
    assert tools.call("write_file", {"path": ".env", "content": "K=1"}).startswith("error")
    assert not (tmp_path.parent / "evil.py").exists()


def test_agent_stops_after_tests_stay_green(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr(coder, "run_tests", lambda root: (True, "ok"))
    loop = [[("write_file", {"path": "app/a.py", "content": "A = 1"}), ("run_tests", {})]] * 10
    res = coder.code("x", app, tf=ScriptTF(loop), emit=lambda *_: None)
    assert res["steps"] == 2 and "tests pass" in res["summary"]
