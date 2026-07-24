"""Authenticated ground-truth receiver for the disposable test observer."""

from __future__ import annotations

import base64
import hmac
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pikvm_agent.harness.protocol import OracleSnapshot
from pikvm_agent.harness.scoring import AccuracyScore, score_snapshot


class SealTrialRequest(BaseModel):
    intended: str
    ocr_text: str | None = None
    expected_file_base64: str | None = None
    timeout_s: float = 10.0


class ObserverReceiver:
    """Own the latest exact observer report and its temporary artifact endpoint."""

    def __init__(self, *, artifact: Path, token: str) -> None:
        if not token:
            raise ValueError("observer token must not be empty")
        self.artifact = artifact.expanduser().resolve()
        if not self.artifact.is_file():
            raise ValueError(f"observer artifact does not exist: {self.artifact}")
        self._token = token
        self._condition = threading.Condition()
        self._latest: OracleSnapshot | None = None
        self._trials: dict[str, int] = {}
        self._sealed: dict[str, AccuracyScore] = {}
        self.public_app = FastAPI(title="PiKVM accuracy observer receiver")
        self.evaluator_app = FastAPI(title="PiKVM blind-trial evaluator")
        # Compatibility name: this is deliberately the public, write-only
        # observer surface, never the evaluator.
        self.app = self.public_app
        self._mount_routes()

    def _authorized(self, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied, self._token)

    def _require_token(self, supplied: str | None) -> None:
        if not self._authorized(supplied):
            raise HTTPException(status_code=401, detail="invalid observer token")

    def accept(
        self,
        payload: OracleSnapshot | dict[str, Any],
        *,
        token: str,
    ) -> OracleSnapshot:
        self._require_token(token)
        snapshot = (
            payload
            if isinstance(payload, OracleSnapshot)
            else OracleSnapshot.model_validate(payload)
        )
        with self._condition:
            if self._latest is not None and snapshot.sequence <= self._latest.sequence:
                raise HTTPException(
                    status_code=409,
                    detail="snapshot sequence must increase",
                )
            self._latest = snapshot
            self._condition.notify_all()
        return snapshot

    def latest(self) -> OracleSnapshot | None:
        with self._condition:
            return self._latest.model_copy(deep=True) if self._latest else None

    def wait_for_sequence(
        self,
        after_sequence: int,
        *,
        timeout_s: float,
    ) -> OracleSnapshot | None:
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while self._latest is None or self._latest.sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._latest.model_copy(deep=True)

    def wait_for_snapshot(
        self,
        after_sequence: int,
        *,
        predicate: Callable[[OracleSnapshot], bool],
        timeout_s: float,
    ) -> OracleSnapshot | None:
        """Wait for new evidence satisfying ``predicate``, skipping races."""
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                if (
                    self._latest is not None
                    and self._latest.sequence > after_sequence
                    and predicate(self._latest)
                ):
                    return self._latest.model_copy(deep=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def begin_trial(self) -> tuple[str, int]:
        with self._condition:
            after_sequence = self._latest.sequence if self._latest else 0
            trial_id = str(uuid.uuid4())
            self._trials[trial_id] = after_sequence
            return trial_id, after_sequence

    def seal_trial(
        self,
        trial_id: str,
        *,
        intended: str,
        ocr_text: str | None,
        expected_file: bytes | None,
        timeout_s: float,
    ) -> AccuracyScore:
        with self._condition:
            if trial_id in self._sealed:
                return self._sealed[trial_id].model_copy(deep=True)
            after_sequence = self._trials.get(trial_id)
        if after_sequence is None:
            raise HTTPException(status_code=404, detail="unknown trial")
        snapshot = self.wait_for_snapshot(
            after_sequence,
            predicate=(
                (lambda item: item.file is not None)
                if expected_file is not None
                else (lambda _item: True)
            ),
            timeout_s=timeout_s,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=504,
                detail="no post-trial observer snapshot arrived",
            )
        score = score_snapshot(
            intended=intended,
            snapshot=snapshot,
            ocr_text=ocr_text,
            expected_file=expected_file,
        )
        with self._condition:
            self._sealed[trial_id] = score
            self._trials.pop(trial_id, None)
        return score.model_copy(deep=True)

    def _mount_routes(self) -> None:
        @self.public_app.get("/healthz")
        def healthz() -> dict[str, bool]:
            return {"ok": True}

        @self.public_app.get("/observer.exe", response_class=FileResponse)
        def observer(token: str | None = Query(default=None)) -> FileResponse:
            self._require_token(token)
            return FileResponse(
                self.artifact,
                filename="pikvm-accuracy-observer.exe",
                media_type="application/vnd.microsoft.portable-executable",
            )

        @self.public_app.post("/ingest", status_code=202)
        def ingest(
            snapshot: OracleSnapshot,
            x_observer_token: str | None = Header(default=None),
        ) -> dict[str, int | bool]:
            accepted = self.accept(snapshot, token=x_observer_token or "")
            return {"accepted": True, "sequence": accepted.sequence}

        @self.evaluator_app.get("/latest")
        def latest() -> OracleSnapshot:
            snapshot = self.latest()
            if snapshot is None:
                raise HTTPException(status_code=404, detail="no snapshot received")
            return snapshot

        @self.evaluator_app.post("/trials", status_code=201)
        def begin_trial() -> dict[str, str | int]:
            trial_id, after_sequence = self.begin_trial()
            return {"trial_id": trial_id, "after_sequence": after_sequence}

        @self.evaluator_app.post("/trials/{trial_id}/seal")
        def seal_trial(trial_id: str, request: SealTrialRequest) -> AccuracyScore:
            expected_file = None
            if request.expected_file_base64 is not None:
                try:
                    expected_file = base64.b64decode(
                        request.expected_file_base64,
                        validate=True,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail="expected_file_base64 is invalid",
                    ) from exc
            return self.seal_trial(
                trial_id,
                intended=request.intended,
                ocr_text=request.ocr_text,
                expected_file=expected_file,
                timeout_s=request.timeout_s,
            )
