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
    assert state["status"] == "done", state.get("error")
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


def test_review_roundtrip(done_run, tmp_path):
    from switchyard import review
    out = tmp_path / "review.tsv"
    n = review.export(str(done_run.dir), str(out))
    assert n > 0
    import csv
    rows = list(csv.DictReader(open(out, encoding="utf-8"), delimiter="\t"))
    res_file = tmp_path / "results.tsv"
    with open(res_file, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["INPUT:item_id", "OUTPUT:verdict"])
        for r in rows:  # humans agree with the grader on every item
            w.writerow([r["INPUT:item_id"], r["GOLDEN:grader_pass"]])
    res = review.import_results(str(done_run.dir), str(res_file))
    assert res["items"] == n and res["agreement"] == 1.0


def test_publish_features_run_and_fills_readme(done_run, tmp_path, monkeypatch):
    import json

    from switchyard import publish as pub
    monkeypatch.setattr(pub, "RUNS_DIR", tmp_path / "runs")
    readme = tmp_path / "README.md"
    readme.write_text("# X\n\n## Results\n\n_Filled in from a recorded run._\n\n## Limits\n\n- a\n", encoding="utf-8")
    res = pub.publish(str(done_run.dir), readme=readme)
    saved = json.loads((res["dest"] / "run.json").read_text(encoding="utf-8"))
    assert saved["source"] == "sample_app"                    # no local paths leak into the public demo
    text = readme.read_text(encoding="utf-8")
    assert "of 3** call sites switched" in text and "## Limits" in text and "_Filled in" not in text
    # publishing again replaces the block instead of stacking a second copy
    pub.publish(str(done_run.dir), readme=readme)
    assert readme.read_text(encoding="utf-8").count(pub.START) == 1


def test_publish_refuses_unfinished_or_simulated(tmp_path):
    import json

    from switchyard import publish as pub
    for rid, status in (("mock-x", "done"), ("r1", "error")):
        d = tmp_path / rid
        d.mkdir()
        (d / "run.json").write_text(json.dumps({"id": rid, "status": status, "source": "s"}), encoding="utf-8")
        with pytest.raises(ValueError):
            pub.publish(str(d), write_readme=False)
