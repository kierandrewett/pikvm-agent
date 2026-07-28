from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pikvm_agent.config import AppConfig
from pikvm_agent.daemon import create_app
from pikvm_agent.daemon_access import DaemonAccess, DaemonAccessError


ACTION_TOKEN = "action-capability-token-0123456789abcdef"
HARNESS_TOKEN = "harness-capability-token-0123456789abcdef"


def test_daemon_requires_a_valid_capability_for_session_access(
    app_config: AppConfig,
) -> None:
    app = create_app(
        app_config,
        access=DaemonAccess(
            action_token=ACTION_TOKEN,
            harness_token=HARNESS_TOKEN,
        ),
    )

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/status").status_code == 200
        assert client.get("/sessions").status_code == 401
        assert (
            client.post("/sessions", json={"task": "t"}).status_code == 401
        )

        started = client.post(
            "/sessions",
            json={"task": "t"},
            headers={"Authorization": f"Bearer {ACTION_TOKEN}"},
        )
        assert started.status_code == 200
        session_id = started.json()["session_id"]
        assert (
            client.get(
                f"/sessions/{session_id}",
                headers={"Authorization": f"Bearer {ACTION_TOKEN}"},
            ).status_code
            == 200
        )


def test_only_harness_capability_can_submit_an_approval(
    app_config: AppConfig,
) -> None:
    app = create_app(
        app_config,
        access=DaemonAccess(
            action_token=ACTION_TOKEN,
            harness_token=HARNESS_TOKEN,
        ),
    )

    with TestClient(app) as client:
        path = "/sessions/missing/approvals/a_missing"
        action_only = client.post(
            path,
            json={"type": "approve"},
            headers={"Authorization": f"Bearer {ACTION_TOKEN}"},
        )
        harness_owned = client.post(
            path,
            json={"type": "approve"},
            headers={"Authorization": f"Bearer {HARNESS_TOKEN}"},
        )

    assert action_only.status_code == 401
    assert harness_owned.status_code == 404


def test_daemon_access_refuses_missing_short_or_shared_capabilities() -> None:
    with pytest.raises(DaemonAccessError, match="required"):
        DaemonAccess.from_environment({})
    with pytest.raises(DaemonAccessError, match="at least 32"):
        DaemonAccess.from_environment(
            {
                "PIKVM_AGENT_DAEMON_TOKEN": "short",
                "PIKVM_AGENT_HARNESS_TOKEN": HARNESS_TOKEN,
            }
        )
    with pytest.raises(DaemonAccessError, match="must differ"):
        DaemonAccess(
            action_token=ACTION_TOKEN,
            harness_token=ACTION_TOKEN,
        )
