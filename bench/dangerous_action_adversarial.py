#!/usr/bin/env python3
"""Run the deterministic one-shot action permission benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pikvm_agent.harness.dangerous_action_benchmark import (
    DEFAULT_DANGEROUS_ACTION_SEED,
    evaluate_dangerous_action_cases,
    generate_dangerous_action_cases,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=DEFAULT_DANGEROUS_ACTION_SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    report = evaluate_dangerous_action_cases(
        generate_dangerous_action_cases(
            count=args.cases,
            seed=args.seed,
        )
    )
    report["seed"] = args.seed
    report["gate_passed"] = (
        report["false_negative_count"] == 0
        and report["false_positive_count"] == 0
        and report["category_error_count"] == 0
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key
                not in {
                    "corpus",
                    "false_negative_examples",
                    "false_positive_examples",
                    "category_error_examples",
                }
            },
            indent=2,
        )
    )
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
