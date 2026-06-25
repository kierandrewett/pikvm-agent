"""Factory: build the configured vision providers + composite parser.

Selection honours config, with graceful fallback so the runtime always gets a
working parser:
  * elements: OmniParser when enabled, else Null (OCR-only evidence).
  * OCR for parsing (needs boxes): PaddleOCR if requested + installed, else the
    tesseract CLI if present, else PiKVM live OCR (text-only, last resort).
"""

from __future__ import annotations

import logging
from typing import Any

from pikvm_agent.config import AppConfig
from pikvm_agent.core.ports import OCRProvider, ScreenElementProvider
from pikvm_agent.vision.omniparser_client import (
    NullElementProvider,
    OmniParserClient,
    OmniParserProvider,
)
from pikvm_agent.vision.paddleocr_client import paddleocr_available
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider
from pikvm_agent.vision.screen_parser import CompositeScreenParser
from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider, tesseract_available

log = logging.getLogger("pikvm_agent.vision.providers")


def build_element_provider(config: AppConfig) -> ScreenElementProvider:
    op = config.omniparser
    if op.enabled:
        return OmniParserProvider(
            OmniParserClient(base_url=op.base_url, health_url=op.health_url, timeout_s=op.timeout_s),
            required=op.required,
        )
    return NullElementProvider()


def build_ocr_provider(config: AppConfig, backend: Any) -> OCRProvider:
    provider = config.ocr.provider
    if provider == "paddleocr":
        if paddleocr_available():
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            return PaddleOCRProvider(lang=config.ocr.lang, device=config.ocr.device)
        log.warning("ocr.provider=paddleocr but the [vision] extra is not installed; falling back")
    elif provider == "pikvm":
        return PiKVMOcrProvider(backend)

    if tesseract_available():
        return TesseractOcrProvider(lang="eng" if config.ocr.lang in ("en", "eng") else config.ocr.lang)
    log.warning("no local OCR engine available; falling back to live PiKVM OCR (text-only)")
    return PiKVMOcrProvider(backend)


def build_screen_parser(config: AppConfig, backend: Any) -> CompositeScreenParser:
    return CompositeScreenParser(build_element_provider(config), build_ocr_provider(config, backend))
