from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Callable

import pytest

from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    MediaTransactionState,
    RunSnapshot,
    RunStatus,
    utc_now,
)
from pikvm_agent.harness.agent_store import InMemoryRunStore
from pikvm_agent.harness.media_transaction import (
    MediaArtifactRepository,
    LocalMediaArtifactRepository,
    MediaMutationAmbiguousError,
    MediaMutationDefiniteError,
    MediaTargetState,
    MediaTransactionCoordinator,
    MediaTransferRequest,
    MediaTransport,
)
from pikvm_agent.pikvm.msd_media import (
    MediaFileReceipt,
    ReadOnlyMediaReceipt,
)


class MemoryArtifacts(MediaArtifactRepository):
    def __init__(self) -> None:
        self.paths: dict[str, Path] = {}
        self.removed: list[str] = []

    async def stage(
        self,
        transaction_id: str,
        receipt: ReadOnlyMediaReceipt,
    ) -> None:
        self.paths[transaction_id] = receipt.image_path

    async def path_for(self, transaction_id: str) -> Path:
        return self.paths[transaction_id]

    async def remove(self, transaction_id: str) -> None:
        self.paths.pop(transaction_id, None)
        self.removed.append(transaction_id)


class FakeMediaTransport(MediaTransport):
    def __init__(self) -> None:
        self.state = MediaTargetState(
            adapter="pikvm",
            supported=True,
            machine_fingerprint="machine-7",
            control_epoch=4,
            drive_online=True,
            drive_busy=False,
            connected=False,
            selected_image=None,
            images=[],
            storage_free_bytes=64 * 1024 * 1024,
        )
        self.mutations: list[tuple[str, object]] = []
        self.ambiguous_on: str | None = None
        self.definite_on: str | None = None

    async def inspect(self, session_id: str) -> MediaTargetState:
        assert session_id == "session-lab"
        return self.state.model_copy(deep=True)

    async def upload(
        self,
        session_id: str,
        name: str,
        image_path: Path,
    ) -> None:
        assert session_id == "session-lab"
        self.mutations.append(("upload", name))
        if self.ambiguous_on == "upload":
            raise MediaMutationAmbiguousError("upload response was lost")
        if self.definite_on == "upload":
            raise MediaMutationDefiniteError("upload was refused")
        self.state.images.append(name)

    async def select(self, session_id: str, name: str | None) -> None:
        assert session_id == "session-lab"
        self.mutations.append(("select", name))
        if self.ambiguous_on == "select":
            raise MediaMutationAmbiguousError("select response was lost")
        if self.definite_on == "select":
            raise MediaMutationDefiniteError("select was refused")
        self.state.selected_image = name

    async def connect(self, session_id: str) -> None:
        assert session_id == "session-lab"
        self.mutations.append(("connect", True))
        if self.ambiguous_on == "connect":
            raise MediaMutationAmbiguousError("connect response was lost")
        if self.definite_on == "connect":
            raise MediaMutationDefiniteError("connect was refused")
        self.state.connected = True

    async def disconnect(self, session_id: str) -> None:
        assert session_id == "session-lab"
        self.mutations.append(("disconnect", True))
        if self.ambiguous_on == "disconnect":
            raise MediaMutationAmbiguousError("disconnect response was lost")
        if self.definite_on == "disconnect":
            raise MediaMutationDefiniteError("disconnect was refused")
        self.state.connected = False

    async def remove(self, session_id: str, name: str) -> None:
        assert session_id == "session-lab"
        self.mutations.append(("remove", name))
        if self.ambiguous_on == "remove":
            raise MediaMutationAmbiguousError("remove response was lost")
        if self.definite_on == "remove":
            raise MediaMutationDefiniteError("remove was refused")
        self.state.images.remove(name)


