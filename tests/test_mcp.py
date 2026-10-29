"""The MCP tools: free scan/audit, spending guarded by an explicit limit, and the server wiring."""
import inspect
import shutil

import pytest

from switchyard import mcp_server, pipeline
from test_pipeline import FIX, FakeTF


def test_scan_and_audit_are_free_and_need_absolute_paths():
    with pytest.raises(ValueError, match="absolute"):
        mcp_server.scan_llm_calls("tests/fixtures/sample_app")
    res = mcp_server.scan_llm_calls(str(FIX.resolve()))
    assert [c["function"] for c in res["calls"]] == ["triage", "extract_invoice", "summarize"]
    assert res["estimated_cost_usd"] > 0
    au = mcp_server.audit_llm_calls(str(FIX.resolve()))
    assert au["counts"]["MEDIUM"] >= 1 and au["note"] is None


def test_migration_needs_a_key_and_a_spend_limit(monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.delenv("TOKEN_FACTORY_API_KEY", raising=False)
    assert "switchyard setup" in mcp_server.start_migration(str(FIX.resolve()), 1.0)["ask_user"]
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    assert "max_spend_usd is required" in mcp_server.start_migration(str(FIX.resolve()), 0)["error"]


def test_background_migration_then_results(monkeypatch, tmp_path):
    monkeypatch.setenv("NEBIUS_API_KEY", "k")
    monkeypatch.setattr(pipeline, "TokenFactory", FakeTF)
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    app = tmp_path / "app"
    shutil.copytree(FIX, app)
    started = mcp_server.start_migration(str(app), 1.0, tests_per_call=4)
    rid = started["run_id"]
    t = next(th for th in __import__("threading").enumerate() if th.name == f"switchyard-{rid}")
    t.join(timeout=120)
    st = mcp_server.migration_status(rid)
    assert st["done"] and st["status"] == "done" and st["calls_decided"] == 3
    res = mcp_server.migration_results(rid)
    assert res["summary"]["migrated"] == 3 and res["patch"]
    assert {d["function"] for d in res["decisions"]} == {"triage", "extract_invoice", "summarize"}


def test_server_registers_every_tool():
    pytest.importorskip("mcp")
    import anyio
    server = mcp_server.build()
    tools = server.list_tools()
    if inspect.isawaitable(tools):
        async def get():
            return await tools
        tools = anyio.run(get)
    names = {t.name for t in tools}
    assert names == {fn.__name__ for fn in mcp_server.TOOLS}
    scan = next(t for t in tools if t.name == "start_migration")
    schema = getattr(scan, "input_schema", None) or scan.inputSchema     # mcp 2.x / 1.x
    assert "max_spend_usd" in schema["properties"] and "max_spend_usd" in schema["required"]
