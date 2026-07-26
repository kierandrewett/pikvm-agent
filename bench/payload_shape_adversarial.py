#!/usr/bin/env python3
"""Run the seeded raw-HID payload-shape safety corpus."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pikvm_agent.harness.payload_shape_benchmark import (
    DEFAULT_PAYLOAD_SHAPE_SEED,
    evaluate_payload_shape_cases,
    generate_payload_shape_cases,
)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1_000)
    parser.add_argument("--seed", type=int, default=DEFAULT_PAYLOAD_SHAPE_SEED)
    parser.add_argument("--safe-fraction", type=float, default=0.2)
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()

    report = evaluate_payload_shape_cases(
        generate_payload_shape_cases(
            count=arguments.count,
            seed=arguments.seed,
            safe_fraction=arguments.safe_fraction,
        )
    )
    report["seed"] = arguments.seed
    encoded = (
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    ).encode()
    if arguments.out is None:
        print(encoded.decode(), end="")
    else:
        try:
            _write_new(arguments.out, encoded)
        except FileExistsError:
            parser.error(f"refusing to overwrite existing report: {arguments.out}")


if __name__ == "__main__":
    main()
