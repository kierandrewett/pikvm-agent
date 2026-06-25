"""Phase 7 — the daemon serves a human console + its data endpoints; Phase 8 —
the memory-update export route returns a redacted Atlas proposal."""

from __future__ import annotations

from fastapi.testclient import TestClient

from pikvm_agent.config import AppConfig
from pikvm_agent.daemon import create_app


def test_console_page_and_assets(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        page = c.get("/")
        assert page.status_code == 200 and "PiKVM Agent" in page.text
        js = c.get("/ui/app.js")
        assert js.status_code == 200 and "loadSessions" in js.text


def test_status_endpoint(app_config: AppConfig) -> None:
    # /status is the readiness probe UIs poll: daemon up (implicit) + OmniParser state.
    app = create_app(app_config)
    with TestClient(app) as c:
        body = c.get("/status").json()
        assert body["ok"] is True
        omni = body["omniparser"]
        assert set(omni) >= {"enabled", "required", "reachable"}
        # The default test config doesn't enable OmniParser ⇒ not reachable.
        assert omni["reachable"] is False


def test_console_data_endpoints(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        sid = c.post("/sessions", json={"task": "open the readme"}).json()["session_id"]
        # before any observe there is no frame yet
        assert c.get(f"/sessions/{sid}/frame").status_code == 404
        # observe captures + saves a frame
        c.get(f"/sessions/{sid}")
        frame = c.get(f"/sessions/{sid}/frame")
        assert frame.status_code == 200 and frame.headers["content-type"].startswith("image/")
        # session list + trace + (empty) approvals
        listing = c.get("/sessions").json()
        assert any(s["id"] == sid for s in listing)
        trace = c.get(f"/sessions/{sid}/trace").json()
        assert any(e["kind"] == "observe" for e in trace)
        assert c.get(f"/sessions/{sid}/approvals").json() == []


def test_memory_update_export_route(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        sid = c.post("/sessions", json={"task": "open the readme"}).json()["session_id"]
        c.get(f"/sessions/{sid}")  # generate some trace
        mu = c.get(f"/sessions/{sid}/memory-update").json()
        assert "markdown" in mu and mu["page_path"].startswith("memory/")
        assert "open the readme" in mu["markdown"].lower()
