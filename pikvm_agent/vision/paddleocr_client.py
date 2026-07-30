"""PaddleOCRProvider — optional local PaddleOCR (the ``[vision]`` extra).

PaddleOCR is imported lazily so the package installs and runs without it. It
produces text, confidence, and boxes; our verifier classifies the result —
PaddleOCR never decides whether typing succeeded.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from pikvm_agent.core.models import OCRLine, OCRResult, Region

_WORKER_RESULT_PREFIX = b"PIKVM_OCR_RESULT "
_WORKER_OUTPUT_LIMIT = 4 * 1024 * 1024


def paddleocr_available() -> bool:
    import importlib.util

    return (
        importlib.util.find_spec("paddleocr") is not None
        and importlib.util.find_spec("paddle") is not None
    )


class PaddleOCRProvider:
    def __init__(self, lang: str = "en", device: str | None = None) -> None:
        # Don't load the (heavy) PaddleOCR model here — defer to the first OCR call so the
        # daemon starts lean. OCR is only used for type read-back verification and the
        # opt-in Layer-2 perception, never the default burst path.
        self._lang = lang
        self._device = device
        self._ocr: Any | None = None
        self._inference_loop: asyncio.AbstractEventLoop | None = None
        self._inference_gate: asyncio.Lock | None = None
        self._inflight: asyncio.Task[OCRResult] | None = None
        self._worker: asyncio.subprocess.Process | None = None
        self._request_id = 0

    def _engine(self) -> Any:
        if self._ocr is None:
            from paddleocr import PaddleOCR  # lazy: only when actually used

            kwargs: dict[str, Any] = {
                "lang": self._lang,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                # paddlepaddle 3.x's PIR executor + the oneDNN CPU backend crash at
                # inference ("ConvertPirAttribute2RuntimeAttribute not support …",
                # onednn_instruction.cc). Disabling oneDNN uses the plain CPU kernels
                # (a little slower, but it actually runs).
                "enable_mkldnn": False,
            }
            if self._device:
                kwargs["device"] = self._device
            self._ocr = PaddleOCR(**kwargs)
        return self._ocr

    def _predict(self, image_path: Path) -> OCRResult:
        output = self._engine().predict(str(image_path))
        lines: list[OCRLine] = []
        for res in output:
            # PaddleOCR 3.x exposes the result as a `.json` DICT property (not a
            # method); older builds had `.json()`/`.to_json()` methods. Handle all.
            raw = getattr(res, "json", None)
            if isinstance(raw, dict):
                data = raw
            elif callable(raw):
                data = raw()
            elif hasattr(res, "to_json") and callable(res.to_json):
                data = res.to_json()
            else:
                data = getattr(res, "res", None) or {}
            raw_res = data.get("res", data) if isinstance(data, dict) else {}
            texts = raw_res.get("rec_texts") or []
            scores = raw_res.get("rec_scores") or []
            boxes = raw_res.get("rec_boxes") or raw_res.get("rec_polys") or []
            for i, text in enumerate(texts):
                box = boxes[i] if i < len(boxes) else None
                if hasattr(box, "tolist"):
                    box = box.tolist()
                lines.append(
                    OCRLine(
                        text=str(text),
                        confidence=float(scores[i]) if i < len(scores) else None,
                        bbox=box,
                    )
                )
        return OCRResult(lines=lines)

    def _predict_region(
        self,
        image_path: Path,
        region: Region | None,
    ) -> OCRResult:
        if region is None:
            return self._predict(image_path)
        x = max(0, int(region.x))
        y = max(0, int(region.y))
        box = (
            x,
            y,
            x + max(1, int(region.width)),
            y + max(1, int(region.height)),
        )
        temporary_path: Path | None = None
        try:
            with Image.open(image_path) as image:
                crop = image.convert("RGB").crop(box)
            temporary = tempfile.NamedTemporaryFile(
                suffix=".png",
                delete=False,
            )
            temporary_path = Path(temporary.name)
            try:
                crop.save(temporary, format="PNG")
            finally:
                temporary.close()
                crop.close()
            return self._predict(temporary_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def ocr(self, image_path: Path, region: Region | None = None) -> OCRResult:
        # PaddleOCR is heavy + synchronous and its native inference cannot be
        # cancelled safely in a Python thread. Keep it in one persistent child
        # process. A caller timeout leaves at most one shielded request warming
        # in that killable worker; later calls wait rather than queue more work.
        loop = asyncio.get_running_loop()
        if self._inference_loop is not loop:
            if (
                self._worker is not None
                and self._worker.returncode is None
            ) or (
                self._inflight is not None
                and not self._inflight.done()
            ):
                raise RuntimeError(
                    "PaddleOCR provider cannot move between event loops "
                    "while its worker is active"
                )
            self._inference_loop = loop
            self._inference_gate = asyncio.Lock()
            self._inflight = None
        assert self._inference_gate is not None
        async with self._inference_gate:
            previous = self._inflight
            if previous is not None and not previous.done():
                try:
                    await asyncio.shield(previous)
                except Exception:
                    # A prior image failure must not poison the provider. The
                    # current image still receives its own bounded attempt.
                    pass
            task = asyncio.create_task(
                self._run_worker_request(Path(image_path), region)
            )
            task.add_done_callback(_consume_task_result)
            self._inflight = task
        return await asyncio.shield(task)

    def busy(self) -> bool:
        """Return whether one native request is still in flight."""

        return self._inflight is not None and not self._inflight.done()

    async def aclose(self) -> None:
        """Cancel the bounded request and terminate the native worker."""

        task = self._inflight
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        await self._stop_worker()

    async def _run_worker_request(
        self,
        image_path: Path,
        region: Region | None,
    ) -> OCRResult:
        try:
            return await self._request_worker(image_path, region)
        except asyncio.CancelledError:
            await self._stop_worker()
            raise
        except Exception:
            await self._stop_worker()
            raise

    async def _request_worker(
        self,
        image_path: Path,
        region: Region | None,
    ) -> OCRResult:
        process = await self._ensure_worker()
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("PaddleOCR worker pipes are unavailable")
        self._request_id += 1
        request_id = self._request_id
        payload = {
            "request_id": request_id,
            "image_path": str(image_path.resolve()),
            "region": region.model_dump(mode="json") if region else None,
        }
        stdin.write(
            (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        )
        await stdin.drain()
        consumed = 0
        while True:
            line = await stdout.readline()
            if not line:
                raise RuntimeError("PaddleOCR worker exited without a result")
            consumed += len(line)
            if consumed > _WORKER_OUTPUT_LIMIT:
                raise RuntimeError("PaddleOCR worker output exceeded its limit")
            if not line.startswith(_WORKER_RESULT_PREFIX):
                continue
            try:
                response = json.loads(
                    line[len(_WORKER_RESULT_PREFIX) :]
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "PaddleOCR worker returned invalid JSON"
                ) from exc
            if response.get("request_id") != request_id:
                continue
            if response.get("ok") is not True:
                raise RuntimeError("PaddleOCR worker inference failed")
            return OCRResult.model_validate(response.get("result"))

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        process = self._worker
        if process is not None and process.returncode is None:
            return process
        argv = [
            sys.executable,
            "-m",
            "pikvm_agent.vision.paddleocr_worker",
            "--lang",
            self._lang,
        ]
        if self._device:
            argv.extend(["--device", self._device])
        self._worker = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=_WORKER_OUTPUT_LIMIT,
        )
        return self._worker

    async def _stop_worker(self) -> None:
        process = self._worker
        self._worker = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                pass

    def __del__(self) -> None:
        process = getattr(self, "_worker", None)
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, RuntimeError):
                pass


def _consume_task_result(task: asyncio.Task[OCRResult]) -> None:
    """Retrieve abandoned inference failures after a caller-side timeout."""

    if task.cancelled():
        return
    task.exception()
