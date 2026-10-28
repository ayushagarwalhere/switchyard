"""check (CI guard) and apply (git branch) against an offline run."""
import shutil
import subprocess

import pytest

from switchyard import check as check_mod
from switchyard import gitops, pipeline
from test_pipeline import FIX, FakeTF


@pytest.fixture
def done_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(check_mod, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    r = pipeline.Run(str(FIX), tests=4, use_tavily=False)
    state = r.run()
    assert state["status"] == "done"
    return r


def test_pr_body_written(done_run):
    body = (done_run.dir / "PR.md").read_text(encoding="utf-8")
    assert "Nemotron" in body and "switchyard check" in body and "triage" in body


def test_check_passes_and_detects_prompt_change(done_run, tmp_path):
    res = check_mod.check(str(done_run.dir), emit=lambda *_: None)
    assert res["ok"] and len(res["results"]) == 3
    # edit a prompt in a checkout: check re-scans and flags it
    repo = tmp_path / "edited"
    shutil.copytree(FIX, repo)
    app = repo / "app.py"
    app.write_text(app.read_text().replace("Summarize in ", "Briefly summarize in "))
    res = check_mod.check(str(done_run.dir), repo=str(repo), emit=lambda *_: None)
    changed = [r for r in res["results"] if r.get("prompt_changed")]
    assert [r["function"] for r in changed] == ["summarize"]


def test_apply_creates_branch(done_run, tmp_path):
    repo = tmp_path / "checkout"
    shutil.copytree(FIX, repo)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    res = gitops.apply(str(done_run.dir), str(repo))
    assert res["branch"].startswith("switchyard/")
    assert "app.py" in " ".join(res["changed"])
    assert "Nemotron" in (repo / "app.py").read_text()
    assert (repo / ".env.switchyard.example").exists()
