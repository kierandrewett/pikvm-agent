"""Compatibility entrypoint for the offline 1,000-case blind OCR benchmark.

No network is used and no corpus path is hardcoded:

    .venv/bin/python bench/ocr_corpus_bench.py --out /tmp/pikvm-ocr-blind
"""

from pikvm_agent.harness.ocr_blind_benchmark import main


if __name__ == "__main__":
    raise SystemExit(main())
