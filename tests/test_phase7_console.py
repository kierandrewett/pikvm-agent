"""Phase 7 — the daemon serves a human console + its data endpoints; Phase 8 —
the memory-update export route returns a redacted Atlas proposal."""

from __future__ import annotations

import json

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
    # /status is the readiness probe UIs poll: it reports every dependency the daemon
    # needs to drive a session.
    app = create_app(app_config)
    with TestClient(app) as c:
        body = c.get("/status").json()
        assert "ok" in body
        deps = body["dependencies"]
        assert set(deps) >= {"pikvm", "omniparser", "operator", "ocr", "store"}
        assert set(deps["omniparser"]) >= {"enabled", "required", "reachable"}
        assert deps["omniparser"]["enabled"] is False  # default test config
        assert "reachable" in deps["pikvm"]
        assert "base_url" not in deps["pikvm"]
        assert body["machine"]["fingerprint"].startswith("target:")
        assert "pikvm.local" not in json.dumps(body["machine"])
        assert {"provider", "configured"} <= set(deps["operator"])
        assert deps["operator"]["routing"]["reasoner_lane"] == "hard"
        assert deps["operator"]["routing"]["controller_lane"] == "cheap"
        assert "provider" in deps["ocr"] and "available" in deps["ocr"]
        # FakeBackend is reachable + the fake operator is "configured" ⇒ ready.
        assert deps["pikvm"]["reachable"] is True
        assert body["ok"] is True


def test_console_data_endpoints(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        sid = c.post("/sessions", json={"task": "open the readme"}).json()["session_id"]
        # before any observe there is no frame yet
        assert c.get(f"/sessions/{sid}/frame").status_code == 404
        # a read-only poll must NOT capture (no frame appears)
        c.get(f"/sessions/{sid}")
        assert c.get(f"/sessions/{sid}/frame").status_code == 404
        # an explicit observe (capture=true) captures + saves a frame
        c.get(f"/sessions/{sid}?capture=true")
        frame = c.get(f"/sessions/{sid}/frame")
        assert frame.status_code == 200 and frame.headers["content-type"].startswith("image/")
        # session list + trace + (empty) approvals
        listing = c.get("/sessions").json()
        assert any(s["id"] == sid for s in listing)
        trace = c.get(f"/sessions/{sid}/trace").json()
        assert any(e["kind"] == "observe" for e in trace)
        assert c.get(f"/sessions/{sid}/approvals").json() == []


def test_preview_frame_is_live_but_does_not_mutate_session_freshness(
    app_config: AppConfig,
) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        sid = c.post("/sessions", json={"task": "observe the desktop"}).json()[
            "session_id"
        ]

        preview = c.get(f"/sessions/{sid}/preview-frame")

        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("image/")
        assert preview.headers["x-pikvm-frame-mode"] == "preview"
        assert c.get(f"/sessions/{sid}/frame").status_code == 404
        assert not any(
            event["kind"] == "observe"
            for event in c.get(f"/sessions/{sid}/trace").json()
        )


def test_memory_update_export_route(app_config: AppConfig) -> None:
    app = create_app(app_config)
    with TestClient(app) as c:
        sid = c.post("/sessions", json={"task": "open the readme"}).json()["session_id"]
        c.get(f"/sessions/{sid}")  # generate some trace
        mu = c.get(f"/sessions/{sid}/memory-update").json()
        assert "markdown" in mu and mu["page_path"].startswith("memory/")
        assert "open the readme" in mu["markdown"].lower()
