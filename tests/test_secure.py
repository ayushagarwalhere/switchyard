"""`switchyard secure`: secrets, tracked .env, vulnerable deps (OSV stubbed), debug/CORS, Dockerfile, gate."""
import json
import subprocess

from switchyard import cli, secure


def _app(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\nrequests>=2\n")
    (tmp_path / "app.py").write_text('from flask import Flask\napp = Flask(__name__)\nAWS = "AKIAABCDEFGHIJKLMNOP"\n'
                                     'if __name__ == "__main__":\n    app.run(debug=True)\n')
    (tmp_path / "Dockerfile").write_text('FROM python:latest\nCOPY . .\nCMD ["python", "app.py"]\n')
    return tmp_path


def fake_osv(deps):
    return [[{"id": "GHSA-test-0001"}] if n == "flask" else [] for _, n, *_ in deps]


def test_finds_secret_debug_deps_and_docker(tmp_path):
    res = secure.run(_app(tmp_path), query=fake_osv)
    rules = sorted(f["rule"] for f in res["findings"])
    assert rules == ["DK1", "DK2", "DP1", "SE1", "SE3"]
    dp = next(f for f in res["findings"] if f["rule"] == "DP1")
    assert "flask 2.0.0" in dp["detail"] and "GHSA-test-0001" in dp["detail"]   # unpinned requests isn't queried
    assert {f["rule"] for f in res["blocking"]} == {"SE1", "SE3", "DP1"}


def test_placeholders_and_untracked_env_are_fine(tmp_path):
    (tmp_path / "config.py").write_text('KEY = "sk-your-key-here-xxxxxxxxxxxxxxxxxxxx"\n')
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123\n")
    res = secure.run(tmp_path, deps=False)
    assert res["findings"] == []          # a local .env outside git is where keys belong


def test_tracked_env_is_critical(tmp_path):
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=v1.abcdefghijklmnopqrstuvwxyz0123456789abcd\n")
    (tmp_path / ".env.example").write_text("NEBIUS_API_KEY=\n")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    res = secure.run(tmp_path, deps=False)
    rules = {(f["rule"], f["file"]) for f in res["findings"]}
    assert ("SE2", ".env") in rules and ("SE1", ".env") in rules
    assert not any(f["file"] == ".env.example" for f in res["findings"])


def test_offline_dependency_check_is_a_note_not_a_failure(tmp_path):
    def down(deps):
        raise OSError("no network")
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\n")
    res = secure.run(tmp_path, query=down)
    assert res["findings"] == [] and "skipped" in res["notes"][0]


def test_cli_exit_code_and_sarif(tmp_path, monkeypatch):
    _app(tmp_path)
    monkeypatch.setattr(secure, "osv_query", fake_osv)
    out = tmp_path / "secure.sarif"
    assert cli.main(["secure", str(tmp_path), "--format", "sarif", "--out", str(out), "--no-deps"]) == 1
    data = json.loads(out.read_text())
    assert data["runs"][0]["tool"]["driver"]["name"] == "Switchyard secure"
    assert {r["ruleId"] for r in data["runs"][0]["results"]} >= {"SE1", "SE3"}
    assert cli.main(["secure", str(tmp_path), "--no-deps", "--fail-on", "never"]) == 0
