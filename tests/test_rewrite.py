import ast
from pathlib import Path

from switchyard import rewrite, scanner

FIX = Path(__file__).parent / "fixtures" / "sample_app"


def _sites(scan, decisions):
    out = []
    for c in scan["calls"]:
        d = decisions.get(c["function"])
        if d is None:
            out.append({"site": c, "status": "done", "decision": {"action": "keep"}})
        else:
            model, hint = d
            out.append({"site": c, "status": "done",
                        "decision": {"action": "migrate", "model": model, "choice": model, "hint": hint, "pass_rate": 1.0}})
    return out


def test_all_migrate_rewrites_client(tmp_path):
    scan = scanner.scan_repo(FIX)
    sites = _sites(scan, {"triage": ("lightning", ""), "extract_invoice": ("super", "Return only JSON."), "summarize": ("lightning", "")})
    res = rewrite.apply(FIX, tmp_path / "out", scan, sites)
    assert res["ok"] and res["mode"] == "rewrite-clients"
    new = (tmp_path / "out" / "app.py").read_text()
    ast.parse(new)
    assert 'OpenAI(base_url=os.environ.get("TOKEN_FACTORY_BASE_URL"' in new
    assert 'os.environ.get("LLM_MODEL_TRIAGE", "nvidia/Nemotron-3_5-Lightning")' in new
    assert '"Return only JSON."' in new
    assert new.startswith("import json\nimport os\n") or "import os\n" in new.splitlines(True)[:3][-1] or "import os" in new


def test_partial_migration_uses_dedicated_client(tmp_path):
    scan = scanner.scan_repo(FIX)
    sites = _sites(scan, {"triage": ("lightning", "")})
    res = rewrite.apply(FIX, tmp_path / "out", scan, sites)
    assert res["ok"] and res["mode"] == "dedicated-client"
    new = (tmp_path / "out" / "app.py").read_text()
    ast.parse(new)
    assert "client = OpenAI()" in new                      # original client untouched for kept calls
    assert "_token_factory.chat.completions.create(" in new
    assert new.count("_token_factory.chat") == 1
    assert 'model="gpt-4o"' in new                         # kept call unchanged
