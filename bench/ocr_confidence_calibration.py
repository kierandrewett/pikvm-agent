#!/usr/bin/env python3
"""Build a public OCR confidence/risk-coverage artifact."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pikvm_agent.harness.ocr_confidence import confidence_calibration


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    source: dict[str, Any] = json.loads(args.report.read_text())
    calibration = confidence_calibration(source["results"])
    corpus = source.get("corpus") or {
        "seed": source.get("corpus_seed"),
        "evaluation_seed": source.get("evaluation_seed"),
        "cases": source.get("cases", len(source["results"])),
    }
    artifact = {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "source_report": args.report.name,
        "provider": source.get("provider", "tesseract"),
        "corpus": corpus,
        "calibration": calibration,
        "production_decision": {
            "confidence_only_commit_gate": "fail",
            "reason": (
                "No threshold has a 95% Wilson lower accuracy bound of 99%; "
                "high-confidence OCR still requires independent verification."
            ),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n")
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
