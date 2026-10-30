"""`switchyard.prreview`: a PR comment for AI code, built from a real two-commit git repo (OSV stubbed)."""
import json
import subprocess

import pytest

from switchyard import config, prreview

KEY = "sk-proj-" + "abcdefghijklmnopqrstuvwxyz0123456789"

BASE_APP = '''import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30)


def summarize(text):
    """Write a detailed report of the meeting."""
    return client.chat.completions.create(
        model="gpt-4o",
        max_tokens=800,
        messages=[{"role": "user", "content": f"Write a detailed report of this meeting: {text}"}],
    ).choices[0].message.content


def old_helper(q):
    return client.chat.completions.create(model="gpt-4o-mini", max_tokens=50,
                                          messages=[{"role": "user", "content": q}])
'''

HEAD_APP = '''import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=30)
backup = OpenAI(api_key="''' + KEY + '''", timeout=30)


def summarize(text):
    """Write a detailed report of the meeting."""
    return client.chat.completions.create(
        model="gpt-4o",
        max_tokens=800,
        messages=[{"role": "user", "content": f"Write a detailed report of this meeting: {text}"}],
    ).choices[0].message.content


def classify_ticket(ticket):
    resp = backup.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a support ticket classifier. Classify the ticket into one of: "
                                          "billing, bug, feature, other. Respond with only the label."},
            {"role": "user", "content": ticket},
        ],
    )
    return resp.choices[0].message.content
'''


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "app"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text(BASE_APP, encoding="utf-8")
    (r / "requirements.txt").write_text("openai==1.40.0\nflask==3.0.0\n", encoding="utf-8")
    (r / "Dockerfile").write_text("FROM python:latest\nCMD python app.py\n", encoding="utf-8")   # untouched by the PR
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "checkout", "-q", "-b", "feature")
    (r / "app.py").write_text(HEAD_APP, encoding="utf-8")
    (r / "requirements.txt").write_text("openai==1.40.0\nflask==2.0.0\nrequests==2.19.0\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "head")
    return r


def stub_osv(calls):
    def query(deps):
        calls.append([d[1] for d in deps])
        return [[{"id": f"GHSA-{d[1]}-0001"}] if d[1] in ("flask", "openai") else [] for d in deps]
    return query


def _review(repo, tmp_path, **kw):
    seen = []
    res = prreview.review(repo, base="main", osv_query=stub_osv(seen), runs_dir=tmp_path / "no-runs", **kw)
    return res, seen


def test_parse_diff_maps_hunks_to_new_and_old_lines():
    diff = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n"
            "@@ -2,0 +3,2 @@\n+a\n+--- not a header\n@@ -7 +9 @@\n-old\n+new\n"
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
    files = prreview.parse_diff(diff)
    assert files[0]["new"] == "x.py" and files[0]["added"] == {3, 4, 9} and files[0]["removed"] == {7}
    assert files[1]["status"] == "deleted" and files[1]["new"] is None and files[1]["removed"] == {1, 2}


def test_diff_mapping_added_and_removed_calls(repo, tmp_path):
    res, _ = _review(repo, tmp_path)
    assert [(c["status"], c["function"], c["line"]) for c in res["calls"]] == [("added", "classify_ticket", 18)]
    assert res["removed"] == [{"file": "app.py", "line": 17, "function": "old_helper", "sdk": "openai", "model": "gpt-4o-mini"}]
    # summarize() only moved down a line: not reported
    assert not any(c["function"] == "summarize" for c in res["calls"])
    c = res["calls"][0]
    assert c["sdk"] == "openai" and c["model"] == "gpt-4o" and c["what"] == "acts as a support ticket classifier"


def test_changed_call_reports_what_changed(repo, tmp_path):
    (repo / "app.py").write_text(HEAD_APP.replace('model="gpt-4o",\n        max_tokens=800', 'model="gpt-4.1",\n        max_tokens=800'),
                                 encoding="utf-8")
    _git(repo, "commit", "-qam", "switch model")
    res, _ = _review(repo, tmp_path)
    s = next(c for c in res["calls"] if c["function"] == "summarize")
    assert s["status"] == "changed" and s["changes"] == ["model gpt-4o → gpt-4.1"]
    assert s["suggestion"]["model"] == "super" and s["suggestion"]["task"] == "long-form writing"


def test_cost_lines_use_the_price_tables(repo, tmp_path):
    res, _ = _review(repo, tmp_path, volume=100_000)
    c = res["calls"][0]
    tin, tout = prreview.tokens(c)
    assert tout == prreview.DEFAULT_OUT and tin == sum(len(m["content"]) for m in c["messages"]) // 4 + prreview.INPUT_PAD
    pin, pout, _ = config.source_price("gpt-4o")
    per_call = tin / 1e6 * pin + tout / 1e6 * pout
    assert c["cost"]["current"]["per_1k"] == pytest.approx(per_call * 1000)
    assert c["cost"]["current"]["monthly"] == pytest.approx(per_call * 100_000)
    assert c["cost"]["lightning"]["per_1k"] == pytest.approx(config.MODELS["lightning"].cost(tin, tout) * 1000)
    assert c["target"] == "lightning" and c["projected_monthly"] == pytest.approx(c["cost"]["lightning"]["monthly"])
    md = prreview.markdown(res)
    now, new = prreview._usd(per_call * 1000), prreview._usd(c["cost"]["lightning"]["per_1k"])
    assert f"~{now} per 1k calls on gpt-4o → ~{new} on Nemotron 3.5 Lightning" in md
    assert "at 100k calls each could drop to" in md and "estimate" in md.lower()


def test_suggestion_is_worded_as_something_to_test(repo, tmp_path):
    res, _ = _review(repo, tmp_path)
    s = res["calls"][0]["suggestion"]
    assert s["model"] == "lightning" and s["task"] == "classification"
    md = prreview.markdown(res)
    assert "**Nemotron 3.5 Lightning** for classification: worth testing with `switchyard run`" in md
    assert "passed" not in md.split("### Security")[0]          # no claim that it passes without a test


def test_findings_restricted_to_changed_lines(repo, tmp_path):
    res, seen = _review(repo, tmp_path)
    got = {(f["rule"], f["file"], f["line"]) for f in res["findings"]}
    assert ("SE1", "app.py", 5) in got                                # the hard-coded key, on an added line
    assert ("AU4", "app.py", 18) in got                               # no max_tokens on the new call
    assert ("DP1", "requirements.txt", 2) in got                      # flask downgraded to a vulnerable pin
    assert not any(f["file"] == "Dockerfile" for f in res["findings"])   # not touched by the PR
    assert not any(f["line"] == 10 for f in res["findings"])         # summarize() is unchanged
    # OSV is asked only about packages whose lines changed (openai==1.40.0 didn't)
    assert seen == [["flask", "requests"]]
    key = next(f for f in res["findings"] if f["rule"] in ("AU1", "SE1"))
    assert key["severity"] == "CRITICAL" and key["fix"]
    assert sum(1 for f in res["findings"] if f["line"] == 5 and f["file"] == "app.py") == 1   # one finding per key


def test_marker_verdict_and_fail_level(repo, tmp_path):
    res, _ = _review(repo, tmp_path)
    md = prreview.markdown(res)
    assert md.startswith(prreview.MARKER) and md.count(prreview.MARKER) == 1
    first = next(line for line in md.splitlines() if line.startswith("**"))
    assert first.startswith("**1 AI call added, 1 removed · ~$") and "issues (" in first
    assert "<details>" in md and md.count("<details>") == md.count("</details>")
    assert prreview.fail_level(res) == "CRITICAL"
    assert prreview.should_fail(res, "high") and not prreview.should_fail(res, "never")


def test_no_ai_changes_is_clean(repo, tmp_path):
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    res = prreview.review(repo, base="feature~0", head="HEAD", osv_query=False, runs_dir=tmp_path / "x")
    assert res["calls"] == [] and res["findings"] == [] and prreview.fail_level(res) is None
    assert "No AI calls changed" in prreview.markdown(res)


def test_cites_a_previous_migration_run(repo, tmp_path):
    runs = tmp_path / "runs"
    (runs / "r1").mkdir(parents=True)
    site = {"file": "app.py", "function": "classify_ticket", "line": 18, "messages": []}
    state = {"id": "r1", "status": "done", "started": 1, "finished": 2,
             "checkout": {"root": str(repo.resolve()), "subdir": ""},
             "sites": [{"site": site, "cases": [{}] * 8, "savings": {"saving_pct": 97.6},
                        "decision": {"action": "migrate", "model": "lightning", "pass_rate": 1.0, "hint": ""}}]}
    (runs / "r1" / "run.json").write_text(json.dumps(state), encoding="utf-8")
    res = prreview.review(repo, base="main", osv_query=False, runs_dir=runs)
    t = res["calls"][0]["tested"]
    assert t["action"] == "migrate" and t["model"] == "lightning" and t["tests"] == 8 and t["stale"]
    md = prreview.markdown(res)
    assert "**Tested:** Nemotron 3.5 Lightning passed 100% of 8 tests (run r1)" in md
    assert "re-run `switchyard run` to confirm" in md


def test_module_entry_point_writes_comment_and_fails_on_high(repo, tmp_path, monkeypatch):
    out = tmp_path / "review.md"
    monkeypatch.setattr(prreview.config, "runs_dir", lambda: tmp_path / "none")
    code = prreview.main([str(repo), "--base", "main", "--out", str(out), "--no-deps", "--fail-on", "high"])
    assert code == 1 and out.read_text(encoding="utf-8").startswith(prreview.MARKER)
    assert prreview.main([str(repo), "--base", "main", "--out", str(out), "--no-deps", "--fail-on", "never"]) == 0


def test_action_review_mode_and_example_stay_in_sync():
    yaml = pytest.importorskip("yaml")         # installed with uvicorn[standard]
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    action = yaml.safe_load((root / "action.yml").read_text(encoding="utf-8"))
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "review")
    assert step["if"] == "inputs.command == 'review'" and "NEBIUS_API_KEY" not in step["env"]   # static, no key
    assert "github.event.pull_request.base.sha" in step["env"]["BASE"]
    assert "python -m switchyard.prreview" in step["run"] and prreview.MARKER in step["run"]
    example = yaml.safe_load((root / "examples" / "switchyard-review.yml").read_text(encoding="utf-8"))
    assert example["permissions"]["pull-requests"] == "write"
    steps = example["jobs"]["review"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert steps[1]["uses"] == "ayushagarwalhere/switchyard@v0.4.1" and steps[1]["with"]["command"] == "review"
