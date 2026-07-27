"""Killable single-process worker for PaddleOCR native inference."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from pikvm_agent.core.models import Region
from pikvm_agent.vision.paddleocr_client import (
    PaddleOCRProvider,
    _WORKER_RESULT_PREFIX,
)


def _handle_request(
    provider: PaddleOCRProvider,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request_id = payload.get("request_id")
    try:
        if not isinstance(request_id, int) or request_id < 1:
            raise ValueError("invalid request id")
        raw_path = payload.get("image_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("invalid image path")
        image_path = Path(raw_path)
        if not image_path.is_absolute() or not image_path.is_file():
            raise ValueError("image is unavailable")
        raw_region = payload.get("region")
        region = (
            Region.model_validate(raw_region)
            if raw_region is not None
            else None
        )
        result = provider._predict_region(image_path, region)
        return {
            "request_id": request_id,
            "ok": True,
            "result": result.model_dump(mode="json"),
        }
    except BaseException as exc:
        return {
            "request_id": request_id,
            "ok": False,
            "error_class": type(exc).__name__,
        }


def serve(
    provider: PaddleOCRProvider,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> None:
    prefix = _WORKER_RESULT_PREFIX.decode("ascii")
    for line in input_stream:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            response = _handle_request(provider, payload)
        except BaseException as exc:
            response = {
                "request_id": None,
                "ok": False,
                "error_class": type(exc).__name__,
            }
        output_stream.write(
            prefix
            + json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en")
    parser.add_argument("--device")
    args = parser.parse_args(argv)
    serve(
        PaddleOCRProvider(lang=args.lang, device=args.device),
        input_stream=sys.stdin,
        output_stream=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
