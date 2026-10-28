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


ANTH = Path(__file__).parent / "fixtures" / "anthropic_app"


def test_anthropic_call_goes_through_shim(tmp_path):
    scan = scanner.scan_repo(ANTH)
    assert scan["calls"][0]["sdk"] == "anthropic"
    sites = _sites(scan, {"classify": ("lightning", "")})
    res = rewrite.apply(ANTH, tmp_path / "out", scan, sites)
    assert res["ok"] and not res["manual"]
    new = (tmp_path / "out" / "bot.py").read_text()
    assert "_token_factory_anthropic.messages.create(" in new
    assert "client = anthropic.Anthropic()" in new

    # the shim really behaves like Anthropic's Message object
    ns: dict = {}
    exec(compile(rewrite.ANTHROPIC_SHIM, "shim", "exec"), ns)

    class FakeOpenAI:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    from types import SimpleNamespace as S
                    assert kw["messages"][0] == {"role": "system", "content": "sys"}
                    return S(choices=[S(message=S(content="positive"), finish_reason="stop")],
                             usage=S(prompt_tokens=5, completion_tokens=1))

    msg = ns["_NemotronMessages"](FakeOpenAI()).messages.create(model="m", system="sys", max_tokens=5,
                                                                 messages=[{"role": "user", "content": "great"}])
    assert msg.content[0].text == "positive" and msg.stop_reason == "end_turn"
