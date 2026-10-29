"""`switchyard new`: fixed scaffold and code clean-up (no model calls)."""
from switchyard import create, secure


def test_strip_fences_and_filename_line():
    assert create._strip_fences("app/main.py\nimport os\n") == "import os\n"
    assert create._strip_fences("Here you go:\n```python\nx = 1\n```\nDone.") == "x = 1\n"


def test_scaffold_is_secure_and_deployable(tmp_path):
    plan = {"name": "notes", "title": "Notes", "endpoints": [], "ai_features": [{"name": "summarize", "tier": "lightning"}]}
    create.scaffold(tmp_path, plan)
    docker = (tmp_path / "Dockerfile").read_text()
    assert "USER appuser" in docker and "python:3.12-slim" in docker
    assert "LLM_MODEL_SUMMARIZE" in (tmp_path / ".env.example").read_text()
    assert all("==" in line for line in (tmp_path / "requirements.txt").read_text().split())
    assert secure.run(tmp_path, deps=False)["blocking"] == []
