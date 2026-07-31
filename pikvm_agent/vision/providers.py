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
from pikvm_agent.vision.hybrid_ocr import HybridOcrProvider
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


def _tesseract_provider(config: AppConfig) -> TesseractOcrProvider:
    return TesseractOcrProvider(
        lang="eng" if config.ocr.lang in ("en", "eng") else config.ocr.lang,
        psm=config.ocr.psm,
        upscale=config.ocr.upscale,
        ensemble=config.ocr.ensemble,
        syntax_aware_selection=config.ocr.syntax_aware_selection,
    )


def _with_blind_model_fallback(
    config: AppConfig,
    primary: OCRProvider,
) -> OCRProvider:
    provider_kind = config.ocr.blind_model_provider
    if provider_kind == "none":
        return primary
    if provider_kind != "codex_app_server":
        raise ValueError(
            "ocr.blind_model_provider must be none or codex_app_server"
        )
    if not config.ocr.blind_model:
        raise ValueError(
            "ocr.blind_model is required when blind model OCR is enabled"
        )
    from pikvm_agent.harness.providers import CodexAppServerProvider
    from pikvm_agent.vision.model_ocr import (
        BlindModelOcrProvider,
        PreciseFallbackOcrProvider,
    )

    model = CodexAppServerProvider(
        name="blind-ocr",
        model=config.ocr.blind_model,
        executable=config.ocr.blind_model_executable,
        reasoning_effort=config.ocr.blind_model_reasoning_effort,
        service_tier=config.ocr.blind_model_service_tier,
        timeout_s=config.ocr.blind_model_timeout_s,
    )
    return PreciseFallbackOcrProvider(
        primary,
        BlindModelOcrProvider(
            model,
            minimum_confidence=(
                config.ocr.blind_model_min_confidence
            ),
            samples=config.ocr.blind_model_samples,
        ),
    )


def build_ocr_provider(config: AppConfig, backend: Any) -> OCRProvider:
    provider = config.ocr.provider
    if provider == "hybrid":
        has_tesseract = tesseract_available()
        has_paddle = paddleocr_available()
        if has_tesseract and has_paddle:
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            selected: OCRProvider = HybridOcrProvider(
                _tesseract_provider(config),
                PaddleOCRProvider(
                    lang=config.ocr.lang,
                    device=config.ocr.device,
                ),
                secondary_timeout_s=config.ocr.hybrid_secondary_timeout_s,
            )
        elif has_paddle:
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            log.warning(
                "ocr.provider=hybrid but tesseract is unavailable; "
                "using PaddleOCR alone"
            )
            selected = PaddleOCRProvider(
                lang=config.ocr.lang,
                device=config.ocr.device,
            )
        elif has_tesseract:
            log.warning(
                "ocr.provider=hybrid but PaddleOCR is unavailable; "
                "using Tesseract alone"
            )
            selected = _tesseract_provider(config)
        else:
            log.warning(
                "ocr.provider=hybrid but neither local engine is available; "
                "falling back to live PiKVM OCR"
            )
            selected = PiKVMOcrProvider(backend)
    elif provider == "paddleocr":
        if paddleocr_available():
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            selected = PaddleOCRProvider(
                lang=config.ocr.lang,
                device=config.ocr.device,
            )
        elif tesseract_available():
            log.warning(
                "ocr.provider=paddleocr but the [vision] extra is not "
                "installed; falling back to Tesseract"
            )
            selected = _tesseract_provider(config)
        else:
            log.warning(
                "ocr.provider=paddleocr but the [vision] extra is not "
                "installed; falling back to live PiKVM OCR"
            )
            selected = PiKVMOcrProvider(backend)
    elif provider == "pikvm":
        selected = PiKVMOcrProvider(backend)
    elif tesseract_available():
        selected = _tesseract_provider(config)
    else:
        log.warning(
            "no local OCR engine available; falling back to live PiKVM OCR "
            "(text-only)"
        )
        selected = PiKVMOcrProvider(backend)
    return _with_blind_model_fallback(config, selected)


def build_screen_parser(
    config: AppConfig,
    backend: Any,
    *,
    ocr_provider: OCRProvider | None = None,
) -> CompositeScreenParser:
    return CompositeScreenParser(
        build_element_provider(config),
        ocr_provider or build_ocr_provider(config, backend),
    )
