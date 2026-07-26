"""Approval-gated, durable read-only virtual-media transactions.

The coordinator is the product seam.  Transport adapters provide evidence and
bounded MSD mutations; they do not own approval, target binding, retries,
rollback, or the public run trace.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pikvm_agent.harness.agent_models import (
    MediaFileEvidence,
    MediaTransaction,
    MediaTransactionState,
    RunSnapshot,
    RunStatus,
    utc_now,
)
from pikvm_agent.harness.agent_store import RunStore
from pikvm_agent.pikvm.msd_media import ReadOnlyMediaReceipt

_SAFE_MEDIA_IMAGE_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,98}\.iso\Z",
    re.IGNORECASE,
)
_SAFE_TRANSACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_MINIMUM_LEASE = timedelta(seconds=30)
_MAXIMUM_LEASE = timedelta(hours=24)


class MediaMutationAmbiguousError(RuntimeError):
    """A mutating request may have changed the target, but proof was lost."""


class MediaMutationDefiniteError(RuntimeError):
    """The adapter proved that a mutating request did not take effect."""


class MediaPreflightError(ValueError):
    """A transaction was safely refused before any target mutation."""


class MediaTransferRequest(BaseModel):
    """Operator-visible intent and exact target binding for one media image."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    purpose: str = Field(min_length=1, max_length=500)
    machine_fingerprint: str = Field(min_length=1, max_length=500)
    control_epoch: int = Field(ge=0)
    media_name: str = Field(min_length=5, max_length=103)
    attach_lease: timedelta

    @field_validator("media_name")
    @classmethod
    def safe_content_addressed_name(cls, value: str) -> str:
        if not _SAFE_MEDIA_IMAGE_NAME.fullmatch(value):
            raise ValueError("media name must be a safe flat ISO filename")
        return value

    @field_validator("attach_lease")
    @classmethod
    def bounded_lease(cls, value: timedelta) -> timedelta:
        if value < _MINIMUM_LEASE or value > _MAXIMUM_LEASE:
            raise ValueError("attach lease must be between 30 seconds and 24 hours")
        return value


class MediaTargetState(BaseModel):
    """Read-only state returned by a target adapter."""

    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(min_length=1, max_length=100)
    supported: bool
    unsupported_reason: str | None = Field(default=None, max_length=500)
    machine_fingerprint: str = Field(min_length=1, max_length=500)
    control_epoch: int = Field(ge=0)
    drive_online: bool = False
    drive_busy: bool = False
    connected: bool = False
    selected_image: str | None = Field(default=None, max_length=103)
    images: list[str] = Field(default_factory=list, max_length=10_000)
    storage_free_bytes: int | None = Field(default=None, ge=0)


class MediaArtifactRepository(Protocol):
    """Private artifact storage; implementations never expose paths to models."""

    async def stage(
        self,
        transaction_id: str,
        receipt: ReadOnlyMediaReceipt,
    ) -> None: ...

    async def path_for(self, transaction_id: str) -> Path: ...

    async def remove(self, transaction_id: str) -> None: ...


