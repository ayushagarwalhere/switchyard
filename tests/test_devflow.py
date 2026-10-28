"""The developer path: run inside your own checkout, then `switchyard apply` with no arguments."""
import json
import shutil
import subprocess

import pytest

from switchyard import check as check_mod
from switchyard import cli, config, gitops, onboard, pipeline
from test_pipeline import FIX, FakeTF


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def monorepo(tmp_path):
    """A git repo whose LLM app lives in services/support/."""
    root = tmp_path / "mono"
    shutil.copytree(FIX, root / "services" / "support")
    (root / "README.md").write_text("hi\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(check_mod, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")


def test_run_in_subfolder_then_apply_with_no_arguments(offline, monorepo):
    app = monorepo / "services" / "support"
    state = pipeline.Run(str(app), tests=4, use_tavily=False, execute=False).run()
    assert state["status"] == "done", state.get("error")
    co = state["checkout"]
    assert co["subdir"] == "services/support" and not co["dirty"] and co["commit"]

    res = gitops.apply()                       # latest run, the checkout it scanned, the right folder
    assert res["branch"].startswith("switchyard/") and res["subdir"] == "services/support"
    assert "Nemotron" in (app / "app.py").read_text()
    assert (app / ".env.switchyard.example").exists()
    wf = (monorepo / ".github" / "workflows" / "switchyard-check.yml").read_text()
    assert "secrets.NEBIUS_API_KEY" in wf and 'path: "services/support"' in wf
    saved = json.loads((monorepo / ".switchyard" / "run.json").read_text())
    assert "events" not in saved and "checkout" not in saved
    assert saved["source"] == "support"        # no absolute paths from the developer's machine
    assert saved["sites"] and saved["sites"][0]["cases"]


def test_apply_to_an_explicit_app_folder_infers_the_offset(offline, monorepo, tmp_path):
    state = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=False).run()
    assert state["status"] == "done"
    res = gitops.apply(state["id"], str(monorepo / "services" / "support"), ci=False)
    assert res["subdir"] == "services/support"
    assert not (monorepo / ".github").exists()


def test_check_reads_the_ci_copy(offline, monorepo):
    app = monorepo / "services" / "support"
    pipeline.Run(str(app), tests=4, use_tavily=False, execute=False).run()
    gitops.apply()
    res = check_mod.check(str(monorepo / ".switchyard" / "run.json"), repo=str(app), emit=lambda *_: None)
    assert res["ok"] and len(res["results"]) == 3
    assert not any(r.get("prompt_changed") for r in res["results"])


def test_dry_run_needs_no_key(monkeypatch, capsys):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TOKEN_FACTORY_API_KEY", raising=False)
    assert cli.main(["run", str(FIX), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Found 3 LLM call(s)" in out and "costs about $" in out


def test_no_key_outside_a_terminal_explains_setup(monkeypatch, capsys):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TOKEN_FACTORY_API_KEY", raising=False)
    monkeypatch.setattr(onboard, "interactive", lambda: False)
    assert cli.main(["run", str(FIX), "--yes"]) == 2
    assert "switchyard setup" in capsys.readouterr().out


def test_cli_run_applies_when_asked(offline, monorepo, capsys):
    app = monorepo / "services" / "support"
    assert cli.main(["run", str(app), "--yes", "--apply", "--tests", "4", "--no-tavily", "--no-exec"]) == 0
    out = capsys.readouterr().out
    assert "3 of 3 call site(s) switched" in out and "Applied on new branch switchyard/" in out
    assert _git(monorepo, "rev-parse", "--abbrev-ref", "HEAD").strip().startswith("switchyard/")


def test_setup_saves_keys_privately(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setattr(config, "USER_ENV", tmp_path / "home" / ".env")
    monkeypatch.setattr(onboard, "interactive", lambda: False)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".env").write_text("NEBIUS_API_KEY=old\nOTHER=1\n")
    res = onboard.setup("new-key", tavily="tv-1", check=False, out=lambda *_: None)
    text = (tmp_path / "home" / ".env").read_text()
    assert "NEBIUS_API_KEY=new-key" in text and "OTHER=1" in text and "TAVILY_API_KEY=tv-1" in text
    assert "old" not in text and res["saved"] == ["NEBIUS_API_KEY", "TAVILY_API_KEY"]


def test_latest_skips_unfinished_runs(offline, tmp_path):
    done = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=False).run()
    broken = pipeline.RUNS_DIR / "zzz-broken"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text(json.dumps({"id": "zzz", "status": "error", "started": 9e12}))
    state, _ = check_mod.load_run("latest")
    assert state["id"] == done["id"]


def test_python_api(offline, monorepo):
    import switchyard
    app = monorepo / "services" / "support"
    assert [c["function"] for c in switchyard.scan(str(app))] == ["triage", "extract_invoice", "summarize"]
    seen = []
    res = switchyard.migrate(str(app), tests=4, tavily=False, execute=False, on_event=seen.append)
    assert res.ok and res.summary["migrated"] == 3 and res.patch_path and seen
    assert {d["function"]: d["to"] for d in res.decisions}["triage"] == "lightning"
    assert res.apply()["subdir"] == "services/support"
