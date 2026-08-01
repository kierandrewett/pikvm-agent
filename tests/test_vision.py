"""Vision evidence: fingerprint/diff, frame store, OCR, element parsing, merge."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pikvm_agent.core.models import (
    BBox,
    ElementMap,
    OCRCandidate,
    OCRLine,
    OCRResult,
    Region,
    VisualElement,
)
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.vision.frame_diff import (
    FP_MEANINGFUL,
    fingerprint,
    fp_diff,
    fp_meaningful_change,
    grid,
    is_blank,
)
from pikvm_agent.vision.hybrid_ocr import HybridOcrProvider
from pikvm_agent.vision.omniparser_client import (
    NullElementProvider,
    OmniParserClient,
    OmniParserProvider,
    bbox_to_pixels,
    classify_kind,
)
from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider
from pikvm_agent.vision.screen_parser import CompositeScreenParser, bbox_from_ocr, iou
from pikvm_agent.vision.tesseract_ocr import (
    TesseractOcrProvider,
    _choose_ocr_candidate,
    _parse_tsv,
    _readable_font,
    render_text_image,
    tesseract_available,
)

requires_tesseract = pytest.mark.skipif(not tesseract_available(), reason="tesseract CLI absent")


def _jpeg(color, size=(640, 360)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="JPEG", quality=90)
    return b.getvalue()


# ---- fingerprint ---------------------------------------------------------- #

def test_fingerprint_diff_thresholds() -> None:
    a, b, c = _jpeg((30, 60, 90)), _jpeg((30, 60, 90)), _jpeg((220, 40, 10))
    fa, fb, fc = fingerprint(a), fingerprint(b), fingerprint(c)
    assert len(fa) == 256
    assert fp_diff(fa, fb) < 0.01
    assert fp_diff(fa, fc) > FP_MEANINGFUL
    assert fp_diff(None, fa) == 1.0
    assert len(grid(a)) == 96 * 54
    assert is_blank(fingerprint(_jpeg((0, 0, 0)))) is True


def test_fingerprint_detects_a_large_dark_on_dark_panel() -> None:
    before = Image.new("RGB", (1280, 800), (13, 28, 49))
    after = before.copy()
    panel = Image.new("RGB", (480, 450), (60, 60, 60))
    after.paste(panel, (8, 310))

    def encoded(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    before_fp = fingerprint(encoded(before))
    after_fp = fingerprint(encoded(after))

    assert fp_diff(before_fp, after_fp) < FP_MEANINGFUL
    assert fp_meaningful_change(before_fp, after_fp) is True


# ---- frame store world-versioning ----------------------------------------- #

async def test_frame_store_world_versioning(tmp_path) -> None:
    be = FakeBackend()
    fs = FrameStore("sess", tmp_path, be)
    f1 = await fs.capture()
    assert (f1.frame_id, f1.world_version) == (1, 1)
    f2 = await fs.capture()  # unchanged screen
    assert (f2.frame_id, f2.world_version) == (2, 1)
    be.set_screen("modal", bg=(210, 30, 30))  # meaningful change
    assert (await fs.capture()).world_version == 2
    be.caps_lock = True  # keyboard change bumps world too
    assert (await fs.capture()).world_version == 3


async def test_frame_store_invalidates_a_stale_dark_panel(tmp_path) -> None:
    backend = FakeBackend(width=1280, height=800)
    desktop = Image.new("RGB", (1280, 800), (13, 28, 49))
    panel = desktop.copy()
    ImageDraw.Draw(panel).rectangle(
        (8, 310, 488, 760),
        fill=(60, 60, 60),
    )

    def encoded(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
        return output.getvalue()

    backend.set_frame_bytes(encoded(panel))
    frames = FrameStore("dark-panel", tmp_path, backend)
    first = await frames.capture()
    backend.set_frame_bytes(encoded(desktop))
    second = await frames.capture()

    assert first.world_version == 1
    assert second.world_version == 2


# ---- OCR ------------------------------------------------------------------ #

@requires_tesseract
async def test_tesseract_ocr_reads_text_with_boxes(tmp_path) -> None:
    p = tmp_path / "s.png"
    p.write_bytes(render_text_image("Open the README file\nfind . -name README"))
    res = await TesseractOcrProvider().ocr(p)
    assert "readme" in res.text.lower()
    assert all(len(ln.bbox) == 4 for ln in res.lines)
    assert res.lines[0].confidence and res.lines[0].confidence > 0.5


@requires_tesseract
async def test_tesseract_precise_ocr_detects_an_anomalous_double_space(
    tmp_path,
) -> None:
    intended = (
        "this sentence has exactly one doubled space near the middle"
    )
    observed = intended.replace("one doubled", "one  doubled")
    image_path = tmp_path / "spacing.png"
    image_path.write_bytes(
        render_text_image(observed, size=(1200, 140), font_size=36)
    )

    result = await TesseractOcrProvider().ocr_precise(image_path)

    assert result.text == intended
    assert any(
        candidate.evidence_kind == "spacing"
        and candidate.text == observed
        for candidate in result.alternatives
    )


async def test_tesseract_retains_a_secondary_scale_for_exact_intended_text(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 50), "white").save(image_path)
    observed_sizes: list[tuple[int, int]] = []

    async def fake_tesseract(src, *, lang, psm):
        del lang, psm
        size = Image.open(src).size
        observed_sizes.append(size)
        text, confidence = {
            (100, 50): ("raw-read", 70),
            (150, 75): ("secondary-exact", 80),
            (200, 100): ("primary-read", 95),
        }[size]
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            f"5\t1\t1\t1\t1\t1\t1\t1\t80\t20\t{confidence}\t{text}\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    result = await TesseractOcrProvider(
        upscale=2.0,
        alternative_upscales=(1.5,),
    ).ocr(image_path)

    assert sorted(observed_sizes) == [(100, 50), (150, 75), (200, 100)]
    assert result.text == "primary-read"
    assert {candidate.text for candidate in result.alternatives} == {
        "raw-read",
        "secondary-exact",
    }


async def test_tesseract_general_profile_keeps_the_two_read_latency_budget(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 50), "white").save(image_path)
    observed_sizes: list[tuple[int, int]] = []

    async def fake_tesseract(src, *, lang, psm):
        del lang, psm
        size = Image.open(src).size
        observed_sizes.append(size)
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t1\t1\t80\t20\t95\tread\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    await TesseractOcrProvider(upscale=2.0).ocr(image_path)

    assert sorted(observed_sizes) == [(100, 50), (200, 100)]


async def test_tesseract_precise_uses_sparse_text_mode_for_a_slender_field(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (400, 200), "white").save(image_path)
    observed_psms: list[int] = []

    async def fake_tesseract(src, *, lang, psm):
        del src, lang
        observed_psms.append(psm)
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t1\t1\t80\t12\t95\tfield\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    await TesseractOcrProvider(psm=6).ocr_precise(
        image_path,
        region=Region(x=20, y=80, width=240, height=24),
    )

    assert observed_psms
    assert set(observed_psms) == {12}


async def test_tesseract_region_adds_context_and_translates_boxes(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    image = Image.new("RGB", (100, 50), "white")
    ImageDraw.Draw(image).rectangle((10, 5, 11, 14), fill=(165, 165, 165))
    image.save(image_path)
    observed: dict[str, object] = {}

    async def fake_tesseract(src, *, lang, psm):
        del lang, psm
        with Image.open(src) as image:
            observed["size"] = image.size
            observed["border_pixel"] = image.getpixel((0, 0))
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t14\t15\t5\t4\t95\tfield\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    result = await TesseractOcrProvider(
        upscale=1.0,
        ensemble=False,
    ).ocr(
        image_path,
        region=Region(x=10, y=5, width=20, height=10),
    )

    assert observed == {
        "size": (42, 34),
        "border_pixel": (255, 255, 255),
    }
    assert result.text == "field"
    assert result.lines[0].bbox == [4, 3, 9, 7]


async def test_tesseract_precise_profile_can_read_a_small_ui_label_at_four_x(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 50), "white").save(image_path)
    observed_sizes: list[tuple[int, int]] = []

    async def fake_tesseract(src, *, lang, psm):
        del lang, psm
        size = Image.open(src).size
        observed_sizes.append(size)
        text, confidence = {
            (100, 50): ("Styles", 62),
            (150, 75): ("Styles", 63),
            (200, 100): ("Styles", 64),
            (400, 200): ("Title", 91),
        }[size]
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            f"5\t1\t1\t1\t1\t1\t40\t20\t40\t20\t{confidence}\t{text}\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    result = await TesseractOcrProvider(upscale=2.0).ocr_precise(image_path)

    assert result.text == "Title"
    assert (400, 200) in observed_sizes


async def test_tesseract_precise_profile_retains_spacing_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (100, 50), "white").save(image_path)

    async def fake_tesseract(_src, *, lang, psm):
        del lang, psm
        return (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t1\t1\t40\t20\t95\texactly\n"
            "5\t1\t1\t1\t1\t2\t50\t1\t20\t20\t95\tone\n"
            "5\t1\t1\t1\t1\t3\t100\t1\t30\t20\t95\tspace\n"
            "5\t1\t1\t1\t1\t4\t140\t1\t25\t20\t95\tnow\n"
        ).encode()

    monkeypatch.setattr(
        "pikvm_agent.vision.tesseract_ocr._run_tesseract",
        fake_tesseract,
    )

    result = await TesseractOcrProvider(upscale=2.0).ocr_precise(image_path)

    assert result.text == "exactly one space now"
    spacing_candidates = [
        candidate.text
        for candidate in result.alternatives
        if candidate.evidence_kind == "spacing"
    ]
    assert len(spacing_candidates) == 1
    assert "one  " in spacing_candidates[0]


def test_paddleocr_crops_the_requested_region_before_inference(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    image = Image.new("RGB", (120, 80), "white")
    for x in range(20, 50):
        for y in range(30, 50):
            image.putpixel((x, y), (15, 80, 170))
    image.save(image_path)
    provider = PaddleOCRProvider()
    captured: dict[str, object] = {}

    def fake_predict(path: Path) -> OCRResult:
        captured["path"] = path
        with Image.open(path) as crop:
            captured["size"] = crop.size
            captured["pixel"] = crop.getpixel((0, 0))
        return OCRResult(lines=[OCRLine(text="field", confidence=0.99)])

    monkeypatch.setattr(provider, "_predict", fake_predict)

    result = provider._predict_region(
        image_path,
        Region(x=20, y=30, width=30, height=20),
    )

    assert result.text == "field"
    assert captured["size"] == (30, 20)
    assert captured["pixel"] == (15, 80, 170)
    assert not Path(captured["path"]).exists()


def test_paddleocr_removes_region_crop_after_inference_failure(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    captured: dict[str, Path] = {}

    def fail_predict(path: Path) -> OCRResult:
        captured["path"] = path
        assert path.exists()
        raise RuntimeError("synthetic inference failure")

    monkeypatch.setattr(provider, "_predict", fail_predict)

    with pytest.raises(RuntimeError, match="synthetic inference failure"):
        provider._predict_region(
            image_path,
            Region(x=10, y=12, width=30, height=20),
        )

    assert not captured["path"].exists()


async def test_paddleocr_public_ocr_forwards_region_to_worker(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    region = Region(x=10, y=12, width=30, height=20)

    async def fake_worker_request(
        path: Path,
        selected: Region | None,
    ) -> OCRResult:
        assert path == image_path
        assert selected == region
        return OCRResult(lines=[OCRLine(text="field", confidence=0.99)])

    monkeypatch.setattr(provider, "_request_worker", fake_worker_request)

    result = await provider.ocr(image_path, region=region)

    assert result.text == "field"


async def test_paddleocr_timeout_does_not_start_overlapping_inference(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    release = asyncio.Event()
    background: set[asyncio.Task[OCRResult]] = set()
    calls = 0
    active = 0
    max_active = 0

    async def stubborn_worker_request(
        path: Path,
        selected: Region | None,
    ) -> OCRResult:
        nonlocal calls, active, max_active
        assert path == image_path
        assert selected is None

        async def work() -> OCRResult:
            nonlocal calls, active, max_active
            calls += 1
            active += 1
            max_active = max(max_active, active)
            try:
                await release.wait()
                return OCRResult(
                    lines=[OCRLine(text="field", confidence=0.99)]
                )
            finally:
                active -= 1

        task = asyncio.create_task(work())
        background.add(task)
        task.add_done_callback(background.discard)
        return await asyncio.shield(task)

    monkeypatch.setattr(
        provider,
        "_request_worker",
        stubborn_worker_request,
    )

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.ocr(image_path), timeout=0.01)

    release.set()
    await asyncio.sleep(0)

    assert calls == 1
    assert max_active == 1


async def test_paddleocr_close_cancels_and_cleans_active_worker_request(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (80, 60), "white").save(image_path)
    provider = PaddleOCRProvider()
    started = asyncio.Event()
    stopped = 0

    async def blocked_worker_request(
        path: Path,
        selected: Region | None,
    ) -> OCRResult:
        assert path == image_path
        assert selected is None
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def record_stop() -> None:
        nonlocal stopped
        stopped += 1

    monkeypatch.setattr(provider, "_request_worker", blocked_worker_request)
    monkeypatch.setattr(provider, "_stop_worker", record_stop)

    caller = asyncio.create_task(provider.ocr(image_path))
    await started.wait()
    await provider.aclose()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert caller.cancelled()
    assert stopped >= 1


class _ScriptedOcrProvider:
    def __init__(
        self,
        ordinary: OCRResult,
        *,
        precise: OCRResult | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.ordinary = ordinary
        self.precise = precise
        self.failure = failure
        self.calls: list[tuple[str, Path, Region | None]] = []

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        self.calls.append(("ocr", image_path, region))
        if self.failure is not None:
            raise self.failure
        return self.ordinary

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        self.calls.append(("precise", image_path, region))
        if self.failure is not None:
            raise self.failure
        return self.precise or self.ordinary


class _BusyOcrProvider:
    def busy(self) -> bool:
        return True

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        del image_path, region
        raise AssertionError("busy secondary must not be called")


async def test_hybrid_ocr_keeps_general_reads_on_the_fast_primary_path(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="fast primary", confidence=0.81)])
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="slow secondary", confidence=0.99)])
    )

    result = await HybridOcrProvider(primary, secondary).ocr(image_path)

    assert result.text == "fast primary"
    assert primary.calls == [("ocr", image_path, None)]
    assert secondary.calls == []


async def test_hybrid_precise_read_retains_unique_secondary_evidence(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    region = Region(x=1, y=2, width=30, height=12)
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="ordinary")]),
        precise=OCRResult(
            lines=[OCRLine(text="primary selected", confidence=0.91)],
            alternatives=[
                OCRCandidate(text="primary alternate", mean_confidence=0.8),
                OCRCandidate(
                    text="primary  selected",
                    evidence_kind="spacing",
                ),
            ],
            spacing_evidence="verified",
        ),
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(
            lines=[OCRLine(text="secondary exact", confidence=0.97)],
            alternatives=[
                OCRCandidate(text="primary alternate", mean_confidence=0.7),
                OCRCandidate(text="secondary alternate", mean_confidence=0.6),
            ],
        )
    )

    provider = HybridOcrProvider(primary, secondary)
    result = await provider.ocr_precise(
        image_path,
        region=region,
    )

    assert result.text == "primary selected"
    assert [candidate.text for candidate in result.alternatives] == [
        "primary alternate",
        "primary  selected",
        "secondary exact",
        "secondary alternate",
    ]
    assert result.alternatives[1].evidence_kind == "spacing"
    assert result.spacing_evidence == "verified"
    assert primary.calls == [("precise", image_path, region)]
    assert secondary.calls == [("ocr", image_path, region)]
    assert provider.diagnostics() == {
        "warmup_started": 0,
        "warmup_succeeded": 0,
        "warmup_timed_out": 0,
        "precise_waited_for_warmup": 0,
        "precise_calls": 1,
        "secondary_attempted": 1,
        "secondary_completed": 1,
        "secondary_skipped_busy": 0,
        "secondary_skipped_unbounded": 0,
        "secondary_failed_or_timed_out": 0,
        "secondary_timeout_restarts": 0,
        "secondary_timeout_retries": 0,
    }


async def test_hybrid_precise_prefers_a_confident_single_line_secondary() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settingzaboutf",
                confidence=0.80,
                bbox=[4, 2, 72, 12],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settings:about",
                confidence=0.99,
                bbox=[6, 3, 68, 12],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("field.png"))

    assert result.text == "ms-settings:about"
    assert result.lines == secondary.lines
    assert [candidate.text for candidate in result.alternatives] == [
        "ms-settingzaboutf"
    ]


async def test_hybrid_precise_verifies_one_space_from_aligned_engine_consensus() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="This PC",
                confidence=0.80,
                bbox=[4, 8, 35, 24],
            ),
            OCRLine(text="h", confidence=0.42, bbox=[140, 40, 147, 51]),
        ],
        spacing_evidence="uncertain",
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="This PC",
                confidence=0.995,
                bbox=[4, 9, 35, 21],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("explorer-address.png"))

    assert result.text == "This PC"
    assert result.spacing_evidence == "verified"


async def test_hybrid_precise_does_not_verify_repeated_space_consensus() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="This  PC",
                confidence=0.80,
                bbox=[4, 8, 38, 24],
            )
        ],
        spacing_evidence="uncertain",
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="This  PC",
                confidence=0.995,
                bbox=[4, 9, 38, 21],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("explorer-address.png"))

    assert result.text == "This  PC"
    assert result.spacing_evidence == "not_evaluated"


async def test_hybrid_precise_does_not_verify_unaligned_space_consensus() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="This PC",
                confidence=0.80,
                bbox=[140, 40, 171, 56],
            )
        ],
        spacing_evidence="uncertain",
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="This PC",
                confidence=0.995,
                bbox=[4, 9, 35, 21],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("explorer-address.png"))

    assert result.text == "This PC"
    assert result.spacing_evidence == "not_evaluated"


@pytest.mark.parametrize(
    ("rendered_text", "ocr_text", "ocr_confidence", "expected_spacing"),
    [
        ("This PC", "This PC", 0.9316, "verified"),
        ("This PC", "This PC", 0.89, "not_evaluated"),
        ("This  PC", "This PC", 0.995, "not_evaluated"),
        ("ThisPC", "This PC", 0.995, "not_evaluated"),
        ("1. Observe", "1. Observe", 0.995, "verified"),
        ("1.  Observe", "1. Observe", 0.995, "not_evaluated"),
        ("1.Observe", "1. Observe", 0.995, "not_evaluated"),
    ],
)
async def test_hybrid_precise_verifies_one_visible_geometric_gap(
    tmp_path,
    rendered_text: str,
    ocr_text: str,
    ocr_confidence: float,
    expected_spacing: str,
) -> None:
    image_path = tmp_path / "selected-field.png"
    image = Image.new("RGB", (500, 200), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    font = _readable_font(20)
    origin = (110, 90)
    draw.text(origin, rendered_text, font=font, fill=(240, 245, 250))
    box = draw.textbbox(origin, rendered_text, font=font)
    image.save(image_path)
    region = Region(x=100, y=80, width=220, height=50)
    local_box = [
        box[0] - int(region.x),
        box[1] - int(region.y),
        box[2] - int(region.x),
        box[3] - int(region.y),
    ]
    primary = OCRResult(
        lines=[
            OCRLine(
                text="unreadable",
                confidence=0.40,
                bbox=local_box,
            )
        ],
        spacing_evidence="uncertain",
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text=ocr_text,
                confidence=ocr_confidence,
                bbox=local_box,
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(image_path, region=region)

    assert result.text == ocr_text
    assert result.spacing_evidence == expected_spacing


async def test_hybrid_precise_prefers_a_modestly_better_small_field_read() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="task",
                confidence=0.829,
                bbox=[4, 9, 18, 15],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="taskmgr",
                confidence=0.923,
                bbox=[6, 4, 37, 14],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(
        Path("run-field.png"),
        region=Region(x=49, y=678, width=200, height=24),
    )

    assert result.text == "taskmgr"
    assert result.lines == secondary.lines
    assert [candidate.text for candidate in result.alternatives] == ["task"]


async def test_hybrid_precise_keeps_weak_secondary_as_alternate() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="task",
                confidence=0.40,
                bbox=[4, 9, 18, 15],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="taskmgr",
                confidence=0.84,
                bbox=[6, 4, 37, 14],
            )
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(
        Path("run-field.png"),
        region=Region(x=49, y=678, width=200, height=24),
    )

    assert result.text == "task"
    assert [candidate.text for candidate in result.alternatives] == ["taskmgr"]


async def test_hybrid_precise_prefers_a_clearly_better_bounded_dialog_read() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(text="Open: | taskmgd", confidence=0.53),
            OCRLine(
                text="This task will be crested with privileges.",
                confidence=0.76,
            ),
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(text="Open:", confidence=0.96),
            OCRLine(text="taskmgr", confidence=0.95),
            OCRLine(
                text="This task will be created with privileges.",
                confidence=0.99,
            ),
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(
        Path("run-dialog.png"),
        region=Region(x=0, y=592, width=403, height=208),
    )

    assert result.text == secondary.text
    assert result.lines == secondary.lines
    assert [candidate.text for candidate in result.alternatives] == [
        primary.text
    ]


async def test_hybrid_precise_selects_the_aligned_row_from_secondary_noise() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settingsidisplay",
                confidence=0.72,
                bbox=[5, 5, 75, 16],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settings:display",
                confidence=0.999,
                bbox=[6, 6, 74, 16],
            ),
            OCRLine(
                text="This task will be created with administrative privileges.",
                confidence=0.994,
                bbox=[20, 27, 200, 36],
            ),
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("run-field.png"))

    assert result.text == "ms-settings:display"
    assert result.lines == [secondary.lines[0]]
    assert [candidate.text for candidate in result.alternatives] == [
        "ms-settingsidisplay"
    ]


async def test_hybrid_precise_does_not_select_an_unrelated_secondary_row() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settingsidisplay",
                confidence=0.72,
                bbox=[5, 5, 75, 16],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="Completely unrelated text",
                confidence=0.999,
                bbox=[6, 6, 74, 16],
            ),
            OCRLine(
                text="Administrative privileges",
                confidence=0.994,
                bbox=[20, 27, 200, 36],
            ),
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(Path("run-field.png"))

    assert result.text == "ms-settingsidisplay"
    assert result.lines == primary.lines


async def test_hybrid_precise_selects_the_single_central_secondary_row() -> None:
    primary = OCRResult(
        lines=[
            OCRLine(
                text="This tack will be crested with privileges.",
                confidence=0.78,
                bbox=[5, 26, 198, 35],
            )
        ]
    )
    secondary = OCRResult(
        lines=[
            OCRLine(
                text="ms-settings:display",
                confidence=0.989,
                bbox=[6, 9, 74, 20],
            ),
            OCRLine(
                text="This task will be created with administrative privileges.",
                confidence=0.994,
                bbox=[19, 26, 199, 35],
            ),
        ]
    )

    result = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(
        Path("run-field.png"),
        region=Region(x=45, y=675, width=210, height=35),
    )

    assert result.text == "ms-settings:display"
    assert result.lines == [secondary.lines[0]]


async def test_hybrid_warmup_only_starts_the_secondary_worker(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary")])
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="secondary")])
    )
    provider = HybridOcrProvider(primary, secondary)

    assert await provider.warmup(image_path) is True
    assert primary.calls == []
    assert secondary.calls == [("ocr", image_path, None)]


async def test_hybrid_warmup_uses_a_bounded_crop_for_a_real_screen(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (1280, 800), "white").save(image_path)
    primary = _ScriptedOcrProvider(OCRResult())
    secondary = _ScriptedOcrProvider(OCRResult())
    provider = HybridOcrProvider(primary, secondary)

    assert await provider.warmup(image_path) is True

    assert primary.calls == []
    assert len(secondary.calls) == 1
    _kind, _path, region = secondary.calls[0]
    assert region is not None
    assert region.width <= 384
    assert region.height <= 160


async def test_hybrid_warmup_terminates_a_timed_out_worker(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(OCRResult())

    class HungSecondary:
        def __init__(self) -> None:
            self.restarts = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            await asyncio.Event().wait()
            return OCRResult()

        async def restart_after_timeout(self) -> None:
            self.restarts += 1

    secondary = HungSecondary()
    provider = HybridOcrProvider(
        primary,
        secondary,
        warmup_timeout_s=0.01,
    )

    assert await provider.warmup(image_path) is False
    assert secondary.restarts == 1
    assert provider.diagnostics()["warmup_timed_out"] == 1


async def test_hybrid_precise_read_waits_for_in_progress_warmup(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary", confidence=0.70)])
    )

    class WarmingSecondary:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.running = False

        def busy(self) -> bool:
            return self.running

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.calls += 1
            if self.calls == 1:
                self.running = True
                self.started.set()
                await self.release.wait()
                self.running = False
            return OCRResult(
                lines=[OCRLine(text="secondary", confidence=0.99)]
            )

    secondary = WarmingSecondary()
    provider = HybridOcrProvider(
        primary,
        secondary,
        secondary_timeout_s=0.5,
        warmup_timeout_s=1,
    )
    warmup = asyncio.create_task(provider.warmup(image_path))
    await secondary.started.wait()
    precise = asyncio.create_task(provider.ocr_precise(image_path))
    await asyncio.sleep(0)

    assert precise.done() is False

    secondary.release.set()
    assert await warmup is True
    result = await precise

    assert result.text == "secondary"
    assert secondary.calls == 2
    assert provider.diagnostics()["precise_waited_for_warmup"] == 1


async def test_hybrid_precise_read_skips_secondary_for_unbounded_screen(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (1280, 800), "white").save(image_path)
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary screen read")])
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="secondary must not run")])
    )
    provider = HybridOcrProvider(primary, secondary)

    result = await provider.ocr_precise(image_path)

    assert result.text == "primary screen read"
    assert secondary.calls == []
    assert provider.diagnostics()["secondary_skipped_unbounded"] == 1


async def test_hybrid_precise_read_skips_a_busy_secondary(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary without delay")])
    )

    provider = HybridOcrProvider(primary, _BusyOcrProvider())

    result = await provider.ocr_precise(image_path)

    assert result.text == "primary without delay"
    assert provider.diagnostics() == {
        "warmup_started": 0,
        "warmup_succeeded": 0,
        "warmup_timed_out": 0,
        "precise_waited_for_warmup": 0,
        "precise_calls": 1,
        "secondary_attempted": 0,
        "secondary_completed": 0,
        "secondary_skipped_busy": 1,
        "secondary_skipped_unbounded": 0,
        "secondary_failed_or_timed_out": 0,
        "secondary_timeout_restarts": 0,
        "secondary_timeout_retries": 0,
    }


async def _busy_hybrid_spacing_result(
    tmp_path: Path,
    *,
    rendered_text: str,
    ocr_text: str,
    font_size: int,
    confidence: float,
) -> OCRResult:
    image_path = tmp_path / f"spacing-{font_size}.png"
    image = Image.new("RGB", (500, 200), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    font = _readable_font(font_size)
    origin = (110, 90)
    draw.text(origin, rendered_text, font=font, fill=(240, 245, 250))
    box = draw.textbbox(origin, rendered_text, font=font)
    image.save(image_path)
    region = Region(x=100, y=80, width=220, height=50)
    result = OCRResult(
        lines=[
            OCRLine(
                text=ocr_text,
                confidence=confidence,
                bbox=[
                    box[0] - int(region.x),
                    box[1] - int(region.y),
                    box[2] - int(region.x),
                    box[3] - int(region.y),
                ],
            )
        ],
        spacing_evidence="uncertain",
    )
    return await HybridOcrProvider(
        _ScriptedOcrProvider(result, precise=result),
        _BusyOcrProvider(),
    ).ocr_precise(image_path, region=region)


@pytest.mark.parametrize(
    ("rendered_text", "ocr_text", "expected_spacing"),
    [
        ("This PC", "This PC", "verified"),
        ("This  PC", "This PC", "uncertain"),
        ("ThisPC", "This PC", "uncertain"),
        ("1. Observe", "1. Observe", "verified"),
        ("1.  Observe", "1. Observe", "uncertain"),
        ("1.Observe", "1. Observe", "uncertain"),
    ],
)
async def test_hybrid_busy_secondary_retains_bounded_geometric_gap_evidence(
    tmp_path,
    rendered_text: str,
    ocr_text: str,
    expected_spacing: str,
) -> None:
    verified = await _busy_hybrid_spacing_result(
        tmp_path,
        rendered_text=rendered_text,
        ocr_text=ocr_text,
        font_size=20,
        confidence=0.995,
    )

    assert verified.text == ocr_text
    assert verified.spacing_evidence == expected_spacing


@pytest.mark.parametrize(
    ("rendered_text", "expected_spacing"),
    [
        ("2. Act", "verified"),
        ("2.  Act", "uncertain"),
        ("2.Act", "uncertain"),
    ],
)
async def test_numbered_gap_evidence_handles_small_windows_editor_text(
    tmp_path,
    rendered_text: str,
    expected_spacing: str,
) -> None:
    verified = await _busy_hybrid_spacing_result(
        tmp_path,
        rendered_text=rendered_text,
        ocr_text="2. Act",
        font_size=10,
        confidence=0.95,
    )

    assert verified.spacing_evidence == expected_spacing


async def test_hybrid_busy_secondary_attaches_gap_evidence_to_noisy_row(
    tmp_path,
) -> None:
    image_path = tmp_path / "selected-field.png"
    image = Image.new("RGB", (500, 200), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    font = _readable_font(20)
    origin = (110, 90)
    draw.text(origin, "This PC", font=font, fill=(240, 245, 250))
    box = draw.textbbox(origin, "This PC", font=font)
    image.save(image_path)
    region = Region(x=100, y=80, width=220, height=50)
    result = OCRResult(
        lines=[
            OCRLine(text=">", confidence=0.42, bbox=[4, 2, 10, 9]),
            OCRLine(
                text="This PC",
                confidence=0.995,
                bbox=[
                    box[0] - int(region.x),
                    box[1] - int(region.y),
                    box[2] - int(region.x),
                    box[3] - int(region.y),
                ],
            ),
        ],
        spacing_evidence="uncertain",
    )
    primary = _ScriptedOcrProvider(result, precise=result)

    observed = await HybridOcrProvider(
        primary,
        _BusyOcrProvider(),
    ).ocr_precise(image_path, region=region)

    assert observed.spacing_evidence == "uncertain"
    assert [
        candidate.text
        for candidate in observed.alternatives
        if candidate.evidence_kind == "spacing"
    ] == ["This PC"]


async def test_hybrid_merge_attaches_gap_evidence_to_noisy_selected_row(
    tmp_path,
) -> None:
    image_path = tmp_path / "selected-field.png"
    image = Image.new("RGB", (500, 200), (24, 28, 36))
    draw = ImageDraw.Draw(image)
    font = _readable_font(20)
    origin = (110, 90)
    draw.text(origin, "This PC", font=font, fill=(240, 245, 250))
    box = draw.textbbox(origin, "This PC", font=font)
    image.save(image_path)
    region = Region(x=100, y=80, width=220, height=50)
    primary = OCRResult(
        lines=[
            OCRLine(text=">", confidence=0.42, bbox=[4, 2, 10, 9]),
            OCRLine(
                text="This PC",
                confidence=0.995,
                bbox=[
                    box[0] - int(region.x),
                    box[1] - int(region.y),
                    box[2] - int(region.x),
                    box[3] - int(region.y),
                ],
            ),
        ],
        spacing_evidence="uncertain",
    )
    secondary = OCRResult(
        lines=[OCRLine(text="unrelated", confidence=0.75, bbox=[8, 4, 40, 12])]
    )

    observed = await HybridOcrProvider(
        _ScriptedOcrProvider(primary, precise=primary),
        _ScriptedOcrProvider(secondary),
    ).ocr_precise(image_path, region=region)

    assert observed.lines == primary.lines
    assert observed.spacing_evidence == "uncertain"
    assert [
        candidate.text
        for candidate in observed.alternatives
        if candidate.evidence_kind == "spacing"
    ] == ["This PC"]


async def test_hybrid_precise_restarts_and_retries_a_timed_out_worker(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(
            lines=[OCRLine(text="ms-settingz:about", confidence=0.70)]
        )
    )

    class RecoveringSecondary:
        def __init__(self) -> None:
            self.calls = 0
            self.restarts = 0

        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return OCRResult(
                lines=[
                    OCRLine(
                        text="ms-settings:about",
                        confidence=0.99,
                    )
                ]
            )

        async def restart_after_timeout(self) -> None:
            self.restarts += 1

    secondary = RecoveringSecondary()
    provider = HybridOcrProvider(
        primary,
        secondary,
        secondary_timeout_s=0.01,
    )

    result = await provider.ocr_precise(image_path)

    assert result.text == "ms-settings:about"
    assert secondary.calls == 2
    assert secondary.restarts == 1
    assert provider.diagnostics() == {
        "warmup_started": 0,
        "warmup_succeeded": 0,
        "warmup_timed_out": 0,
        "precise_waited_for_warmup": 0,
        "precise_calls": 1,
        "secondary_attempted": 1,
        "secondary_completed": 1,
        "secondary_skipped_busy": 0,
        "secondary_skipped_unbounded": 0,
        "secondary_failed_or_timed_out": 0,
        "secondary_timeout_restarts": 1,
        "secondary_timeout_retries": 1,
    }


async def test_hybrid_precise_read_degrades_to_primary_when_secondary_fails(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary survives")])
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(),
        failure=RuntimeError("synthetic secondary outage"),
    )

    result = await HybridOcrProvider(primary, secondary).ocr_precise(image_path)

    assert result.text == "primary survives"
    assert result.alternatives == []


async def test_hybrid_precise_read_propagates_secondary_cancellation(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary")])
    )
    secondary = _ScriptedOcrProvider(
        OCRResult(),
        failure=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await HybridOcrProvider(primary, secondary).ocr_precise(image_path)


async def test_hybrid_precise_read_bounds_secondary_latency(
    tmp_path,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fixture")
    primary = _ScriptedOcrProvider(
        OCRResult(lines=[OCRLine(text="primary survives")])
    )

    class SlowSecondary:
        async def ocr(
            self,
            image_path: Path,
            region: Region | None = None,
        ) -> OCRResult:
            del image_path, region
            await asyncio.sleep(10)
            return OCRResult(lines=[OCRLine(text="too late")])

    provider = HybridOcrProvider(
        primary,
        SlowSecondary(),
        secondary_timeout_s=0.01,
    )

    result = await asyncio.wait_for(
        provider.ocr_precise(image_path),
        timeout=0.5,
    )

    assert result.text == "primary survives"
    assert result.alternatives == []


def test_tesseract_toolbar_line_is_split_into_compact_grounding_boxes() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t100\t40\t35\t14\t92\tCopy",
            "5\t1\t1\t1\t1\t2\t140\t40\t70\t14\t94\tsnapshot",
            "5\t1\t1\t1\t1\t3\t270\t40\t72\t14\t90\tDANGEROUS",
            "5\t1\t1\t1\t1\t4\t348\t40\t34\t14\t95\tSend",
        ]
    )

    lines = _parse_tsv(tsv)

    assert [line.text for line in lines] == ["Copy snapshot", "DANGEROUS Send"]
    assert lines[0].bbox == [100, 40, 210, 54]
    assert lines[1].bbox == [270, 40, 382, 54]


def test_tesseract_button_borders_split_adjacent_controls() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t160\t499\t29\t9\t93\tCopy",
            "5\t1\t1\t1\t1\t2\t194\t499\t55\t9\t92\tsnapshot",
            "5\t1\t1\t1\t1\t3\t252\t492\t2\t22\t35\t|",
            "5\t1\t1\t1\t1\t4\t260\t499\t29\t9\t94\tCopy",
            "5\t1\t1\t1\t1\t5\t294\t499\t24\t9\t91\tfile",
            "5\t1\t1\t1\t1\t6\t323\t499\t55\t9\t90\tsnapshot",
        ]
    )

    lines = _parse_tsv(tsv)

    assert [line.text for line in lines] == ["Copy snapshot", "Copy file snapshot"]
    assert lines[0].bbox[2] < lines[1].bbox[0]


def test_tesseract_rejoins_machine_tokens_without_merging_prose() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t16\t10\t84\t20\t90\thttps:",
            "5\t1\t1\t1\t1\t2\t108\t10\t280\t20\t90\t//api.sandbox.test/#evi",
            "5\t1\t1\t1\t1\t3\t395\t10\t65\t20\t90\tdence",
            "5\t1\t1\t1\t2\t1\t16\t40\t30\t20\t96\tThe",
            "5\t1\t1\t1\t2\t2\t53\t40\t53\t20\t96\treview",
            "5\t1\t1\t1\t2\t3\t113\t40\t90\t20\t96\tcheckpoint",
            "5\t1\t1\t1\t3\t1\t16\t70\t24\t20\t90\t<>",
            "5\t1\t1\t1\t3\t2\t47\t70\t8\t20\t90\t|",
            "5\t1\t1\t1\t3\t3\t62\t70\t8\t20\t90\t&",
            "5\t1\t1\t1\t4\t1\t14\t100\t317\t20\t92\tOAuthURLParser0348",
            "5\t1\t1\t1\t4\t2\t341\t100\t76\t20\t93\tkeeps",
            "5\t1\t1\t1\t4\t3\t424\t100\t297\t20\t89\tApiID_0348-AUGAU",
            "5\t1\t1\t1\t4\t4\t730\t100\t86\t20\t93\tbeside",
            "5\t1\t1\t1\t4\t5\t825\t100\t177\t20\t92\thTtP2Frame",
        ]
    )

    lines = _parse_tsv(tsv)

    assert [line.text for line in lines] == [
        "https://api.sandbox.test/#evidence",
        "The review checkpoint",
        "<> | &",
        "OAuthURLParser0348 keeps ApiID_0348-AUGAU beside hTtP2Frame",
    ]


def test_tesseract_uses_tight_dot_geometry_without_merging_prose() -> None:
    tsv = "\n".join(
        [
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "5\t1\t1\t1\t1\t1\t200\t10\t340\t20\t90\torg.gnome.settings-daemon.plugins.",
            "5\t1\t1\t1\t1\t2\t532\t10\t50\t20\t90\tpower",
            "5\t1\t1\t1\t2\t1\t20\t40\t90\t20\t96\tSentence.",
            "5\t1\t1\t1\t2\t2\t125\t40\t40\t20\t96\tNext",
        ]
    )

    lines = _parse_tsv(tsv)

    assert [line.text for line in lines] == [
        "org.gnome.settings-daemon.plugins.power",
        "Sentence. Next",
    ]


def test_tesseract_prefers_intact_machine_syntax_over_misleading_confidence() -> None:
    def candidate(text: str, confidence: float) -> list[OCRLine]:
        return [OCRLine(text=text, confidence=confidence, bbox=[0, 0, 500, 20])]

    intact_url = candidate(
        "https://api.sandbox.test/runs/0009?view=screen&attempt=9",
        0.47,
    )
    broken_url = candidate(
        "https://api.sandbox. test/runs/0009?view=screen&attempt=9",
        0.87,
    )
    assert _choose_ocr_candidate(broken_url, intact_url) == intact_url
    assert (
        _choose_ocr_candidate(
            broken_url,
            intact_url,
            syntax_aware=False,
        )
        == broken_url
    )

    intact_query = candidate(
        "https://docs.internal/runs/0040?view=screen&attempt=6",
        0.42,
    )
    broken_query = candidate(
        "https://docs. internal/runs/0040?view=screenkattempt=6",
        0.60,
    )
    assert _choose_ocr_candidate(intact_query, broken_query) == intact_query

    intact_unc = candidate(
        r"\\fileserver\validation\2026\case-0018\result.json",
        0.59,
    )
    broken_unc = candidate(
        r"\\ fileserver\validation\2026\case-0018\result.json",
        0.60,
    )
    assert _choose_ocr_candidate(broken_unc, intact_unc) == intact_unc

    broken_unc_prefix = candidate(
        r"| \fiteserver\validation\2026\case-0647\result.json",
        0.81,
    )
    assert (
        _choose_ocr_candidate(broken_unc_prefix, intact_unc)
        == intact_unc
    )

    intact_digest = candidate(
        "sha256:7dc041e1d1557957ad501d7379f8615c",
        0.41,
    )
    broken_digest = candidate(
        "sha256:7de041e1d155795/7ad501d7379T8615c",
        0.62,
    )
    assert _choose_ocr_candidate(broken_digest, intact_digest) == intact_digest

    intact_identifier = candidate("run_0948_xat9e_frame_000948", 0.51)
    broken_identifier = candidate("run_0948 xat9e_frame_000948", 0.87)
    assert (
        _choose_ocr_candidate(broken_identifier, intact_identifier)
        == intact_identifier
    )


# ---- OmniParser ----------------------------------------------------------- #

def test_omniparser_normalize_classify_bbox() -> None:
    r = OmniParserClient._normalize(
        {"parsed_content_list": [
            {"bbox": [0.1, 0.1, 0.3, 0.2], "content": "Send", "type": "button"},
            {"box": [100, 50, 200, 80], "caption": "close window"},
        ]}
    )
    assert len(r.elements) == 2 and r.elements[0].text == "Send"
    assert classify_kind("button", "", "Send") == "send_button"
    assert classify_kind("button", "Save", "Save") == "button"
    assert classify_kind(None, "close window", None) == "close_button"
    assert classify_kind("textbox", None, None) == "input"
    b = bbox_to_pixels([0.1, 0.1, 0.3, 0.2], 1000, 1000)
    assert (b.x, b.y, b.w, b.h) == (100, 100, 200, 100)


async def test_omniparser_provider_builds_and_degrades(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("hi"))
    result = OmniParserClient._normalize(
        {"parsed_content_list": [{"bbox": [0.1, 0.1, 0.3, 0.2], "content": "Send", "type": "button"}]}
    )

    class Stub:
        async def parse_image(self, _p):
            return result

    em = await OmniParserProvider(Stub()).parse_elements(img, frame_id=7, world_version=3)
    assert len(em.elements) == 1 and em.frame_id == 7 and em.elements[0].kind == "send_button"

    class Boom:
        async def parse_image(self, _p):
            raise RuntimeError("server down")

    assert (await OmniParserProvider(Boom()).parse_elements(img, 1, 1)).elements == []
    assert (await NullElementProvider().parse_elements(img, 1, 1)).elements == []


async def test_omniparser_interactivity_maps_to_button(tmp_path) -> None:
    # OmniParser flags clickable elements; an interactable the keyword classifier
    # left "unknown" becomes a button (so the operator sees it as clickable).
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("x"))
    result = OmniParserClient._normalize(
        {"parsed_content_list": [
            {"bbox": [0.1, 0.1, 0.2, 0.2], "content": "Submit", "type": "text", "interactivity": True},
            {"bbox": [0.5, 0.5, 0.6, 0.6], "content": "just a label", "type": "text", "interactivity": False},
        ]}
    )

    class Stub:
        async def parse_image(self, _p):
            return result

    em = await OmniParserProvider(Stub()).parse_elements(img, 1, 1)
    assert em.elements[0].kind == "button" and em.elements[0].text == "Submit"
    assert em.elements[1].kind == "text"  # non-interactive stays text


# ---- composite parser ----------------------------------------------------- #

def test_bbox_from_ocr_and_iou() -> None:
    assert bbox_from_ocr([10, 20, 110, 40]) == BBox(x=10, y=20, w=100, h=20)
    assert bbox_from_ocr([[10, 20], [110, 20], [110, 40], [10, 40]]) == BBox(x=10, y=20, w=100, h=20)
    assert iou(BBox(x=0, y=0, w=10, h=10), BBox(x=0, y=0, w=10, h=10)) == 1.0
    assert iou(BBox(x=0, y=0, w=10, h=10), BBox(x=100, y=100, w=10, h=10)) == 0.0


@requires_tesseract
async def test_composite_parser_attaches_and_keeps(tmp_path) -> None:
    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("Open the README\nfind . -name README"))

    # OCR-only: positioned text elements + ocr_text
    em = await CompositeScreenParser(NullElementProvider(), TesseractOcrProvider()).parse(img, 5, 2)
    assert "readme" in em.ocr_text.lower()
    assert em.elements and all(e.kind == "text" for e in em.elements)
    assert em.elements[0].source == ["tesseract"]

    # an interactable element overlapping the first line gets the text attached
    first = em.elements[0].bbox

    class StubEP:
        async def parse_elements(self, _p, fid, wv):
            el = VisualElement(
                id="e0", frame_id=fid, world_version=wv,
                bbox=BBox(x=first.x - 4, y=first.y - 4, w=first.w + 8, h=first.h + 8),
                kind="input", source=["omniparser"],
            )
            return ElementMap(frame_id=fid, world_version=wv, elements=[el])

    em2 = await CompositeScreenParser(StubEP(), TesseractOcrProvider()).parse(img, 5, 2)
    inp = next(e for e in em2.elements if e.kind == "input")
    assert inp.text and "open" in inp.text.lower() and "tesseract" in inp.source


async def test_composite_parser_ocr_beats_hallucinated_caption() -> None:
    # Real OCR wins over OmniParser/Florence captions for an interactable element's
    # label (those captions are often hallucinated), and the prior guess is demoted
    # to a `caption` hint. A label OCR can't corroborate is PRESERVED, so click-by-
    # label still works (e.g. box-less PiKVM OCR or an OCR miss). No tesseract needed.
    class StubEP:
        async def parse_elements(self, _p, fid, wv):
            return ElementMap(frame_id=fid, world_version=wv, elements=[
                # bogus Florence caption as text, but a real OCR line overlaps it
                VisualElement(id="e0", frame_id=fid, world_version=wv,
                              bbox=BBox(x=10, y=10, w=80, h=20),
                              kind="button", text="Skype", source=["omniparser"]),
                # an icon with a bogus caption and NO overlapping OCR
                VisualElement(id="e1", frame_id=fid, world_version=wv,
                              bbox=BBox(x=400, y=400, w=20, h=20),
                              kind="button", text="14, September, 2024", source=["omniparser"]),
            ])

    class StubOCR:
        async def ocr(self, _p, region=None):
            return OCRResult(lines=[OCRLine(text="Settings", bbox=[12, 12, 78, 28])])

    em = await CompositeScreenParser(StubEP(), StubOCR()).parse(Path("/x.png"), 1, 1)
    overlapped = next(e for e in em.elements if e.id == "e0")
    assert overlapped.text == "Settings"            # real OCR won
    assert overlapped.caption == "Skype"            # hallucination demoted to a hint
    floating = next(e for e in em.elements if e.id == "e1")
    assert floating.text == "14, September, 2024"   # no OCR overlap -> label preserved


# ---- set-of-marks overlay ------------------------------------------------- #

def test_set_of_marks_overlay(tmp_path) -> None:
    from pikvm_agent.vision.set_of_marks import draw_set_of_marks

    img = tmp_path / "f.png"
    img.write_bytes(render_text_image("hi"))
    with Image.open(img) as im:
        src_size = im.size
    em = ElementMap(
        frame_id=1, world_version=1,
        elements=[
            VisualElement(id="e0", frame_id=1, world_version=1,
                          bbox=BBox(x=10, y=10, w=80, h=30), kind="button", text="OK"),
            VisualElement(id="e1", frame_id=1, world_version=1,
                          bbox=BBox(x=10, y=60, w=120, h=24), kind="text", text="hello"),
        ],
    )
    out = draw_set_of_marks(img, em)
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == src_size  # overlay preserves frame dimensions
    # empty map still yields a valid file at the requested path
    out2 = draw_set_of_marks(img, ElementMap(frame_id=1, world_version=1), tmp_path / "empty.png")
    assert out2.exists() and out2.name == "empty.png"
