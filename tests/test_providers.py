"""Vision provider selection: config -> the right adapter, with graceful fallback."""

from __future__ import annotations

from pikvm_agent.config import AppConfig, OcrConfig, OmniParserConfig
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.vision.omniparser_client import NullElementProvider, OmniParserProvider
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider
from pikvm_agent.vision.paddleocr_client import paddleocr_available
from pikvm_agent.vision.providers import (
    build_element_provider,
    build_ocr_provider,
    build_screen_parser,
)
from pikvm_agent.vision.screen_parser import CompositeScreenParser
from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider, tesseract_available


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
    # paddleocr requested: use it when the [vision] extra is installed, else fall back.
    selected = build_ocr_provider(AppConfig(ocr=OcrConfig(provider="paddleocr")), be)
    if paddleocr_available():
        assert selected.__class__.__name__ == "PaddleOCRProvider"
    else:
        assert selected.__class__.__name__ != "PaddleOCRProvider"


def test_build_screen_parser_composes() -> None:
    sp = build_screen_parser(AppConfig(), FakeBackend())
    assert isinstance(sp, CompositeScreenParser)
