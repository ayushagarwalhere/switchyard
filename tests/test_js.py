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
    assert "apiKey: process.env.OPENAI_API_KEY" in lib          # the app's own client is never touched
    assert "const tokenFactory = new OpenAI({ baseURL:" in lib and lib.count("await tokenFactory.chat.completions") == 2
    assert lib.count(f'?? "{mid}"') == 2
    assert '[{ role: "system", content: "Only JSON." }, { role: "user"' in lib
    route = (tmp_path / "run" / "migrated" / "app" / "api" / "chat" / "route.ts").read_text(encoding="utf-8")
    assert 'import { createOpenAI } from "@ai-sdk/openai";' in route
    assert f'tokenFactoryAI.chat(process.env.LLM_MODEL_POST ?? "{mid}")' in route
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


def _one(tmp_path, name, code, hint=""):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(code, encoding="utf-8")
    scan = scanner.scan_repo(repo)
    sites = [{"site": c, "status": "done", "decision": {"action": "migrate", "model": "lightning", "choice": "lightning",
                                                         "pass_rate": 1.0, "hint": hint}} for c in scan["calls"]]
    res = rewrite.apply(repo, tmp_path / "out", scan, sites)
    out = tmp_path / "out" / name
    return res, out.read_text(encoding="utf-8") if out.exists() else ""


CJS = '''"use strict";
const OpenAI = require("openai");
const client = new OpenAI();
async function ask(q) {
  return client.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}
module.exports = { ask };
'''


def test_commonjs_gets_require_after_the_requires(tmp_path):
    res, out = _one(tmp_path, "bot.js", CJS)
    assert out.startswith('"use strict";\nconst OpenAI = require("openai");\n')
    assert out.index("const tokenFactory") > out.index('require("openai")') and "import " not in out
    assert res["ok"], res["compile"]


BOTH = '''import OpenAI, { toFile } from "openai";
import {
  createOpenAI,
} from "@ai-sdk/openai";
import { generateText } from "ai";
const client = new OpenAI();
export async function a(messages, model = "gpt-4o") {
  return client.chat.completions.create({ model, messages });
}
export async function b(prompt) {
  return generateText({ model: createOpenAI({})("gpt-4o"), prompt });
}
'''


def test_shorthand_props_and_both_sdks_in_one_file(tmp_path):
    res, out = _one(tmp_path, "x.ts", BOTH, hint="Be brief.")
    assert "model: process.env.LLM_MODEL_A" in out
    assert 'messages: [{ role: "system", content: "Be brief." }, ...(messages)]' in out
    assert out.count("import OpenAI") == 1 and out.count("createOpenAI,") == 1     # existing imports reused
    assert "const tokenFactory = new OpenAI(" in out and "const tokenFactoryAI = createOpenAI(" in out
    assert balanced(out) is None


CLIENT_COMPONENT = '''"use client";
import OpenAI from "openai";
const c = new OpenAI({ dangerouslyAllowBrowser: true });
export async function x(q) {
  return c.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}
'''

FACTORY = '''import { getClient } from "./c";
export async function y(q) {
  return getClient().chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: q }] });
}
'''


def test_unsafe_targets_go_to_the_manual_list(tmp_path):
    res, _ = _one(tmp_path / "a", "client.tsx", CLIENT_COMPONENT)
    assert "client component" in res["manual"][0]["reason"] and not res["changed"]
    res, _ = _one(tmp_path / "b", "srv.ts", FACTORY)
    assert "can't rename safely" in res["manual"][0]["reason"] and not res["changed"]


TWO_PROMPTS = '''import OpenAI from "openai";
const c = new OpenAI();
export async function one(q) {
  const prompt = "First: " + q;
  return c.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: prompt }] });
}
export async function two(q) {
  const prompt = `Second: ${q}`;
  return c.chat.completions.create({ model: "gpt-4o", messages: [{ role: "user", content: prompt }] });
}
export function View({ x }) {
  return (<p>Hello {x}</p>);
}
'''


def test_each_call_uses_its_own_prompt_const_and_jsx_is_not_a_regex(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "page.tsx").write_text(TWO_PROMPTS, encoding="utf-8")
    calls = {c["function"]: c for c in scanner.scan_repo(tmp_path / "r")["calls"]}
    assert calls["one"]["messages"][0]["content"] == "First: {{q}}"
    assert calls["two"]["messages"][0]["content"] == "Second: {{q}}"
    assert balanced(TWO_PROMPTS) is None


GATEWAY = '''import { generateText } from "ai";
import { gateway } from "@ai-sdk/gateway";
export async function s(t) {
  return generateText({ model: gateway("openai/gpt-4o"), prompt: t });
}
'''


def test_missing_ai_sdk_dependency_is_reported(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "package.json").write_text('{"dependencies": {"ai": "^5.0.0"}}', encoding="utf-8")
    res, out = _one(tmp_path, "r.ts", GATEWAY)
    assert "tokenFactoryAI.chat(" in out
    assert any("npm install @ai-sdk/openai" in m["reason"] for m in res["manual"])


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
