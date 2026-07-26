"""Compare Tesseract configurations on one already-rendered blind corpus."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import defaultdict
from pathlib import Path

from pikvm_agent.harness.ocr_blind_benchmark import (
    BlindOcrInput,
    OcrBlindReport,
    OcrCaseSpec,
    build_ocr_report,
    observe_blind_input,
    score_ocr_case,
)
from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider


def select_balanced(
    report: OcrBlindReport,
    per_category: int | None,
) -> list:
    if per_category is None:
        return list(report.results)
    selected = []
    counts: defaultdict[str, int] = defaultdict(int)
    for result in report.results:
        if counts[result.category] >= per_category:
            continue
        counts[result.category] += 1
        selected.append(result)
    return selected


async def run(args: argparse.Namespace) -> OcrBlindReport:
    source = OcrBlindReport.model_validate_json(
        args.source_report.read_text(encoding="utf-8")
    )
    source_dir = args.source_report.parent
    selected = select_balanced(source, args.per_category)
    inputs = [
        BlindOcrInput(
            case_id=result.case_id,
            image_path=(
                source_dir
                / "images"
                / f"{result.case_id}."
                f"{'jpg' if result.render.image_format == 'JPEG' else 'png'}"
            ),
        )
        for result in selected
    ]
    random.Random(source.evaluation_seed).shuffle(inputs)
    provider = TesseractOcrProvider(
        psm=args.psm,
        upscale=args.upscale,
        ensemble=not args.no_ensemble,
        syntax_aware_selection=not args.no_syntax_selection,
    )
    semaphore = asyncio.Semaphore(args.jobs)

    async def observe(item: BlindOcrInput):
        async with semaphore:
            return await observe_blind_input(provider, item)

    started = time.monotonic()
    observations = await asyncio.gather(*(observe(item) for item in inputs))
    wall_ms = round((time.monotonic() - started) * 1_000)

    # Ground truth is reconstructed only after every provider call completed.
    cases = {
        result.case_id: OcrCaseSpec(
            case_id=result.case_id,
            category=result.category,
            expected=result.expected,
            render=result.render,
        )
        for result in selected
    }
    results = [
        score_ocr_case(cases[observation.case_id], observation)
        for observation in observations
    ]
    return build_ocr_report(
        provider_name=(
            f"tesseract-psm{args.psm}-upscale{args.upscale:g}-"
            f"{'single' if args.no_ensemble else 'ensemble'}"
        ),
        corpus_seed=source.corpus_seed,
        evaluation_seed=source.evaluation_seed,
        evaluation_wall_ms=wall_ms,
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--psm", type=int, required=True)
    parser.add_argument("--upscale", type=float, default=2.0)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--per-category", type=int)
    parser.add_argument("--no-ensemble", action="store_true")
    parser.add_argument("--no-syntax-selection", action="store_true")
    parser.add_argument("--minimum-normalized-exact", type=float)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": report.provider,
                "cases": report.cases,
                "normalized_exact_rate": report.normalized_exact_rate,
                "expected_aware_normalized_exact_rate": (
                    report.expected_aware_normalized_exact_rate
                ),
                "mean_character_error_rate": report.mean_character_error_rate,
                "median_latency_ms": report.median_latency_ms,
                "p95_latency_ms": report.p95_latency_ms,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "by_category": {
                    category: metrics.normalized_exact_rate
                    for category, metrics in report.by_category.items()
                },
                "report": str(args.out.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    if (
        args.minimum_normalized_exact is not None
        and report.normalized_exact_rate < args.minimum_normalized_exact
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
