from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from pikvm_agent.harness.protocol import OracleSnapshot
from pikvm_agent.harness.receiver import ObserverReceiver


def _snapshot(sequence: int = 1) -> dict[str, object]:
    return {
        "protocol": "pikvm-observer.v1",
        "sequence": sequence,
        "text": "exact text",
        "events": [{"at_ms": 12, "kind": "key_down", "vk": 65, "scan": 30}],
        "dangerous_commits": [],
        "foreground_title": "actual.txt - Visual Studio Code",
        "foreground_executable": "Code.exe",
        "foreground_process_id": 4242,
        "focused_control_class": "Chrome_RenderWidgetHostHWND",
        "focused_control_id": 0,
        "focus_in_foreground": True,
        "guest_fingerprint": "guest:0123456789abcdef",
        "guest_session_id": 2,
        "input_desktop": "Default",
        "file": {
            "path": "C:/PiKVM-Harness/workspace/actual.txt",
            "content_base64": base64.b64encode(b"file bytes").decode("ascii"),
            "error": "",
        },
    }


def test_receiver_serves_artifact_and_accepts_authenticated_snapshot(tmp_path) -> None:
    artifact = tmp_path / "observer.exe"
    artifact.write_bytes(b"MZ-observer")
    receiver = ObserverReceiver(artifact=artifact, token="test-token")
    client = TestClient(receiver.public_app)

    denied = client.get("/observer.exe")
    assert denied.status_code == 401

    downloaded = client.get("/observer.exe?token=test-token")
    assert downloaded.status_code == 200
    assert downloaded.content == b"MZ-observer"

    posted = client.post(
        "/ingest",
        headers={"X-Observer-Token": "test-token"},
        json=_snapshot(),
    )
    assert posted.status_code == 202
    assert posted.json() == {"accepted": True, "sequence": 1}

    latest = receiver.latest()
    assert latest is not None
    assert latest.text == "exact text"
    assert latest.file is not None
    assert latest.file.content() == b"file bytes"
    assert latest.safe_environment() == {
        "foreground_title": "actual.txt - Visual Studio Code",
        "foreground_executable": "Code.exe",
        "foreground_process_id": 4242,
        "focused_control_class": "Chrome_RenderWidgetHostHWND",
        "focused_control_id": 0,
        "focus_in_foreground": True,
        "guest_fingerprint": "guest:0123456789abcdef",
        "guest_session_id": 2,
        "input_desktop": "Default",
        "observer_process_id": None,
    }
    assert "guest_computer_name" not in type(latest).model_fields
    assert client.get("/latest").status_code == 404


def test_compact_visual_wire_keys_expand_to_the_same_snapshot() -> None:
    snapshot = OracleSnapshot.model_validate(
        {
            "p": "pikvm-observer.v1",
            "s": 9,
            "t": "",
            "e": [],
            "ic": 6,
            "kv": [162, 160, 121],
            "kc": 3,
            "kt": False,
            "dc": [],
            "ft": "PiKVM Accuracy Observer",
            "fe": "observer.exe",
            "fp": 7676,
            "fc": "Edit",
            "fi": 1001,
            "ff": True,
            "gf": "guest:0123456789abcdef",
            "gs": 1,
            "id": "Default",
            "op": "C:/PiKVM-Harness/workspace/report.docx",
            "oi": 7171,
        }
    )

    assert snapshot.sequence == 9
    assert snapshot.key_down_vks == [162, 160, 121]
    assert snapshot.safe_environment()["guest_fingerprint"] == (
        "guest:0123456789abcdef"
    )
    assert snapshot.focused_control_class == "Edit"
    assert snapshot.observed_path.endswith("report.docx")
    assert snapshot.observer_process_id == 7171


def test_receiver_rejects_bad_tokens_and_sequence_regressions(tmp_path) -> None:
    artifact = tmp_path / "observer.exe"
    artifact.write_bytes(b"MZ")
    receiver = ObserverReceiver(artifact=artifact, token="right-token")
    client = TestClient(receiver.public_app)

    assert (
        client.post(
            "/ingest",
            headers={"X-Observer-Token": "wrong-token"},
            json=_snapshot(),
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/ingest",
            headers={"X-Observer-Token": "right-token"},
            json=_snapshot(sequence=4),
        ).status_code
        == 202
    )
    stale = client.post(
        "/ingest",
        headers={"X-Observer-Token": "right-token"},
        json=_snapshot(sequence=3),
    )
    assert stale.status_code == 409
    assert receiver.latest() is not None
    assert receiver.latest().sequence == 4


@pytest.mark.parametrize(
    "untrusted",
    (
        {"guest_fingerprint": "raw-windows-hostname"},
        {"guest_computer_name": "private-hostname"},
    ),
)
def test_receiver_rejects_untrusted_guest_identity_shape(
    tmp_path, untrusted: dict[str, str]
) -> None:
    artifact = tmp_path / "observer.exe"
    artifact.write_bytes(b"MZ")
    receiver = ObserverReceiver(artifact=artifact, token="right-token")
    client = TestClient(receiver.public_app)
    payload = _snapshot()
    payload.update(untrusted)

    response = client.post(
        "/ingest",
        headers={"X-Observer-Token": "right-token"},
        json=payload,
    )

    assert response.status_code == 422
    assert receiver.latest() is None


def test_receiver_waits_for_a_newer_snapshot(tmp_path) -> None:
    artifact = tmp_path / "observer.exe"
    artifact.write_bytes(b"MZ")
    receiver = ObserverReceiver(artifact=artifact, token="token")

    assert receiver.wait_for_sequence(0, timeout_s=0.01) is None
    receiver.accept(_snapshot(sequence=7), token="token")
    snapshot = receiver.wait_for_sequence(6, timeout_s=0.01)
    assert snapshot is not None
    assert snapshot.sequence == 7


def test_evaluator_releases_ground_truth_only_after_trial_is_sealed(tmp_path) -> None:
    artifact = tmp_path / "observer.exe"
    artifact.write_bytes(b"MZ")
    receiver = ObserverReceiver(artifact=artifact, token="token")
    public = TestClient(receiver.public_app)
    evaluator = TestClient(receiver.evaluator_app)

    public.post(
        "/ingest",
        headers={"X-Observer-Token": "token"},
        json=_snapshot(sequence=2),
    )
    trial = evaluator.post("/trials").json()

    # A pre-trial snapshot cannot satisfy the result.
    timed_out = evaluator.post(
        f"/trials/{trial['trial_id']}/seal",
        json={"intended": "exact text", "timeout_s": 0.01},
    )
    assert timed_out.status_code == 504

    public.post(
        "/ingest",
        headers={"X-Observer-Token": "token"},
        json=_snapshot(sequence=3),
    )
    sealed = evaluator.post(
        f"/trials/{trial['trial_id']}/seal",
        json={"intended": "exact text", "timeout_s": 0.01},
    )
    assert sealed.status_code == 200
    assert sealed.json()["exact_match"] is True
