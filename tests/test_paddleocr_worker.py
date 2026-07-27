"""PaddleOCR worker protocol stays exact without loading the real model."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from pikvm_agent.core.models import OCRLine, OCRResult, Region
from pikvm_agent.vision.paddleocr_client import (
    PaddleOCRProvider,
    _WORKER_RESULT_PREFIX,
)
from pikvm_agent.vision.paddleocr_worker import _handle_request, serve


def test_worker_forwards_exact_path_and_region(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    selected = Region(x=2, y=3, width=30, height=20)
    captured: list[tuple[Path, Region | None]] = []

    def fake_predict(path: Path, region: Region | None) -> OCRResult:
        captured.append((path, region))
        return OCRResult(
            lines=[OCRLine(text="exact worker text", confidence=0.98)]
        )

    monkeypatch.setattr(provider, "_predict_region", fake_predict)

    response = _handle_request(
        provider,
        {
            "request_id": 7,
            "image_path": str(image_path),
            "region": selected.model_dump(mode="json"),
        },
    )

    assert response["request_id"] == 7
    assert response["ok"] is True
    assert response["result"]["lines"][0]["text"] == "exact worker text"
    assert captured == [(image_path, selected)]


def test_worker_protocol_frames_results_and_redacts_failures(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()

    def fail_predict(path: Path, region: Region | None) -> OCRResult:
        del path, region
        raise RuntimeError("private worker detail")

    monkeypatch.setattr(provider, "_predict_region", fail_predict)
    output = io.StringIO()
    serve(
        provider,
        input_stream=io.StringIO(
            json.dumps(
                {
                    "request_id": 11,
                    "image_path": str(image_path),
                    "region": None,
                }
            )
            + "\n"
        ),
        output_stream=output,
    )

    line = output.getvalue()
    assert line.startswith(_WORKER_RESULT_PREFIX.decode("ascii"))
    assert "private worker detail" not in line
    response = json.loads(
        line.removeprefix(_WORKER_RESULT_PREFIX.decode("ascii"))
    )
    assert response == {
        "request_id": 11,
        "ok": False,
        "error_class": "RuntimeError",
    }


async def test_provider_timeout_returns_then_close_kills_native_worker(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys,time; sys.stdin.readline(); time.sleep(60)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def fake_ensure_worker() -> asyncio.subprocess.Process:
        provider._worker = process
        return process

    monkeypatch.setattr(provider, "_ensure_worker", fake_ensure_worker)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(provider.ocr(image_path), timeout=0.05)
    assert process.returncode is None

    await provider.aclose()
    await asyncio.wait_for(process.wait(), timeout=1.0)

    assert process.returncode is not None
