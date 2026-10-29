"""On-call: deployed apps become WispOps services; restart/rollback/release; Nemotron log reading (offline)."""
import json

import pytest

from switchyard import create, deploy, diagnose, llm, oncall


class Sh:
    def __init__(self):
        self.calls = []

    def run(self, cmd, input=None, cwd=None, stream=False, env=None, check=True):
        self.calls.append(tuple(cmd))
        return ""

    def nebius(self, *a, **k):
        self.calls.append(("nebius", *a))
        return "{}"


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(oncall, "HOME", tmp_path / "oncall")
    monkeypatch.setattr(oncall, "CONFIG", tmp_path / "oncall" / "wisprops.config.json")
    app = tmp_path / "notes"
    create.scaffold(app, {"name": "notes", "title": "Notes", "endpoints": [], "ai_features": []})
    deploy.save_state({"registry": None, "apps": {"notes": {
        "local": True, "container": "switchyard-notes", "image": "switchyard-local/notes:20261030-020000",
        "url": "http://127.0.0.1:5555", "path": str(app), "host_port": 5555, "container_port": 8080,
        "history": ["switchyard-local/notes:20261030-010000", "switchyard-local/notes:20261030-020000"]}}})
    return app


def test_config_turns_each_app_into_a_service(deployed):
    cfg = oncall.write_config()
    svc = next(s for s in cfg["services"] if s["id"] == "notes")
    assert svc["health"]["url"] == "http://127.0.0.1:5555/api/health"      # made by switchyard new
    assert svc["rollback"][-3:] == ["rollback", "notes", "{to}"] and svc["release"]["command"][-2:] == ["release", "notes"]
    assert cfg["settings"]["allowTyped"] is True                           # Wispr Flow optional
    assert oncall.write_config(voice=True)["settings"]["allowTyped"] is False


def test_user_services_and_settings_survive_a_rewrite(deployed):
    oncall.write_config()
    cfg = json.loads(oncall.CONFIG.read_text())
    cfg["services"].append({"id": "postgres", "health": "tcp://localhost:5432"})
    cfg["notify"]["ntfy"] = "my-topic"
    oncall.CONFIG.write_text(json.dumps(cfg))
    again = oncall.write_config()
    assert {s["id"] for s in again["services"]} == {"notes", "postgres"} and again["notify"]["ntfy"] == "my-topic"


def test_release_restart_rollback_local(deployed):
    sh = Sh()
    assert deploy.release("notes") == "20261030-020000"
    deploy.restart("notes", shell=sh)
    assert ("docker", "restart", "switchyard-notes") in sh.calls
    deploy.rollback("notes", "20261030-010000", shell=sh, emit=lambda *_: None)
    run = next(c for c in sh.calls if c[:2] == ("docker", "run"))
    assert run[-1] == "switchyard-local/notes:20261030-010000" and "127.0.0.1:5555:8080" in run
    assert deploy.release("notes") == "20261030-010000"
    with pytest.raises(deploy.DeployError, match="isn't a release"):
        deploy.rollback("notes", "nope", shell=sh)


def test_explain_reads_errors_not_noise(deployed):
    logs = "\n".join([f"INFO request {i} 200" for i in range(300)] + ["ERROR db pool exhausted: 0 connections"])
    assert "db pool exhausted" in diagnose.focus(logs)

    class TF:
        def complete(self, model, messages, **kw):
            assert "db pool exhausted" in messages[1]["content"] and "20261030-010000" in messages[1]["content"]
            return llm.Completion("What's failing: the API.\nFix now: switchyard deploy rollback notes 20261030-010000",
                                  None, 1, 1, 0.0, 0.0, model)
    out = diagnose.explain(logs, "notes", tf=TF())
    assert "rollback" in out
