"""JavaScript / TypeScript: Next.js with the OpenAI SDK and the Vercel AI SDK."""
import shutil
from pathlib import Path

from switchyard import audit, cli, config, pipeline, rewrite, scanner
from switchyard.jsrewrite import balanced
from test_pipeline import FakeTF

NEXT = Path(__file__).parent / "fixtures" / "next_app"


def _calls(root):
    return {c["function"]: c for c in scanner.scan_repo(root)["calls"]}


def test_scans_openai_and_ai_sdk_calls():
    calls = _calls(NEXT)
    assert set(calls) == {"triageTicket", "extractInvoice", "POST", "reply"}
    t = calls["triageTicket"]
    assert t["sdk"] == "js-openai" and t["model"] == "gpt-4o-mini" and t["params"]["temperature"] == 0
    assert t["messages"][0]["content"].startswith("You are a support triage assistant")   # resolved from a const
    assert t["messages"][1]["content"] == "Ticket:\n{{ticket}}" and t["variables"] == ["ticket"]
    assert calls["extractInvoice"]["params"]["response_format"] == {"type": "json_object"}
    post = calls["POST"]
    assert post["sdk"] == "js-ai-sdk" and post["model"] == "gpt-4o" and post["params"]["max_tokens"] == 300
    assert [m["role"] for m in post["messages"]] == ["system", "user"]
    assert calls["reply"]["params"]["stream"] is True


def _migrate(tmp_path, root, hint_for=()):
    scan = scanner.scan_repo(root)
    sites = [{"site": c, "status": "done", "decision": {"action": "migrate", "model": "super", "choice": "super",
                                                         "pass_rate": 1.0, "hint": "Only JSON." if c["function"] in hint_for else ""}}
             for c in scan["calls"]]
    return rewrite.apply(root, tmp_path / "run" / "migrated", scan, sites)


def test_rewrite_points_everything_at_token_factory(tmp_path):
    res = _migrate(tmp_path, NEXT, hint_for={"extractInvoice", "POST"})
    assert res["ok"] and set(res["changed"]) == {"lib/ai.ts", "app/api/chat/route.ts"}
    mid = config.MODELS["super"].id
    lib = (tmp_path / "run" / "migrated" / "lib" / "ai.ts").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in lib and "process.env.NEBIUS_API_KEY" in lib     # the shared client moved
    assert lib.count(f'?? "{mid}"') == 2
    assert '[{ role: "system", content: "Only JSON." }, { role: "user"' in lib
    route = (tmp_path / "run" / "migrated" / "app" / "api" / "chat" / "route.ts").read_text(encoding="utf-8")
    assert 'import { createOpenAI } from "@ai-sdk/openai";' in route
    assert f'tokenFactory.chat(process.env.LLM_MODEL_POST ?? "{mid}")' in route
    assert 'system: "Only JSON." + "\\n\\n" + ("Summarize' in route
    assert balanced(route) is None and balanced(lib) is None
    assert "LLM_MODEL_TRIAGETICKET=" + mid in (tmp_path / "run" / "migration.env").read_text()


def test_partial_migration_keeps_the_original_client_and_line_endings(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(NEXT, repo)
    lib = repo / "lib" / "ai.ts"
    lib.write_bytes(b"\xef\xbb\xbf" + lib.read_text(encoding="utf-8").replace("\n", "\r\n").encode())   # BOM + CRLF
    scan = scanner.scan_repo(repo)
    sites = [{"site": c, "status": "done",
              "decision": {"action": "migrate" if c["function"] == "triageTicket" else "keep", "model": "lightning",
                           "choice": "lightning", "pass_rate": 1.0, "hint": ""}} for c in scan["calls"]]
    res = rewrite.apply(repo, tmp_path / "out", scan, sites)
    raw = (tmp_path / "out" / "lib" / "ai.ts").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw
    text = raw.decode("utf-8-sig")
    assert "apiKey: process.env.OPENAI_API_KEY" in text                      # gpt-4o call still uses it
    assert "await tokenFactory.chat.completions.create" in text and "const tokenFactory = new OpenAI(" in text
    assert len(res["diff"].splitlines()) < 30                                  # no whole-file churn


def test_client_built_from_a_config_object_keeps_its_options(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bot.js").write_text('const OpenAI = require("openai");\nconst cfg = { timeout: 20000 };\n'
                                 'const client = new OpenAI(cfg);\n'
                                 'async function ask(q) {\n  return client.chat.completions.create({ model: "gpt-4o", '
                                 'messages: [{ role: "user", content: q }] });\n}\nmodule.exports = { ask };\n', encoding="utf-8")
    res = _migrate(tmp_path, repo)
    out = (tmp_path / "run" / "migrated" / "bot.js").read_text(encoding="utf-8")
    assert "new OpenAI({ ...(cfg), baseURL:" in out and res["ok"]


def test_pipeline_runs_end_to_end_on_a_next_app(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    state = pipeline.Run(str(NEXT), tests=4, use_tavily=False, execute=True).run()
    assert state["status"] == "done", state.get("error")
    assert len(state["sites"]) == 4 and state["patch"]["ok"]
    rt = state["patch"].get("runtime") or []
    assert rt and all(r["status"] == "skipped" and "JavaScript" in r["reason"] for r in rt)


def test_audit_and_dry_run_see_typescript(tmp_path, capsys):
    shutil.copytree(NEXT, tmp_path / "app")
    (tmp_path / "app" / "lib" / "keys.ts").write_text(
        'export const KEY = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789";\n', encoding="utf-8")
    f = audit.static(tmp_path / "app")
    assert any(x["rule"] == "AU1" and x["file"] == "lib/keys.ts" for x in f)
    assert any(x["rule"] == "AU4" and x["function"] == "triageTicket" for x in f)
    assert cli.main(["run", str(tmp_path / "app"), "--dry-run"]) == 0
    assert "triageTicket" in capsys.readouterr().out
