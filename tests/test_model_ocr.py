from pathlib import Path

from PIL import Image

from pikvm_agent.core.models import OCRResult, Region
from pikvm_agent.harness.agent_models import ModelResponse
from pikvm_agent.vision.model_ocr import BlindModelOcrProvider


class ScriptedModelProvider:
    name = "blind-test"

    def __init__(self, values: list[dict[str, object]]) -> None:
        self.values = list(values)
        self.requests = []
        self.closed = False

    async def complete(self, request):
        self.requests.append(request)
        value = self.values[len(self.requests) - 1]
        return ModelResponse(
            provider=self.name,
            model="test-vision",
            data=value,
            latency_ms=4,
        )

    async def aclose(self) -> None:
        self.closed = True


def _frame(path: Path) -> None:
    image = Image.new("RGB", (320, 180), (24, 28, 36))
    image.save(path)


async def test_blind_model_ocr_requires_two_matching_transcriptions(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.png"
    _frame(image_path)
    provider = ScriptedModelProvider(
        [
            {
                "text": r"C:\PiKVM-Harness\workspace\codex-50",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": r"C:\PiKVM-Harness\workspace\code-50",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": r"C:\PiKVM-Harness\workspace\codex-50",
                "confidence": 0.98,
                "uncertain_characters": [],
            },
        ]
    )
    ocr = BlindModelOcrProvider(provider)
    region = Region(x=40, y=50, width=180, height=28)

    result = await ocr.ocr_precise(image_path, region=region)

    assert result.text == r"C:\PiKVM-Harness\workspace\codex-50"
    assert result.lines[0].confidence == 0.98
    assert result.lines[0].bbox == [40, 50, 220, 78]
    assert len(provider.requests) == 3
    assert all(request.image_path != str(image_path) for request in provider.requests)
    assert all(
        r"C:\PiKVM-Harness\workspace\codex-50" not in request.prompt
        for request in provider.requests
    )
    assert all(
        request.metadata["image_detail"] == "original"
        for request in provider.requests
    )


async def test_blind_model_ocr_fails_closed_without_consensus(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.png"
    _frame(image_path)
    provider = ScriptedModelProvider(
        [
            {
                "text": "alpha",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": "alpba",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": "alpca",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
        ]
    )
    ocr = BlindModelOcrProvider(provider)

    result = await ocr.ocr_precise(
        image_path,
        region=Region(x=40, y=50, width=180, height=28),
    )

    assert result == OCRResult()


async def test_blind_model_ocr_reuses_same_frame_consensus(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.png"
    _frame(image_path)
    provider = ScriptedModelProvider(
        [
            {
                "text": "exact",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": "exact",
                "confidence": 0.98,
                "uncertain_characters": [],
            },
            {
                "text": "exacd",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
        ]
    )
    ocr = BlindModelOcrProvider(provider)
    region = Region(x=40, y=50, width=180, height=28)

    first = await ocr.ocr_precise(image_path, region=region)
    second = await ocr.ocr_precise(image_path, region=region)

    assert first.text == second.text == "exact"
    assert len(provider.requests) == 2


async def test_blind_model_ocr_stops_after_two_matching_reads(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.png"
    _frame(image_path)
    provider = ScriptedModelProvider(
        [
            {
                "text": "exact",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
            {
                "text": "exact",
                "confidence": 0.98,
                "uncertain_characters": [],
            },
            {
                "text": "unused",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
        ]
    )
    ocr = BlindModelOcrProvider(provider)

    result = await ocr.ocr_precise(
        image_path,
        region=Region(x=40, y=50, width=180, height=28),
    )

    assert result.text == "exact"
    assert len(provider.requests) == 2


async def test_blind_model_ocr_rejects_declared_uncertainty(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screen.png"
    _frame(image_path)
    provider = ScriptedModelProvider(
        [
            {
                "text": "exact",
                "confidence": 0.99,
                "uncertain_characters": [2],
            },
            {
                "text": "exact",
                "confidence": 0.99,
                "uncertain_characters": [2],
            },
            {
                "text": "exact",
                "confidence": 0.99,
                "uncertain_characters": [],
            },
        ]
    )
    ocr = BlindModelOcrProvider(provider)

    result = await ocr.ocr_precise(
        image_path,
        region=Region(x=40, y=50, width=180, height=28),
    )

    assert result == OCRResult()


async def test_blind_model_ocr_closes_model_provider() -> None:
    provider = ScriptedModelProvider([])
    ocr = BlindModelOcrProvider(provider)

    await ocr.aclose()

    assert provider.closed is True
