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


def test_switchyard_demo_settings_and_tavily_key_ship_with_the_app(app):
    import os
    (app / ".env").write_text("LLM_MODEL_SUMMARIZE=nvidia/Nemotron-3_5-Lightning\nOTHER_SECRET=x\n"
                              "SWITCHYARD_PUBLIC=1\nSWITCHYARD_RUN_BUDGET=0.50\nSWITCHYARD_RUN_CODE=old-code\n"
                              "TAVLY_API_KEY=tvly-typo-name\n")
    (app / ".env.local").write_text("SWITCHYARD_RUN_CODE='demo-code'\nexport SWITCHYARD_EXEC=0\n"
                                    "SWITCHYARD_EXEC_TIMEOUT=5\n")
    env = deploy.app_env(app)
    assert env["SWITCHYARD_PUBLIC"] == "1" and env["SWITCHYARD_RUN_BUDGET"] == "0.50" and env["SWITCHYARD_EXEC"] == "0"
    assert env["SWITCHYARD_RUN_CODE"] == "demo-code"           # .env.local wins, quotes stripped
    assert env["TAVILY_API_KEY"] == "tvly-typo-name"           # any of config.TAVILY_NAMES, under the main name
    assert "OTHER_SECRET" not in env and "SWITCHYARD_EXEC_TIMEOUT" not in env and "TAVLY_API_KEY" not in env

    sh = FakeShell()
    deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda u, t: 200).run()
    create_call = next(c for c in sh.calls if c[:4] == ("nebius", "ai", "endpoint", "create"))
    envs = {create_call[i + 1] for i, a in enumerate(create_call) if a == "--env"}
    assert {"SWITCHYARD_PUBLIC=1", "SWITCHYARD_RUN_CODE=demo-code", "SWITCHYARD_EXEC=0",
            "TAVILY_API_KEY=tvly-typo-name", "NEBIUS_API_KEY=tf-test-key"} <= envs
    # a CPU platform and the small preset by default (Nebius would otherwise pick a GPU platform)
    flat = " ".join(create_call)
    assert f"--platform {deploy.DEFAULT_PLATFORM}" in flat and f"--preset {deploy.DEFAULT_PRESET}" in flat
    if not os.environ.get("SWITCHYARD_NEBIUS_PRESET"):
        assert deploy.DEFAULT_PRESET == "2vcpu-8gb"
    if not os.environ.get("SWITCHYARD_NEBIUS_PLATFORM"):
        assert deploy.DEFAULT_PLATFORM == "cpu-d3"

    # locally the values go through the environment, never the docker command line
    sh2 = FakeShell()
    deploy.Deployer(app, shell=sh2, emit=lambda *_: None, http=lambda u, t: 200, local=True).run()
    run = next(c for c in sh2.calls if c[:2] == ("docker", "run"))
    assert "SWITCHYARD_PUBLIC" in run and "TAVILY_API_KEY" in run
    assert not any("demo-code" in x or "tvly-" in x for x in run)
    assert sh2.env["SWITCHYARD_RUN_CODE"] == "demo-code"


def test_deploy_records_timings_and_a_shareable_record(app, tmp_path):
    sh = FakeShell()
    res = deploy.Deployer(app, shell=sh, emit=lambda *_: None, http=lambda u, t: 200, private=True,
                          include_key=False).run()
    assert {"build_s", "push_s", "endpoint_ready_s", "first_response_s", "cold_start_s", "total_s"} <= set(res["timings"])
    flat = " ".join(next(c for c in sh.calls if c[:4] == ("nebius", "ai", "endpoint", "create")))
    assert "NEBIUS_API_KEY" not in flat and "LLM_MODEL_SUMMARIZE=" in flat
    saved = json.loads(deploy.STATE.read_text())["apps"]["notes"]
    path = deploy.write_record({**saved, "name": "notes", "secure": res["secure"]}, tmp_path / "records")
    rec = json.loads(path.read_text())
    assert rec["url"] == "https://e-42.nebius.app" and rec["app"] == "notes" and rec["timings"] == res["timings"]
    assert "token" not in rec and "path" not in rec and "history" not in rec


def test_findings_in_files_the_image_leaves_out_do_not_block(app):
    (app / "fixtures").mkdir(exist_ok=True)
    (app / "fixtures" / "fake.py").write_text('KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"\n')
    with pytest.raises(deploy.DeployError, match="blocking"):
        deploy.Deployer(app, shell=FakeShell(), emit=lambda *_: None, http=lambda u, t: 200).run()
    (app / ".dockerignore").write_text("fixtures\n!keep\n")
    res = deploy.Deployer(app, shell=FakeShell(), emit=lambda *_: None, http=lambda u, t: 200).run()
    assert res["healthy"]
    ig = deploy.dockerignored(app)
    assert ig("fixtures/fake.py") and not ig("app/main.py") and not ig("keep")
