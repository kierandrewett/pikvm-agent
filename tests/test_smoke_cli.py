"""Phase 2 acceptance: ``pikvm-agent smoke-test --screenshot sample.png``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pikvm_agent.cli import app
from pikvm_agent.vision.tesseract_ocr import render_text_image, tesseract_available


def _extract_json(output: str) -> dict:
    start, end = output.index("{"), output.rindex("}")
    return json.loads(output[start : end + 1])


@pytest.mark.skipif(not tesseract_available(), reason="tesseract CLI absent")
def test_smoke_test_reports_pipeline_counts(tmp_path) -> None:
    img = tmp_path / "sample.png"
    img.write_bytes(render_text_image("Open the README file\nfind . -name README"))
    out = tmp_path / "output"
    result = CliRunner().invoke(app, ["smoke-test", "--screenshot", str(img), "--out", str(out)])
    assert result.exit_code == 0, result.output
    data = _extract_json(result.output)
    assert {"ocr_lines", "omniparser_elements", "merged_elements", "set_of_marks_path"} <= set(data)
    assert data["ocr_lines"] >= 1
    assert data["omniparser_elements"] == 0  # OmniParser disabled by default
    assert data["merged_elements"] >= data["ocr_lines"]
    assert Path(data["set_of_marks_path"]).exists()