async def configured_coordinator(
    tmp_path: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> tuple[
    MediaTransactionCoordinator,
    InMemoryRunStore,
    MemoryArtifacts,
    FakeMediaTransport,
]:
    store = InMemoryRunStore()
    run = RunSnapshot(
        run_id="run-media",
        task="Open the supplied quarterly earnings workbook",
        status=RunStatus.PAUSED,
        session_id="session-lab",
        observation=ComputerObservation(
            session_id="session-lab",
            status="ready",
            machine={
                "alias": "Disposable Windows lab",
                "fingerprint": "machine-7",
                "desktop_layer": "windows",
            },
            control_epoch=4,
        ),
    )
    await store.save(run)
    artifacts = MemoryArtifacts()
    transport = FakeMediaTransport()
    coordinator = MediaTransactionCoordinator(
        store=store,
        artifacts=artifacts,
        transport=transport,
        **({"clock": clock} if clock is not None else {}),
    )
    return coordinator, store, artifacts, transport


def media_receipt(tmp_path: Path) -> ReadOnlyMediaReceipt:
    image = tmp_path / "quarterly-earnings.iso"
    image.write_bytes(b"exact read-only media")
    return ReadOnlyMediaReceipt(
        image_path=image,
        image_sha256="f8630ff6e99b04b088e179fe71815b80"
        "e0c68f7739d683958400af758cad3db4",
        manifest_sha256="b" * 64,
        files=(
            MediaFileReceipt(
                name="quarterly-earnings.xlsx",
                size=25,
                sha256="c" * 64,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_attach_requires_exact_operator_approval_before_mutation(
    tmp_path: Path,
) -> None:
    coordinator, store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Open the exact workbook without HID text transfer",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )

    transaction = staged.media_transaction
    assert transaction is not None
    assert transaction.state is MediaTransactionState.AWAITING_APPROVAL
    assert staged.status is RunStatus.NEEDS_APPROVAL
    assert staged.pending_approval == {
        "approval_id": transaction.approval_id,
        "kind": "virtual_media_attach",
        "transaction_id": transaction.transaction_id,
        "session_id": "session-lab",
        "risk": "external_file_upload",
        "reason": "Open the exact workbook without HID text transfer",
        "machine": {
            "alias": "Disposable Windows lab",
            "fingerprint": "machine-7",
            "desktop_layer": "windows",
        },
        "control_epoch": 4,
        "media_name": "pikvm-598fd477b3bf.iso",
        "image_sha256": transaction.image_sha256,
        "image_bytes": len(b"exact read-only media"),
        "read_only": True,
        "lease_expires_at": transaction.lease_expires_at.isoformat(),
    }
    assert transport.mutations == []

    with pytest.raises(ValueError, match="does not match"):
        await coordinator.resolve_approval(
            "run-media",
            "wrong-approval",
            {"type": "approve", "reason": "not exact"},
        )
    assert transport.mutations == []

    attached = await coordinator.resolve_approval(
        "run-media",
        transaction.approval_id,
        {"type": "approve", "reason": "lab workbook is expected"},
    )

    assert attached.media_transaction is not None
    assert attached.media_transaction.state is MediaTransactionState.ATTACHED
    assert attached.pending_approval is None
    assert attached.status is RunStatus.PAUSED
    assert transport.mutations == [
        ("upload", "pikvm-598fd477b3bf.iso"),
        ("select", "pikvm-598fd477b3bf.iso"),
        ("connect", True),
    ]
    assert await artifacts.path_for(transaction.transaction_id) == (
        tmp_path / "quarterly-earnings.iso"
    )
    persisted = await store.get("run-media")
    assert [
        event.kind
        for event in persisted.events
        if event.kind.startswith("media.")
    ] == [
        "media.prepared",
        "media.awaiting_approval",
        "media.upload_started",
        "media.upload_verified",
        "media.select_started",
        "media.select_verified",
        "media.attach_started",
        "media.attached",
    ]


@pytest.mark.asyncio
async def test_operator_rejection_removes_private_media_without_target_mutation(
    tmp_path: Path,
) -> None:
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    transaction_id = staged.media_transaction.transaction_id

    rejected = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "reject", "reason": "unexpected file"},
    )

    assert rejected.media_transaction is not None
    assert rejected.media_transaction.state is MediaTransactionState.REJECTED
    assert rejected.status is RunStatus.PAUSED
    assert rejected.pending_approval is None
    assert transport.mutations == []
    assert transaction_id in artifacts.removed


@pytest.mark.asyncio
async def test_ambiguous_upload_latches_cleanup_without_retry_or_delete(
    tmp_path: Path,
) -> None:
    coordinator, store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    transport.ambiguous_on = "upload"

    result = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )

    assert result.media_transaction is not None
    assert (
        result.media_transaction.state
        is MediaTransactionState.CLEANUP_REQUIRED
    )
    assert result.media_transaction.cleanup_reason == "upload response was lost"
    assert result.status is RunStatus.BLOCKED
    assert result.error == "virtual media outcome is uncertain; cleanup required"
    assert transport.mutations == [
        ("upload", "pikvm-598fd477b3bf.iso"),
    ]
    assert result.events[-1].kind == "media.cleanup_required"
    assert await artifacts.path_for(
        staged.media_transaction.transaction_id
    ) == (tmp_path / "quarterly-earnings.iso")
    persisted = await store.get("run-media")
    assert persisted.media_transaction == result.media_transaction


@pytest.mark.asyncio
async def test_definite_select_failure_rolls_back_uploaded_image_once(
    tmp_path: Path,
) -> None:
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    transaction_id = staged.media_transaction.transaction_id
    transport.definite_on = "select"

    result = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )

    assert result.media_transaction is not None
    assert result.media_transaction.state is MediaTransactionState.RELEASED
    assert result.media_transaction.failure_reason == "select was refused"
    assert result.media_transaction.cleanup_reason is None
    assert result.status is RunStatus.BLOCKED
    assert result.error == (
        "virtual media attach failed; rollback was confirmed"
    )
    assert transport.mutations == [
        ("upload", "pikvm-598fd477b3bf.iso"),
        ("select", "pikvm-598fd477b3bf.iso"),
        ("remove", "pikvm-598fd477b3bf.iso"),
    ]
    assert transaction_id in artifacts.removed