class LocalMediaArtifactRepository:
    """Mode-0600 durable media copies under one mode-0700 state directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def stage(
        self,
        transaction_id: str,
        receipt: ReadOnlyMediaReceipt,
    ) -> None:
        destination = self._path(transaction_id)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        digest = hashlib.sha256()
        try:
            with receipt.image_path.open("rb") as source:
                with destination.open("xb") as target:
                    for chunk in iter(
                        lambda: source.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            destination.chmod(0o600)
        except FileExistsError as exc:
            raise FileExistsError(
                f"prepared media already exists for {transaction_id}"
            ) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if not secrets.compare_digest(
            digest.hexdigest(),
            receipt.image_sha256,
        ):
            destination.unlink(missing_ok=True)
            raise ValueError("prepared media image hash does not match receipt")

    async def path_for(self, transaction_id: str) -> Path:
        path = self._path(transaction_id)
        if not path.is_file():
            raise FileNotFoundError(
                f"prepared media is unavailable for {transaction_id}"
            )
        return path

    async def remove(self, transaction_id: str) -> None:
        self._path(transaction_id).unlink(missing_ok=True)

    def _path(self, transaction_id: str) -> Path:
        if not _SAFE_TRANSACTION_ID.fullmatch(transaction_id):
            raise ValueError("invalid transaction id for private media store")
        return self._root / f"{transaction_id}.iso"


class MediaTransport(Protocol):
    """Bounded target adapter used only by the transaction coordinator."""

    async def inspect(self, session_id: str) -> MediaTargetState: ...

    async def upload(
        self,
        session_id: str,
        name: str,
        image_path: Path,
    ) -> None: ...

    async def select(self, session_id: str, name: str | None) -> None: ...

    async def connect(self, session_id: str) -> None: ...

    async def disconnect(self, session_id: str) -> None: ...

    async def remove(self, session_id: str, name: str) -> None: ...


class MediaTransactionCoordinator:
    """Own exact approval, durable state changes, and uncertain outcomes."""

    def __init__(
        self,
        *,
        store: RunStore,
        artifacts: MediaArtifactRepository,
        transport: MediaTransport,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self._transport = transport
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    async def stage_artifact(
        self,
        run_id: str,
        request: MediaTransferRequest,
        receipt: ReadOnlyMediaReceipt,
    ) -> RunSnapshot:
        async with self._lock_for(run_id):
            run = await self._store.get_state(run_id)
            if run.media_transaction is not None:
                raise ValueError("run already owns a virtual media transaction")
            image_bytes = self._validate_receipt(receipt)
            self._validate_run_target(run, request)
            target_adapter = "unavailable"
            try:
                if run.session_id is None:
                    raise MediaPreflightError(
                        "run has no active computer session"
                    )
                target = await self._transport.inspect(run.session_id)
                target_adapter = target.adapter
                self._validate_preflight(
                    target,
                    machine_fingerprint=request.machine_fingerprint,
                    control_epoch=request.control_epoch,
                    media_name=request.media_name,
                    image_bytes=image_bytes,
                )
            except (MediaPreflightError, OSError, RuntimeError) as exc:
                reason = str(exc)[:500]
                run.status = RunStatus.BLOCKED
                run.error = f"virtual media preflight refused: {reason}"
                run.record(
                    (
                        "media.unsupported"
                        if "does not support" in reason
                        else "media.preflight_refused"
                    ),
                    adapter=target_adapter,
                    reason=reason,
                    machine_fingerprint=request.machine_fingerprint,
                    control_epoch=request.control_epoch,
                )
                await self._store.save(run)
                raise ValueError(reason) from exc

            transaction_id = f"media-{secrets.token_hex(12)}"
            approval_id = f"approval-{secrets.token_hex(12)}"
            await self._artifacts.stage(transaction_id, receipt)
            transaction = MediaTransaction(
                transaction_id=transaction_id,
                state=MediaTransactionState.PREPARED,
                approval_id=approval_id,
                purpose=request.purpose,
                session_id=run.session_id,
                machine_fingerprint=request.machine_fingerprint,
                control_epoch=request.control_epoch,
                adapter=target.adapter,
                media_name=request.media_name,
                image_sha256=receipt.image_sha256,
                image_bytes=image_bytes,
                manifest_sha256=receipt.manifest_sha256,
                files=[
                    MediaFileEvidence(
                        name=item.name,
                        size=item.size,
                        sha256=item.sha256,
                    )
                    for item in receipt.files
                ],
                lease_expires_at=self._clock() + request.attach_lease,
            )
            run.media_transaction = transaction
            run.record(
                "media.prepared",
                transaction_id=transaction_id,
                media_name=transaction.media_name,
                image_sha256=transaction.image_sha256,
                image_bytes=transaction.image_bytes,
                machine_fingerprint=transaction.machine_fingerprint,
                control_epoch=transaction.control_epoch,
            )
            await self._store.save(run)

            transaction.state = MediaTransactionState.AWAITING_APPROVAL
            run.status = RunStatus.NEEDS_APPROVAL
            run.pending_approval = self._approval_request(run, transaction)
            run.record(
                "media.awaiting_approval",
                transaction_id=transaction_id,
                approval_id=approval_id,
                lease_expires_at=transaction.lease_expires_at.isoformat(),
            )
            run.record(
                "approval.required",
                approval_id=approval_id,
                risk="external_file_upload",
                approval_kind="virtual_media_attach",
            )
            await self._store.save(run)
            return run

    async def resolve_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
    ) -> RunSnapshot:
        async with self._lock_for(run_id):
            run = await self._store.get_state(run_id)
            transaction = self._pending_transaction(run, approval_id)
            decision_type = str(decision.get("type") or "")
            reason = str(decision.get("reason") or "")[:500]
            if decision_type not in {"approve", "reject"}:
                raise ValueError("approval decision must be approve or reject")
            if decision_type == "reject":
                transaction.state = MediaTransactionState.REJECTED
                run.pending_approval = None
                run.status = RunStatus.PAUSED
                run.record(
                    "approval.not_approved",
                    approval_id=approval_id,
                    reason=reason,
                )
                run.record(
                    "media.rejected",
                    transaction_id=transaction.transaction_id,
                    reason=reason,
                )
                await self._store.save(run)
                await self._artifacts.remove(transaction.transaction_id)
                return run

            run.pending_approval = None
            run.record(
                "approval.approved",
                approval_id=approval_id,
                reason=reason,
            )
            try:
                return await self._attach(run, transaction)
            except MediaPreflightError as exc:
                return await self._refuse_before_mutation(
                    run,
                    transaction,
                    str(exc),
                )
            except MediaMutationAmbiguousError as exc:
                return await self._latch_cleanup_required(
                    run,
                    transaction,
                    str(exc),
                )
            except MediaMutationDefiniteError as exc:
                return await self._rollback_after_definite_failure(
                    run,
                    transaction,
                    str(exc),
                )
            except Exception as exc:
                return await self._latch_cleanup_required(
                    run,
                    transaction,
                    f"{type(exc).__name__}: {exc}",
                )

    async def release(
        self,
        run_id: str,
        reason: str = "virtual media no longer needed",
    ) -> RunSnapshot:
        """Detach and remove known attached media with proof after each step."""

        async with self._lock_for(run_id):
            run = await self._store.get_state(run_id)
            transaction = run.media_transaction
            if transaction is None:
                raise ValueError("run has no virtual media transaction")
            if transaction.state not in {
                MediaTransactionState.ATTACHED,
                MediaTransactionState.VERIFIED,
            }:
                raise ValueError(
                    "virtual media is not in a safely releasable state"
                )
            try:
                target = await self._transport.inspect(transaction.session_id)
                self._require_identity(target, transaction)
                if (
                    not target.connected
                    or target.selected_image != transaction.media_name
                ):
                    raise MediaMutationAmbiguousError(
                        "attached media state changed before release"
                    )

                transaction.state = MediaTransactionState.DETACHING
                run.record(
                    "media.detach_started",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                    reason=reason[:500],
                )
                await self._store.save(run)
                await self._transport.disconnect(transaction.session_id)
                detached = await self._transport.inspect(transaction.session_id)
                self._require_identity(detached, transaction)
                if detached.connected:
                    raise MediaMutationAmbiguousError(
                        "disconnect returned while media remained connected"
                    )
                run.record(
                    "media.detach_verified",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)

                run.record(
                    "media.selection_clear_started",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)
                await self._transport.select(transaction.session_id, None)
                cleared = await self._transport.inspect(transaction.session_id)
                self._require_identity(cleared, transaction)
                if cleared.selected_image is not None:
                    raise MediaMutationAmbiguousError(
                        "selection clear returned while media remained selected"
                    )
                run.record(
                    "media.selection_clear_verified",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)

                run.record(
                    "media.remove_started",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)
                await self._transport.remove(
                    transaction.session_id,
                    transaction.media_name,
                )
                removed = await self._transport.inspect(transaction.session_id)
                self._require_identity(removed, transaction)
                if transaction.media_name in removed.images:
                    raise MediaMutationAmbiguousError(
                        "remove returned while media image remained present"
                    )
                transaction.state = MediaTransactionState.RELEASED
                transaction.released_at = self._clock()
                run.record(
                    "media.released",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)
                await self._artifacts.remove(transaction.transaction_id)
                return run
            except MediaMutationAmbiguousError as exc:
                return await self._latch_cleanup_required(
                    run,
                    transaction,
                    str(exc),
                )
            except Exception as exc:
                return await self._latch_cleanup_required(
                    run,
                    transaction,
                    f"{type(exc).__name__}: {exc}",
                )

    async def release_expired(self) -> list[RunSnapshot]:
        """Release every known attached image whose durable lease has expired."""

        released: list[RunSnapshot] = []
        for run in await self._store.list(limit=1_000):
            transaction = run.media_transaction
            if (
                transaction is None
                or transaction.state
                not in {
                    MediaTransactionState.ATTACHED,
                    MediaTransactionState.VERIFIED,
                }
                or self._clock() < transaction.lease_expires_at
            ):
                continue
            released.append(
                await self.release(run.run_id, "attach lease expired")
            )
        return released

    async def _attach(
        self,
        run: RunSnapshot,
        transaction: MediaTransaction,
    ) -> RunSnapshot:
        try:
            target = await self._transport.inspect(transaction.session_id)
        except Exception as exc:
            raise MediaPreflightError(
                f"target inspection failed before upload: {type(exc).__name__}"
            ) from exc
        self._validate_preflight(
            target,
            machine_fingerprint=transaction.machine_fingerprint,
            control_epoch=transaction.control_epoch,
            media_name=transaction.media_name,
            image_bytes=transaction.image_bytes,
        )
        if self._clock() >= transaction.lease_expires_at:
            raise MediaPreflightError("attach lease expired before upload")

        transaction.state = MediaTransactionState.UPLOADING
        run.status = RunStatus.PAUSED
        run.record(
            "media.upload_started",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            image_bytes=transaction.image_bytes,
        )
        await self._store.save(run)
        image_path = await self._artifacts.path_for(transaction.transaction_id)
        await self._transport.upload(
            transaction.session_id,
            transaction.media_name,
            image_path,
        )
        uploaded = await self._transport.inspect(transaction.session_id)
        self._require_identity(uploaded, transaction)
        if transaction.media_name not in uploaded.images:
            raise MediaMutationAmbiguousError(
                "upload returned without a matching target image"
            )
        run.record(
            "media.upload_verified",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
        )
        await self._store.save(run)

        run.record(
            "media.select_started",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            read_only=True,
        )
        await self._store.save(run)
        await self._transport.select(
            transaction.session_id,
            transaction.media_name,
        )
        selected = await self._transport.inspect(transaction.session_id)
        self._require_identity(selected, transaction)
        if selected.selected_image != transaction.media_name:
            raise MediaMutationAmbiguousError(
                "select returned without the expected selected image"
            )
        transaction.state = MediaTransactionState.SELECTED
        run.record(
            "media.select_verified",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            read_only=True,
        )
        await self._store.save(run)

        run.record(
            "media.attach_started",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
        )
        await self._store.save(run)
        await self._transport.connect(transaction.session_id)
        attached = await self._transport.inspect(transaction.session_id)
        self._require_identity(attached, transaction)
        if (
            not attached.connected
            or attached.selected_image != transaction.media_name
        ):
            raise MediaMutationAmbiguousError(
                "connect returned without the expected attached image"
            )
        transaction.state = MediaTransactionState.ATTACHED
        transaction.attached_at = self._clock()
        run.record(
            "media.attached",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            lease_expires_at=transaction.lease_expires_at.isoformat(),
        )
        await self._store.save(run)
        return run

    async def _refuse_before_mutation(
        self,
        run: RunSnapshot,
        transaction: MediaTransaction,
        reason: str,
    ) -> RunSnapshot:
        transaction.state = MediaTransactionState.REJECTED
        transaction.failure_reason = reason[:500]
        run.pending_approval = None
        run.status = RunStatus.BLOCKED
        run.error = f"virtual media attach refused before upload: {reason}"
        run.record(
            "media.preflight_refused",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            reason=transaction.failure_reason,
        )
        await self._store.save(run)
        await self._artifacts.remove(transaction.transaction_id)
        return run

    async def _rollback_after_definite_failure(
        self,
        run: RunSnapshot,
        transaction: MediaTransaction,
        reason: str,
    ) -> RunSnapshot:
        """Clean only state proven to belong to this transaction."""

        transaction.state = MediaTransactionState.ROLLING_BACK
        transaction.failure_reason = reason[:500]
        run.record(
            "media.rollback_started",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            reason=transaction.failure_reason,
        )
        await self._store.save(run)
        try:
            state = await self._transport.inspect(transaction.session_id)
            self._require_identity(state, transaction)
            if (
                state.connected
                and state.selected_image != transaction.media_name
            ):
                raise MediaMutationAmbiguousError(
                    "rollback found unrelated connected media"
                )
            if state.connected:
                run.record(
                    "media.rollback_disconnect_started",
                    transaction_id=transaction.transaction_id,
                )
                await self._store.save(run)
                await self._transport.disconnect(transaction.session_id)
                state = await self._transport.inspect(transaction.session_id)
                self._require_identity(state, transaction)
                if state.connected:
                    raise MediaMutationAmbiguousError(
                        "rollback disconnect was not confirmed"
                    )
            if state.selected_image not in {None, transaction.media_name}:
                raise MediaMutationAmbiguousError(
                    "rollback found unrelated selected media"
                )
            if state.selected_image == transaction.media_name:
                run.record(
                    "media.rollback_selection_clear_started",
                    transaction_id=transaction.transaction_id,
                )
                await self._store.save(run)
                await self._transport.select(transaction.session_id, None)
                state = await self._transport.inspect(transaction.session_id)
                self._require_identity(state, transaction)
                if state.selected_image is not None:
                    raise MediaMutationAmbiguousError(
                        "rollback selection clear was not confirmed"
                    )
            if transaction.media_name in state.images:
                run.record(
                    "media.rollback_remove_started",
                    transaction_id=transaction.transaction_id,
                    media_name=transaction.media_name,
                )
                await self._store.save(run)
                await self._transport.remove(
                    transaction.session_id,
                    transaction.media_name,
                )
                state = await self._transport.inspect(transaction.session_id)
                self._require_identity(state, transaction)
                if transaction.media_name in state.images:
                    raise MediaMutationAmbiguousError(
                        "rollback image removal was not confirmed"
                    )
        except Exception as exc:
            return await self._latch_cleanup_required(
                run,
                transaction,
                f"rollback could not be proven: {type(exc).__name__}: {exc}",
            )

        transaction.state = MediaTransactionState.RELEASED
        transaction.released_at = self._clock()
        run.status = RunStatus.BLOCKED
        run.error = "virtual media attach failed; rollback was confirmed"
        run.record(
            "media.rollback_confirmed",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            reason=transaction.failure_reason,
        )
        await self._store.save(run)
        await self._artifacts.remove(transaction.transaction_id)
        return run

    async def _latch_cleanup_required(
        self,
        run: RunSnapshot,
        transaction: MediaTransaction,
        reason: str,
    ) -> RunSnapshot:
        transaction.state = MediaTransactionState.CLEANUP_REQUIRED
        transaction.cleanup_reason = reason[:500]
        run.pending_approval = None
        run.status = RunStatus.BLOCKED
        run.error = "virtual media outcome is uncertain; cleanup required"
        run.record(
            "media.cleanup_required",
            transaction_id=transaction.transaction_id,
            media_name=transaction.media_name,
            reason=transaction.cleanup_reason,
        )
        await self._store.save(run)
        return run

    @staticmethod
    def _pending_transaction(
        run: RunSnapshot,
        approval_id: str,
    ) -> MediaTransaction:
        transaction = run.media_transaction
        if (
            transaction is None
            or transaction.state is not MediaTransactionState.AWAITING_APPROVAL
        ):
            raise ValueError("run has no pending virtual media approval")
        pending = run.pending_approval or {}
        expected = str(pending.get("approval_id") or "")
        if (
            not expected
            or not secrets.compare_digest(expected, approval_id)
            or not secrets.compare_digest(transaction.approval_id, approval_id)
        ):
            raise ValueError("approval id does not match the pending transaction")
        return transaction

    @staticmethod
    def _approval_request(
        run: RunSnapshot,
        transaction: MediaTransaction,
    ) -> dict[str, Any]:
        machine = dict(run.observation.machine if run.observation else {})
        return {
            "approval_id": transaction.approval_id,
            "kind": "virtual_media_attach",
            "transaction_id": transaction.transaction_id,
            "session_id": transaction.session_id,
            "risk": "external_file_upload",
            "reason": transaction.purpose,
            "machine": machine,
            "control_epoch": transaction.control_epoch,
            "media_name": transaction.media_name,
            "image_sha256": transaction.image_sha256,
            "image_bytes": transaction.image_bytes,
            "read_only": True,
            "lease_expires_at": transaction.lease_expires_at.isoformat(),
        }

    @staticmethod
    def _validate_receipt(receipt: ReadOnlyMediaReceipt) -> int:
        path = receipt.image_path
        if not path.is_file():
            raise ValueError("prepared media image does not exist")
        digest = hashlib.sha256()
        image_bytes = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                image_bytes += len(chunk)
        if image_bytes < 1:
            raise ValueError("prepared media image is empty")
        if not secrets.compare_digest(digest.hexdigest(), receipt.image_sha256):
            raise ValueError("prepared media image hash does not match receipt")
        if not receipt.files:
            raise ValueError("prepared media receipt has no guest files")
        return image_bytes

    @staticmethod
    def _validate_run_target(
        run: RunSnapshot,
        request: MediaTransferRequest,
    ) -> None:
        observation = run.observation
        if observation is None:
            raise ValueError("run has no observed target")
        fingerprint = str(observation.machine.get("fingerprint") or "")
        if (
            not fingerprint
            or not secrets.compare_digest(
                fingerprint,
                request.machine_fingerprint,
            )
            or observation.control_epoch != request.control_epoch
        ):
            raise ValueError("media request does not match the run target")

    @staticmethod
    def _validate_preflight(
        target: MediaTargetState,
        *,
        machine_fingerprint: str,
        control_epoch: int,
        media_name: str,
        image_bytes: int,
    ) -> None:
        if not target.supported:
            raise MediaPreflightError(
                target.unsupported_reason
                or f"{target.adapter} does not support exact-byte media"
            )
        if (
            target.machine_fingerprint != machine_fingerprint
            or target.control_epoch != control_epoch
        ):
            raise MediaPreflightError(
                "target identity changed before media mutation"
            )
        if not target.drive_online:
            raise MediaPreflightError("target mass-storage drive is offline")
        if target.drive_busy:
            raise MediaPreflightError("target mass-storage drive is busy")
        if target.connected:
            raise MediaPreflightError(
                "target mass-storage drive is already connected"
            )
        if target.selected_image is not None:
            raise MediaPreflightError(
                "target mass-storage drive already has selected media"
            )
        if any(
            secrets.compare_digest(item.casefold(), media_name.casefold())
            for item in target.images
        ):
            raise MediaPreflightError(
                "target already contains the requested media name"
            )
        if (
            target.storage_free_bytes is not None
            and target.storage_free_bytes < image_bytes
        ):
            raise MediaPreflightError(
                "target has insufficient mass-storage capacity"
            )

    @staticmethod
    def _require_identity(
        target: MediaTargetState,
        transaction: MediaTransaction,
    ) -> None:
        if (
            target.machine_fingerprint != transaction.machine_fingerprint
            or target.control_epoch != transaction.control_epoch
        ):
            raise MediaMutationAmbiguousError(
                "target identity changed during virtual media transaction"
            )

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())
