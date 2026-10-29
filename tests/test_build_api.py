"""Web app build & ship routes: local only, secure works, deployments list, public demo refuses."""
from fastapi.testclient import TestClient

from switchyard import create, deploy, server


def test_routes_local(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "STATE", tmp_path / "state.json")
    create.scaffold(tmp_path / "app1", {"name": "a", "title": "A", "endpoints": [], "ai_features": []})
    (tmp_path / "app1" / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    c = TestClient(server.app)
    assert c.get("/api/config").json()["build"] is True
    r = c.post("/api/secure", json={"path": str(tmp_path / "app1")})
    assert r.status_code == 200 and r.json()["blocking"] == 0
    assert c.get("/api/deployments").json() == []
    assert c.post("/api/deploy", json={"path": str(tmp_path / "app1")}).json()["error"].startswith("tell the user")
    assert c.post("/api/build", json={"idea": "x"}).status_code == 400   # no key in tests (and too short)


def test_public_demo_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PUBLIC", True)
    c = TestClient(server.app)
    assert c.get("/api/config").json()["build"] is False
    for path, body in (("/api/build", {"idea": "a todo app with reminders"}), ("/api/secure", {"path": str(tmp_path)}),
                       ("/api/deploy", {"path": str(tmp_path), "agreed": True})):
        assert c.post(path, json=body).status_code == 403
