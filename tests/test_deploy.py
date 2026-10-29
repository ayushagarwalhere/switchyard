"""`switchyard deploy` against a stand-in for the nebius CLI and docker (no cloud, no spend)."""
import json

import pytest

from switchyard import config, create, deploy
from switchyard import secure as deploy_secure


class FakeShell:
    def __init__(self):
        self.calls = []
        self.gets = 0

    def nebius(self, *args, input=None, check=True):
        self.calls.append(("nebius", *args))
        if args[:2] == ("registry", "create"):
            return json.dumps({"metadata": {"id": "registry-e00abc"}})
        if args[:2] == ("config", "get"):
            return "eu-north1\n"
        if args[:3] == ("vpc", "subnet", "list"):
            return json.dumps({"items": [{"metadata": {"id": "vpcsubnet-1"}}]})
        if args[:2] == ("iam", "get-access-token"):
            return "iam-token\n"
        if args[:3] == ("ai", "endpoint", "create"):
            return json.dumps({"metadata": {"id": "endpoint-42"}})
        if args[:3] == ("ai", "endpoint", "get"):
            self.gets += 1
            if self.gets < 2:
                return json.dumps({"status": {"state": "STARTING"}})
            return json.dumps({"status": {"state": "RUNNING", "public_endpoints": ["https://e-42.nebius.app"]}})
        return "{}"

    def run(self, cmd, input=None, cwd=None, stream=False, env=None, check=True):
        self.calls.append(tuple(cmd))
        self.env = env or getattr(self, "env", None)
        return "running" if cmd[:2] == ["docker", "inspect"] else ""


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    monkeypatch.setenv("NEBIUS_API_KEY", "tf-test-key")
    monkeypatch.setattr(deploy_secure, "osv_query", lambda deps: [[] for _ in deps])
    d = tmp_path / "notes"
    create.scaffold(d, {"name": "notes", "title": "Notes", "endpoints": [], "ai_features": []})
    (d / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (d / ".env").write_text("LLM_MODEL_SUMMARIZE=nvidia/Nemotron-3_5-Lightning\nOTHER_SECRET=x\n")
    return d


def test_deploy_end_to_end(app):
    sh = FakeShell()
    res = deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda url, tok: 200).run()
    assert res["url"] == "https://e-42.nebius.app" and res["healthy"] and res["endpoint_id"] == "endpoint-42"
    build = next(c for c in sh.calls if c[:2] == ("docker", "build"))
    image = build[build.index("-t") + 1]
    assert image.startswith("cr.eu-north1.nebius.cloud/e00abc/notes:") and "linux/amd64" in build
    create_call = next(c for c in sh.calls if c[:4] == ("nebius", "ai", "endpoint", "create"))
    flat = " ".join(create_call)
    assert "--auth none" in flat and "--container-port 8080" in flat and "--subnet-id vpcsubnet-1" in flat
    assert "NEBIUS_API_KEY=tf-test-key" in flat and "LLM_MODEL_SUMMARIZE=" in flat and "OTHER_SECRET" not in flat
    assert ("docker", "login", "cr.eu-north1.nebius.cloud", "--username", "iam", "--password-stdin") in sh.calls
    saved = json.loads(deploy.STATE.read_text())
    assert saved["registry"]["path"] == "e00abc" and saved["apps"]["notes"]["url"] == res["url"]
    assert "token" not in json.dumps(res) or res["token"] is None


def test_redeploy_replaces_old_endpoint_and_reuses_registry(app):
    sh = FakeShell()
    deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda u, t: 200).run()
    sh2 = FakeShell()
    deploy.Deployer(app, shell=sh2, emit=lambda *_: None, http=lambda u, t: 200).run()
    assert not any(c[:3] == ("nebius", "registry", "create") for c in sh2.calls)
    assert ("nebius", "ai", "endpoint", "delete", "endpoint-42") in sh2.calls


def test_blocking_findings_stop_the_deploy(app):
    (app / "app" / "main.py").write_text('KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"\n')
    sh = FakeShell()
    with pytest.raises(deploy.DeployError, match="blocking"):
        deploy.Deployer(app, shell=sh, emit=lambda *_: None).run()
    assert not any(c[:1] == ("docker",) for c in sh.calls)


def test_writes_a_dockerfile_when_missing(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.1.0\n")
    info = deploy.detect(tmp_path)
    assert info["kind"] == "flask"
    assert "USER appuser" in deploy.dockerfile(info) and "gunicorn" in deploy.dockerfile(info)
    (tmp_path / "requirements.txt").unlink()
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"next": "15.0.0"}}))
    assert deploy.detect(tmp_path)["kind"] == "next"


def test_status_logs_delete(app):
    sh = FakeShell()
    deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda u, t: 200).run()
    assert deploy.status("notes", shell=sh)["state"] == "RUNNING"
    assert deploy.delete("notes", shell=sh) == "notes"
    assert json.loads(deploy.STATE.read_text())["apps"] == {}
    assert config.HOME  # state lives under the Switchyard home, never in the app repo


def test_local_deploy_runs_the_same_image_without_nebius(app):
    sh = FakeShell()
    res = deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda u, t: 200, local=True).run()
    assert res["local"] and res["url"].startswith("http://127.0.0.1:") and res["healthy"]
    assert not any(c[0] == "nebius" for c in sh.calls)
    run = next(c for c in sh.calls if c[:2] == ("docker", "run"))
    assert "-e" in run and "NEBIUS_API_KEY" in run and not any("tf-test-key" in x for x in run)   # key not on the command line
    assert sh.env["NEBIUS_API_KEY"] == "tf-test-key"
    assert deploy.status("notes", shell=sh)["state"] == "running"
    deploy.delete("notes", shell=sh)
    assert ("docker", "rm", "-f", "switchyard-notes") in sh.calls