@pytest.mark.asyncio
async def test_vnc_without_exact_byte_capability_refuses_before_staging(
    tmp_path: Path,
) -> None:
    coordinator, store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    transport.state.adapter = "vnc"
    transport.state.supported = False
    transport.state.unsupported_reason = (
        "arbitrary VNC does not support exact-byte virtual media"
    )

    with pytest.raises(
        ValueError,
        match="VNC does not support exact-byte virtual media",
    ):
        await coordinator.stage_artifact(
            "run-media",
            MediaTransferRequest(
                purpose="Stage exact source",
                machine_fingerprint="machine-7",
                control_epoch=4,
                media_name="pikvm-598fd477b3bf.iso",
                attach_lease=timedelta(minutes=10),
            ),
            media_receipt(tmp_path),
        )

    assert artifacts.paths == {}
    assert transport.mutations == []
    refused = await store.get("run-media")
    assert refused.status is RunStatus.BLOCKED
    assert refused.events[-1].kind == "media.unsupported"
    assert "VNC does not support" in (refused.error or "")


@pytest.mark.asyncio
async def test_target_change_after_approval_is_clean_refusal_not_uncertainty(
    tmp_path: Path,
) -> None:
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    transaction_id = staged.media_transaction.transaction_id
    transport.state.machine_fingerprint = "different-machine"

    refused = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )

    assert refused.media_transaction is not None
    assert refused.media_transaction.state is MediaTransactionState.REJECTED
    assert refused.media_transaction.cleanup_reason is None
    assert refused.media_transaction.failure_reason == (
        "target identity changed before media mutation"
    )
    assert refused.status is RunStatus.BLOCKED
    assert refused.events[-1].kind == "media.preflight_refused"
    assert transport.mutations == []
    assert transaction_id in artifacts.removed


@pytest.mark.asyncio
async def test_expired_lease_refuses_before_upload(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 7, 26, 12, 0, tzinfo=UTC)]
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path,
        clock=lambda: current[0],
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(seconds=30),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    current[0] += timedelta(seconds=31)

    refused = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "too late"},
    )

    assert refused.media_transaction is not None
    assert refused.media_transaction.state is MediaTransactionState.REJECTED
    assert refused.media_transaction.failure_reason == (
        "attach lease expired before upload"
    )
    assert transport.mutations == []
    assert staged.media_transaction.transaction_id in artifacts.removed


@pytest.mark.asyncio
async def test_release_disconnects_clears_selection_and_removes_exact_image(
    tmp_path: Path,
) -> None:
    coordinator, store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    attached = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )
    assert attached.media_transaction is not None
    transaction_id = attached.media_transaction.transaction_id

    released = await coordinator.release(
        "run-media",
        "acceptance file has been read",
    )

    assert released.media_transaction is not None
    assert released.media_transaction.state is MediaTransactionState.RELEASED
    assert released.media_transaction.released_at is not None
    assert transport.mutations == [
        ("upload", "pikvm-598fd477b3bf.iso"),
        ("select", "pikvm-598fd477b3bf.iso"),
        ("connect", True),
        ("disconnect", True),
        ("select", None),
        ("remove", "pikvm-598fd477b3bf.iso"),
    ]
    assert transaction_id in artifacts.removed
    persisted = await store.get("run-media")
    media_events = [
        event.kind
        for event in persisted.events
        if event.kind.startswith("media.")
    ]
    assert media_events[-6:] == [
        "media.detach_started",
        "media.detach_verified",
        "media.selection_clear_started",
        "media.selection_clear_verified",
        "media.remove_started",
        "media.released",
    ]


