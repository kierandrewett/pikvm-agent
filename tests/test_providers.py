"""Vision provider selection: config -> the right adapter, with graceful fallback."""

from __future__ import annotations

import importlib.util

from pikvm_agent.config import AppConfig, OcrConfig, OmniParserConfig
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.vision.omniparser_client import NullElementProvider, OmniParserProvider
from pikvm_agent.vision.hybrid_ocr import HybridOcrProvider
from pikvm_agent.vision.model_ocr import (
    BlindModelOcrProvider,
    PreciseFallbackOcrProvider,
)
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider
from pikvm_agent.vision.paddleocr_client import paddleocr_available
from pikvm_agent.vision.providers import (
    build_element_provider,
    build_ocr_provider,
    build_screen_parser,
)
from pikvm_agent.vision.screen_parser import CompositeScreenParser
from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider, tesseract_available


def test_paddleocr_availability_requires_its_inference_runtime(
    monkeypatch,
) -> None:
    modules: dict[str, object | None] = {
        "paddleocr": object(),
        "paddle": None,
    }
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: modules.get(name),
    )

    assert paddleocr_available() is False

    modules["paddle"] = object()
    assert paddleocr_available() is True


def test_element_provider_selection() -> None:
    assert isinstance(build_element_provider(AppConfig()), NullElementProvider)
    enabled = AppConfig(omniparser=OmniParserConfig(enabled=True))
    assert isinstance(build_element_provider(enabled), OmniParserProvider)


def test_ocr_provider_selection_and_fallback() -> None:
    be = FakeBackend()
    assert isinstance(build_ocr_provider(AppConfig(ocr=OcrConfig(provider="pikvm")), be), PiKVMOcrProvider)
    if tesseract_available():
        prov = build_ocr_provider(AppConfig(ocr=OcrConfig(provider="tesseract")), be)
        assert isinstance(prov, TesseractOcrProvider)
        assert prov.psm == 6
        assert prov.upscale == 2.0
        assert prov.ensemble is True
        assert prov.syntax_aware_selection is True
    # paddleocr requested: use it when the [vision] extra is installed, else fall back.
    selected = build_ocr_provider(AppConfig(ocr=OcrConfig(provider="paddleocr")), be)
    if paddleocr_available():
        assert selected.__class__.__name__ == "PaddleOCRProvider"
    else:
        assert selected.__class__.__name__ != "PaddleOCRProvider"


def test_tesseract_runtime_profile_is_configurable() -> None:
    if not tesseract_available():
        return
    config = AppConfig(
        ocr=OcrConfig(
            provider="tesseract",
            psm=11,
            upscale=1.5,
            ensemble=False,
            syntax_aware_selection=False,
        )
    )

    provider = build_ocr_provider(config, FakeBackend())

    assert isinstance(provider, TesseractOcrProvider)
    assert provider.psm == 11
    assert provider.upscale == 1.5
    assert provider.ensemble is False
    assert provider.syntax_aware_selection is False


def test_hybrid_provider_uses_fast_tesseract_and_paddle_evidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.tesseract_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.paddleocr_available",
        lambda: True,
    )

    provider = build_ocr_provider(
        AppConfig(ocr=OcrConfig(provider="hybrid")),
        FakeBackend(),
    )

    assert isinstance(provider, HybridOcrProvider)
    assert isinstance(provider.primary, TesseractOcrProvider)
    assert provider.secondary.__class__.__name__ == "PaddleOCRProvider"
    assert provider.secondary_timeout_s == 5.0


def test_hybrid_provider_falls_back_when_only_one_engine_is_available(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.tesseract_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.paddleocr_available",
        lambda: True,
    )

    provider = build_ocr_provider(
        AppConfig(ocr=OcrConfig(provider="hybrid")),
        FakeBackend(),
    )

    assert provider.__class__.__name__ == "PaddleOCRProvider"


def test_hybrid_secondary_timeout_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.tesseract_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.paddleocr_available",
        lambda: True,
    )

    provider = build_ocr_provider(
        AppConfig(
            ocr=OcrConfig(
                provider="hybrid",
                hybrid_secondary_timeout_s=1.25,
            )
        ),
        FakeBackend(),
    )

    assert isinstance(provider, HybridOcrProvider)
    assert provider.secondary_timeout_s == 1.25


def test_blind_model_fallback_is_explicit_and_model_configured(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "pikvm_agent.vision.providers.tesseract_available",
        lambda: True,
    )

    provider = build_ocr_provider(
        AppConfig(
            ocr=OcrConfig(
                provider="tesseract",
                blind_model_provider="codex_app_server",
                blind_model="test-vision-model",
                blind_model_reasoning_effort="minimal",
                blind_model_service_tier="priority",
                blind_model_timeout_s=12,
                blind_model_min_confidence=0.98,
                blind_model_samples=3,
            )
        ),
        FakeBackend(),
    )

    assert isinstance(provider, PreciseFallbackOcrProvider)
    assert isinstance(provider.primary, TesseractOcrProvider)
    assert isinstance(provider.fallback, BlindModelOcrProvider)
    assert provider.fallback.provider.model == "test-vision-model"
    assert provider.fallback.provider.reasoning_effort == "minimal"
    assert provider.fallback.provider.service_tier == "priority"
    assert provider.fallback.provider.timeout_s == 12
    assert provider.fallback.minimum_confidence == 0.98
    assert provider.fallback.samples == 3


def test_build_screen_parser_composes() -> None:
    sp = build_screen_parser(AppConfig(), FakeBackend())
    assert isinstance(sp, CompositeScreenParser)


def test_build_screen_parser_reuses_supplied_ocr_provider() -> None:
    selected = PiKVMOcrProvider(FakeBackend())

    parser = build_screen_parser(
        AppConfig(),
        FakeBackend(),
        ocr_provider=selected,
    )

    assert parser.ocr is selected
