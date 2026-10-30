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


PARSE_APP = '''"""Calendar helper."""
from pydantic import BaseModel, Field
from openai import OpenAI
from models import Ticket

client = OpenAI()


class Event(BaseModel):
    name: str = Field(description="Name of the event")
    date: str = Field(description="ISO 8601 date and time")


def extract(text):
    r = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[{"role": "system", "content": "Extract the event."}, {"role": "user", "content": text}],
        response_format=Event,
    )
    return r.choices[0].message.parsed


def extract_again(text):
    r = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[{"role": "user", "content": text}],
        response_format=Event,
    )
    return r.choices[0].message.parsed


def triage(text):
    r = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[{"role": "user", "content": text}],
        response_format=Ticket,
    )
    return r.choices[0].message.parsed


def local(text):
    class Local(BaseModel):
        label: str

    r = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[{"role": "user", "content": text}],
        response_format=Local,
    )
    return r.choices[0].message.parsed
'''

MODELS = '''from pydantic import BaseModel


class Ticket(BaseModel):
    team: str
'''


def _parse_repo(tmp_path, pin=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(PARSE_APP, encoding="utf-8")
    (repo / "models.py").write_text(MODELS, encoding="utf-8")
    if pin:
        (repo / "requirements.txt").write_text(pin, encoding="utf-8")
    scan = scanner.scan_repo(repo)
    sites = _sites(scan, {"extract": ("lightning", "Use 24-hour times."), "extract_again": ("lightning", ""),
                          "triage": ("lightning", ""), "local": ("lightning", "")})
    for s in sites:      # the scanner resolves only same-file classes; give every .parse() call a schema
        s["site"].setdefault("params", {}).setdefault("output_schema", {"name": "X", "schema": {"type": "object"}})
    res = rewrite.apply(repo, tmp_path / "out", scan, sites)
    return res, (tmp_path / "out" / "app.py").read_text(encoding="utf-8")


def test_parse_schema_is_a_module_constant_merged_with_the_hint(tmp_path):
    from switchyard import evaluate
    res, new = _parse_repo(tmp_path)
    assert res["ok"], res["compile"]
    tree = ast.parse(new)
    assert "__import__" not in new and "getattr(" not in new
    assert new.count("import json\n") == 1 and new.index("import json") < new.index("class Event")
    # one constant per class, defined right after the class (or after the import that brings it in)
    assert new.count("_SCHEMA_EVENT = json.dumps(Event.model_json_schema())") == 1
    assert new.index("class Event") < new.index("_SCHEMA_EVENT =") < new.index("def extract")
    assert new.index("from models import Ticket") < new.index("_SCHEMA_TICKET = json.dumps(Ticket.model_json_schema())")
    # a class defined inside a function can't be read at import time: inlined at the call instead
    assert "json.dumps(Local.model_json_schema())" in new and "_SCHEMA_LOCAL" not in new
    # schema and Ultra hint go in ONE system message in front of the app's own
    calls = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    msgs = next(k.value for n in ast.walk(calls["extract"]) if isinstance(n, ast.Call)
                for k in n.keywords if k.arg == "messages")
    assert isinstance(msgs, ast.BinOp) and len(msgs.left.elts) == 1
    assert ast.unparse(msgs.left.elts[0]).count("'role': 'system'") == 1
    assert "_SCHEMA_EVENT" in ast.unparse(msgs.left) and "Use 24-hour times." in ast.unparse(msgs.left)
    # read back by the scanner (as `switchyard check` does), the hint survives and the schema is recognised
    site = next(c for c in scanner.scan_repo(tmp_path / "out")["calls"] if c["function"] == "extract")
    assert site["messages"][0]["content"].startswith(evaluate.SCHEMA_PREFIX)
    kept = evaluate._without_schema(site["messages"])
    assert kept[0] == {"role": "system", "content": "Use 24-hour times."} and kept[1]["content"] == "Extract the event."
    plain = next(c for c in scanner.scan_repo(tmp_path / "out")["calls"] if c["function"] == "extract_again")
    assert [m["role"] for m in evaluate._without_schema(plain["messages"])] == ["user"]


def test_parse_schema_on_pydantic_v1(tmp_path):
    res, new = _parse_repo(tmp_path, pin="pydantic==1.10.13\nopenai>=1.40\n")
    assert res["ok"], res["compile"]
    assert "_SCHEMA_EVENT = json.dumps(Event.schema())" in new and "model_json_schema" not in new