@pytest.mark.asyncio
async def test_ambiguous_disconnect_stops_before_clear_or_remove(
    tmp_path: Path,
) -> None:
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(minutes=10),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    attached = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )
    assert attached.media_transaction is not None
    transaction_id = attached.media_transaction.transaction_id
    transport.ambiguous_on = "disconnect"

    uncertain = await coordinator.release("run-media", "emergency stop")

    assert uncertain.media_transaction is not None
    assert (
        uncertain.media_transaction.state
        is MediaTransactionState.CLEANUP_REQUIRED
    )
    assert transport.mutations[-1] == ("disconnect", True)
    assert ("select", None) not in transport.mutations
    assert ("remove", "pikvm-598fd477b3bf.iso") not in transport.mutations
    assert transaction_id not in artifacts.removed


@pytest.mark.asyncio
async def test_expired_attached_lease_is_released_without_model_action(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 7, 26, 12, 0, tzinfo=UTC)]
    coordinator, _store, artifacts, transport = await configured_coordinator(
        tmp_path,
        clock=lambda: current[0],
    )
    staged = await coordinator.stage_artifact(
        "run-media",
        MediaTransferRequest(
            purpose="Stage exact source",
            machine_fingerprint="machine-7",
            control_epoch=4,
            media_name="pikvm-598fd477b3bf.iso",
            attach_lease=timedelta(seconds=30),
        ),
        media_receipt(tmp_path),
    )
    assert staged.media_transaction is not None
    attached = await coordinator.resolve_approval(
        "run-media",
        staged.media_transaction.approval_id,
        {"type": "approve", "reason": "expected lab content"},
    )
    assert attached.media_transaction is not None
    transaction_id = attached.media_transaction.transaction_id
    current[0] += timedelta(seconds=31)

    released = await coordinator.release_expired()

    assert len(released) == 1
    assert released[0].media_transaction is not None
    assert (
        released[0].media_transaction.state
        is MediaTransactionState.RELEASED
    )
    assert transport.mutations[-3:] == [
        ("disconnect", True),
        ("select", None),
        ("remove", "pikvm-598fd477b3bf.iso"),
    ]
    assert transaction_id in artifacts.removed


@pytest.mark.asyncio
async def test_local_artifact_repository_keeps_private_exact_non_overwriting_copy(
    tmp_path: Path,
) -> None:
    receipt = media_receipt(tmp_path)
    repository = LocalMediaArtifactRepository(tmp_path / "private-media")

    await repository.stage("media-abc123", receipt)
    staged = await repository.path_for("media-abc123")

    assert staged != receipt.image_path
    assert staged.read_bytes() == receipt.image_path.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o600
    assert staged.parent.stat().st_mode & 0o777 == 0o700
    receipt.image_path.write_bytes(b"changed source")
    assert staged.read_bytes() == b"exact read-only media"
    with pytest.raises(FileExistsError, match="already exists"):
        await repository.stage("media-abc123", media_receipt(tmp_path))
    with pytest.raises(ValueError, match="invalid transaction id"):
        await repository.path_for("../escape")

    await repository.remove("media-abc123")
    assert not staged.exists()


def test_public_virtual_media_transaction_report_is_fail_closed() -> None:
    report = json.loads(
        (
            Path(__file__).parents[1]
            / "bench"
            / "results"
            / "2026-07-25"
            / "safety"
            / "virtual-media-transaction-2026-07-26.json"
        ).read_text()
    )

    assert report["contracts"] == {
        "passed": 19,
        "total": 19,
        "transaction_coordinator": 11,
        "daemon_adapter": 2,
        "operator_api": 2,
        "ui_projection_and_budget": 2,
        "model_surface_exclusion": 2,
    }
    assert report["target_contacted"] is False
    assert report["safety_boundary"] == {
        "model_facing_transfer_tool": False,
        "daemon_mutation_bridge_exposed": False,
        "reason": (
            "The daemon bridge remains fail-closed until mutations require "
            "a one-time capability bound to the exact browser-approved "
            "checkpoint."
        ),
    }
