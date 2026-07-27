"""TesseractOcrProvider — file OCR via the system ``tesseract`` CLI.

A real, zero-Python-dependency OCRProvider: it shells out to tesseract with TSV
output (words + boxes + confidence) and groups words into lines. Works on the
exact saved frame, so OCR boxes are grounded on the frame we parsed. Used as the
default local OCR when the binary is present (PaddleOCR is the optional upgrade).
"""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from PIL import Image, ImageFilter, ImageOps, ImageStat

from pikvm_agent.core.models import OCRCandidate, OCRLine, OCRResult, Region


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _looks_like_control_border(
    word: dict[str, float | int | str],
    *,
    median_height: int,
) -> bool:
    """Identify vertical Windows control edges misread as text glyphs."""
    return (
        str(word["text"]) in {"|", "I", "l"}
        and int(word["x1"]) - int(word["x0"]) <= max(3, median_height // 3)
        and int(word["height"]) >= round(median_height * 1.35)
    )


_TOKEN_CONTEXT_MARKERS = frozenset("/\\_:#?=@")
_SHA256_TEXT = re.compile(r"sha256:[0-9a-f]{32,128}")
_RUN_IDENTIFIER = re.compile(r"run_[a-z0-9_]+")
_UPPER_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]+")


def _join_segment_words(
    words: list[dict[str, float | int | str]],
) -> str:
    """Rejoin OCR word fragments only when pixels and token syntax agree."""

    if not words:
        return ""
    joiners = _segment_joiners(words)
    return str(words[0]["text"]) + "".join(
        joiner + str(word["text"])
        for joiner, word in zip(joiners, words[1:], strict=True)
    )


def _segment_joiners(
    words: list[dict[str, float | int | str]],
) -> list[str]:
    """Return the syntax-aware separator before every word after the first."""

    if len(words) < 2:
        return []
    text = str(words[0]["text"])
    token_markers = {
        marker for marker in _TOKEN_CONTEXT_MARKERS if marker in text
    }
    previous = words[0]
    joiners: list[str] = []
    for word in words[1:]:
        previous_text = str(previous["text"])
        current_text = str(word["text"])
        gap = int(word["x0"]) - int(previous["x1"])
        character_widths = [
            max(
                1.0,
                (int(candidate["x1"]) - int(candidate["x0"]))
                / max(1, len(str(candidate["text"]))),
            )
            for candidate in (previous, word)
        ]
        maximum_fragment_gap = max(
            2,
            round(min(character_widths) * 0.9),
        )
        visually_tight = gap <= maximum_fragment_gap
        both_have_text_glyphs = (
            any(character.isalnum() for character in previous_text)
            and any(character.isalnum() for character in current_text)
        )
        concatenate = (
            visually_tight
            and bool(token_markers)
            and both_have_text_glyphs
            and not (
                token_markers <= {"_"}
                and current_text.isalpha()
            )
        )
        joiners.append("" if concatenate else " ")
        current_markers = {
            marker
            for marker in _TOKEN_CONTEXT_MARKERS
            if marker in current_text
        }
        token_markers = (
            token_markers | current_markers
            if concatenate
            else current_markers
        )
        previous = word
    return joiners


def _spacing_aware_segment(
    words: list[dict[str, float | int | str]],
) -> tuple[str, bool, bool]:
    """Reconstruct anomalous repeated spaces from grounded word geometry.

    OCR TSV discards inter-word whitespace. A line-local baseline avoids
    pretending that proportional-font gaps have a universal pixel width: only
    a materially wider gap than the other spaces on the same line is promoted
    to two or more spaces. Short uncalibrated lines stay conservative.
    """

    if not words:
        return "", False, False
    joiners = _segment_joiners(words)
    gap_entries = [
        (
            index,
            max(1, int(word["x0"]) - int(previous["x1"])),
        )
        for index, (previous, word, joiner) in enumerate(
            zip(
                words[:-1],
                words[1:],
                joiners,
                strict=True,
            )
        )
        if joiner
    ]
    gaps = [
        gap
        for _index, gap in gap_entries
    ]
    character_widths = [
        max(
            1.0,
            (int(word["x1"]) - int(word["x0"]))
            / max(1, len(str(word["text"]))),
        )
        for word in words
        if any(character.isalnum() for character in str(word["text"]))
    ]
    median_character_width = (
        statistics.median(character_widths)
        if character_widths
        else 0.0
    )
    baseline = 0.0
    if len(gaps) >= 3:
        ordered = sorted(gaps)
        baseline_count = max(2, (len(ordered) + 1) // 2)
        baseline_values = ordered[:baseline_count]
        baseline = sum(baseline_values) / len(baseline_values)
    heights = sorted(max(1, int(word["height"])) for word in words)
    median_height = heights[len(heights) // 2]
    text = str(words[0]["text"])
    anomaly = False
    safely_calibrated = bool(
        baseline
        and gaps
        and max(gaps) < baseline * 1.30
    )
    for index, (previous, word, joiner) in enumerate(
        zip(
            words[:-1],
            words[1:],
            joiners,
            strict=True,
        )
    ):
        separator = joiner
        if joiner:
            gap = max(1, int(word["x0"]) - int(previous["x1"]))
            other_gaps = [
                candidate_gap
                for candidate_index, candidate_gap in gap_entries
                if candidate_index != index
            ]
            second_widest = max(other_gaps, default=0)
            strong_line_anomaly = bool(
                baseline
                and (
                    (
                        gap >= baseline * 1.55
                        and gap - baseline
                        >= max(2.0, median_height * 0.16)
                    )
                    or (
                        gap >= baseline * 1.35
                        and gap - baseline >= 2.0
                        and gap - second_widest >= 2
                    )
                )
            )
            short_line_anomaly = bool(
                not baseline
                and median_character_width
                and gap >= median_character_width * 1.61
            )
            if strong_line_anomaly or short_line_anomaly:
                spacing_baseline = (
                    baseline
                    if baseline
                    else median_character_width
                )
                spaces = min(
                    4,
                    max(2, round(gap / max(1.0, spacing_baseline))),
                )
                separator = " " * spaces
                anomaly = True
                safely_calibrated = False
        text += separator + str(word["text"])
    return text, anomaly, safely_calibrated


def _parse_tsv(
    tsv: str,
    *,
    coordinate_scale: float = 1.0,
    coordinate_offset: tuple[int, int] = (0, 0),
) -> list[OCRLine]:
    rows = tsv.splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    idx = {name: i for i, name in enumerate(header)}
    # Tesseract can place several unrelated controls on one logical line. Keep
    # its line grouping, then split at large visual gaps so click grounding
    # gets one compact box per label instead of a box spanning a whole toolbar.
    groups: dict[tuple[int, int, int], list[dict[str, float | int | str]]] = {}
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < len(header):
            continue
        try:
            conf = float(cols[idx["conf"]])
        except (ValueError, KeyError):
            continue
        text = cols[idx["text"]].strip() if "text" in idx else ""
        if conf < 0 or not text:
            continue
        key = (int(cols[idx["block_num"]]), int(cols[idx["par_num"]]), int(cols[idx["line_num"]]))
        raw_left = (
            round(int(cols[idx["left"]]) / coordinate_scale)
            - coordinate_offset[0]
        )
        raw_top = (
            round(int(cols[idx["top"]]) / coordinate_scale)
            - coordinate_offset[1]
        )
        left = max(0, raw_left)
        top = max(0, raw_top)
        width = round(int(cols[idx["width"]]) / coordinate_scale)
        height = round(int(cols[idx["height"]]) / coordinate_scale)
        groups.setdefault(key, []).append(
            {
                "text": text,
                "confidence": conf,
                "x0": left,
                "y0": top,
                "x1": max(left, raw_left + width),
                "y1": max(top, raw_top + height),
                "height": height,
            }
        )
    lines: list[OCRLine] = []
    for words in groups.values():
        words.sort(key=lambda word: int(word["x0"]))
        heights = sorted(max(1, int(word["height"])) for word in words)
        median_height = heights[len(heights) // 2]
        maximum_word_gap = max(12, round(median_height * 1.75))
        segments: list[list[dict[str, float | int | str]]] = []
        for word in words:
            if _looks_like_control_border(word, median_height=median_height):
                if segments and segments[-1]:
                    segments.append([])
                continue
            if (
                segments
                and segments[-1]
                and int(word["x0"]) - int(segments[-1][-1]["x1"])
                > maximum_word_gap
            ):
                segments.append([])
            if not segments:
                segments.append([])
            segments[-1].append(word)
        for segment in segments:
            if not segment:
                continue
            confidences = [float(word["confidence"]) for word in segment]
            (
                spacing_text,
                spacing_anomaly,
                spacing_safe,
            ) = _spacing_aware_segment(segment)
            lines.append(
                OCRLine(
                    text=_join_segment_words(segment),
                    confidence=sum(confidences) / len(confidences) / 100.0,
                    bbox=[
                        min(int(word["x0"]) for word in segment),
                        min(int(word["y0"]) for word in segment),
                        max(int(word["x1"]) for word in segment),
                        max(int(word["y1"]) for word in segment),
                    ],
                    raw={
                        "spacing_text": spacing_text,
                        "spacing_anomaly": spacing_anomaly,
                        "spacing_safe": spacing_safe,
                    },
                )
            )
    return lines


def _spacing_candidate_text(lines: list[OCRLine]) -> str:
    values: list[str] = []
    anomaly = False
    for line in lines:
        raw = line.raw if isinstance(line.raw, dict) else {}
        value = raw.get("spacing_text")
        if not isinstance(value, str) or not value:
            value = line.text
        values.append(value)
        anomaly = anomaly or raw.get("spacing_anomaly") is True
    return "\n".join(values) if anomaly else ""


def _spacing_lines_verified(lines: list[OCRLine]) -> bool:
    """Whether every parsed line has a safely calibrated whitespace baseline."""

    if not lines:
        return False
    return all(
        isinstance(line.raw, dict)
        and line.raw.get("spacing_safe") is True
        for line in lines
    )


def _consensus_spacing_candidate(
    candidates: list[list[OCRLine]],
    *,
    minimum_reads: int,
) -> str:
    """Return an independently repeated spacing anomaly, never a one-read guess."""

    values = [
        value
        for lines in candidates
        if (value := _spacing_candidate_text(lines))
    ]
    if not values:
        return ""
    value, count = Counter(values).most_common(1)[0]
    return value if count >= minimum_reads else ""


def _normalized_candidate_text(lines: list[OCRLine]) -> str:
    return " ".join(" ".join(line.text.split()) for line in lines).strip()


def _candidate_text(lines: list[OCRLine]) -> str:
    return "\n".join(line.text for line in lines)


def _mean_candidate_confidence(lines: list[OCRLine]) -> float:
    if not lines:
        return -1.0
    return sum(line.confidence for line in lines) / len(lines)


def _machine_syntax_penalty(text: str) -> int:
    """Flag impossible URL/UNC spacing before trusting OCR confidence."""

    value = text.strip()
    lowered = value.casefold()
    if lowered.startswith("sha256:"):
        return 1 if _SHA256_TEXT.fullmatch(value) is None else -1
    if value.startswith("run_"):
        return 1 if _RUN_IDENTIFIER.fullmatch(value) is None else -1
    if value.startswith("IDEMPOTENCY_RETRY_"):
        return 1 if _UPPER_IDENTIFIER.fullmatch(value) is None else -1
    if lowered.startswith(("http:", "https:")):
        parsed = urlsplit(value)
        penalty = sum(
            (
                any(character.isspace() for character in value),
                parsed.scheme not in {"http", "https"},
                not parsed.netloc,
                any(character.isspace() for character in parsed.netloc),
            )
        )
        return penalty or -1
    if value.startswith("\\"):
        penalty = sum(
            (
                not value.startswith("\\\\"),
                len(value) <= 2 or value[2].isspace(),
            )
        )
        return penalty or -1
    return 0


def _saturated_column_runs(
    image: Image.Image,
    bbox: list[int],
) -> list[int]:
    """Return widths of coloured blobs inside a flat OCR bounding box."""

    x0, y0, x1, y1 = bbox
    crop = image.crop(
        (
            max(0, x0),
            max(0, y0),
            min(image.width, x1),
            min(image.height, y1),
        )
    ).convert("RGB")
    active: list[bool] = []
    for x in range(crop.width):
        saturated = 0
        for y in range(crop.height):
            red, green, blue = crop.getpixel((x, y))
            if max(red, green, blue) - min(red, green, blue) >= 55:
                saturated += 1
        active.append(saturated >= 2)
    runs: list[int] = []
    current = 0
    for value in active:
        if value:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _looks_like_window_control_dots(
    line: OCRLine,
    *,
    following_line: OCRLine,
    image: Image.Image,
) -> bool:
    """Detect three coloured title-bar controls misread as a short word."""

    bbox = line.bbox
    following_bbox = following_line.bbox
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(not isinstance(value, int) for value in bbox)
        or not isinstance(following_bbox, list)
        or len(following_bbox) != 4
        or any(not isinstance(value, int) for value in following_bbox)
        or len(line.text.strip()) > 6
        or (line.confidence or 0.0) >= 0.8
    ):
        return False
    x0, y0, x1, y1 = bbox
    _, next_y0, _, _ = following_bbox
    if (
        y0 > max(16, round(image.height * 0.15))
        or y1 - y0 > max(18, round(image.height * 0.18))
        or next_y0 < y1 + 3
        or x1 - x0 > max(80, round(image.width * 0.08))
    ):
        return False
    runs = [width for width in _saturated_column_runs(image, bbox) if width >= 2]
    if len(runs) != 3:
        return False
    return max(runs) <= min(runs) * 2


def _remove_window_control_dot_artifacts(
    lines: list[OCRLine],
    *,
    image: Image.Image,
) -> list[OCRLine]:
    """Remove visually proven title-bar dots without relying on OCR text."""

    if len(lines) < 2:
        return lines
    ordered = sorted(
        lines,
        key=lambda line: (
            line.bbox[1]
            if isinstance(line.bbox, list)
            and len(line.bbox) == 4
            and isinstance(line.bbox[1], int)
            else image.height
        ),
    )
    first = ordered[0]
    if _looks_like_window_control_dots(
        first,
        following_line=ordered[1],
        image=image,
    ):
        return [line for line in lines if line is not first]
    return lines


def _inset_contrasting_left_border(
    image: Image.Image,
    *,
    max_inset: int = 4,
) -> tuple[Image.Image, int]:
    """Remove a narrow widget border that Tesseract can join to the first word.

    A plain margin is preserved. We only inset consecutive low-variance columns
    whose colour materially differs from the crop's median background.
    """

    if image.width <= max_inset + 1 or image.height <= 1:
        return image, 0
    background = ImageStat.Stat(image).median[:3]
    inset = 0
    for x in range(min(max_inset, image.width - 1)):
        stats = ImageStat.Stat(image.crop((x, 0, x + 1, image.height)))
        mean = stats.mean[:3]
        stddev = stats.stddev[:3]
        contrasts_with_background = max(
            abs(channel - background[index])
            for index, channel in enumerate(mean)
        ) >= 20
        if max(stddev) > 6 or not contrasts_with_background:
            break
        inset += 1
    if inset == 0:
        return image, 0
    return image.crop((inset, 0, image.width, image.height)), inset


def _choose_ocr_candidate(
    raw_lines: list[OCRLine],
    prepared_lines: list[OCRLine],
    *,
    syntax_aware: bool = True,
) -> list[OCRLine]:
    """Choose between independent raw and enhanced reads without ground truth."""

    raw_text = _normalized_candidate_text(raw_lines)
    prepared_text = _normalized_candidate_text(prepared_lines)
    if raw_text == prepared_text:
        return prepared_lines
    if syntax_aware:
        raw_syntax_penalty = _machine_syntax_penalty(raw_text)
        prepared_syntax_penalty = _machine_syntax_penalty(prepared_text)
        if raw_syntax_penalty != prepared_syntax_penalty:
            return (
                raw_lines
                if raw_syntax_penalty < prepared_syntax_penalty
                else prepared_lines
            )
    for shorter_lines, shorter, longer in (
        (raw_lines, raw_text, prepared_text),
        (prepared_lines, prepared_text, raw_text),
    ):
        if shorter and shorter in longer:
            extra = longer.replace(shorter, "", 1).strip()
            if len(extra) <= 4:
                return shorter_lines
    raw_score = _mean_candidate_confidence(raw_lines)
    prepared_score = _mean_candidate_confidence(prepared_lines)
    if len(raw_lines) < len(prepared_lines):
        raw_score += 0.035
    elif len(prepared_lines) < len(raw_lines):
        prepared_score += 0.035
    return raw_lines if raw_score >= prepared_score else prepared_lines


async def _run_tesseract(
    src: Path,
    *,
    lang: str,
    psm: int,
) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "tesseract",
        str(src),
        "stdout",
        "-l",
        lang,
        "--psm",
        str(psm),
        "tsv",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out


class TesseractOcrProvider:
    def __init__(
        self,
        lang: str = "eng",
        psm: int = 6,
        upscale: float = 2.0,
        ensemble: bool = True,
        syntax_aware_selection: bool = True,
        alternative_upscales: tuple[float, ...] = (),
    ) -> None:
        self.lang = lang
        self.psm = psm
        self.upscale = max(1.0, upscale)
        self.ensemble = ensemble
        self.syntax_aware_selection = syntax_aware_selection
        self.alternative_upscales = tuple(
            scale
            for scale in dict.fromkeys(
                max(1.0, value) for value in alternative_upscales
            )
            if scale != self.upscale
        )

    async def ocr(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        """Run the normal two-read profile used for general screen OCR."""

        return await self._ocr(
            image_path,
            region=region,
            primary_upscale=self.upscale,
            alternative_upscales=self.alternative_upscales,
            preserve_spacing=False,
        )

    async def ocr_precise(
        self,
        image_path: Path,
        region: Region | None = None,
    ) -> OCRResult:
        """Retain one extra independent scale for exact intended-text checks."""

        primary_upscale = max(self.upscale, 4.0)
        alternative_upscales = tuple(
            scale
            for scale in dict.fromkeys(
                (
                    self.upscale,
                    *self.alternative_upscales,
                    1.5,
                )
            )
            if scale != primary_upscale
        )
        return await self._ocr(
            image_path,
            region=region,
            primary_upscale=primary_upscale,
            alternative_upscales=alternative_upscales,
            preserve_spacing=True,
        )

    async def _ocr(
        self,
        image_path: Path,
        *,
        region: Region | None,
        primary_upscale: float,
        alternative_upscales: tuple[float, ...],
        preserve_spacing: bool,
    ) -> OCRResult:
        src = Path(image_path)
        tmp: Path | None = None
        coordinate_offset = (0, 0)
        prepared_images: list[tuple[Path, float]] = []
        analysis_image: Image.Image
        if region is not None:
            img = Image.open(src).convert("RGB")
            x = max(0, int(region.x))
            y = max(0, int(region.y))
            box = (x, y, x + max(1, int(region.width)), y + max(1, int(region.height)))
            crop_img = img.crop(box)
            analysis_image = crop_img.copy()
            crop_img, left_inset = _inset_contrasting_left_border(crop_img)
            padding = 12
            median = tuple(
                round(value)
                for value in ImageStat.Stat(crop_img).median[:3]
            )
            crop_img = ImageOps.expand(
                crop_img,
                border=padding,
                fill=median,
            )
            coordinate_offset = (padding - left_inset, padding)
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            crop_img.save(fd, format="PNG")
            fd.close()
            tmp = Path(fd.name)
            src = tmp
        source_image = Image.open(src).convert("RGB")
        if region is None:
            analysis_image = source_image

        def prepare(scale: float) -> Path:
            image = source_image.convert("L")
            image = ImageOps.autocontrast(image)
            image = image.resize(
                (
                    round(image.width * scale),
                    round(image.height * scale),
                ),
                Image.Resampling.LANCZOS,
            )
            image = image.filter(
                ImageFilter.UnsharpMask(radius=1.0, percent=140, threshold=3)
            )
            fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            image.save(fd, format="PNG")
            fd.close()
            return Path(fd.name)

        scales = [primary_upscale]
        if self.ensemble:
            scales.extend(alternative_upscales)
        for scale in dict.fromkeys(scales):
            if scale > 1.0:
                prepared_images.append((prepare(scale), scale))
        try:
            alternatives: list[OCRCandidate] = []
            spacing_evidence: Literal[
                "not_evaluated",
                "verified",
                "uncertain",
            ] = "not_evaluated"
            if self.ensemble and prepared_images:
                tsv_calls = [
                    _run_tesseract(
                        src,
                        lang=self.lang,
                        psm=self.psm,
                    ),
                    *(
                        _run_tesseract(
                            prepared,
                            lang=self.lang,
                            psm=self.psm,
                        )
                        for prepared, _scale in prepared_images
                    ),
                ]
                outputs = list(await asyncio.gather(*tsv_calls))
                raw_lines = _remove_window_control_dot_artifacts(
                    _parse_tsv(
                        outputs[0].decode("utf-8", "replace"),
                        coordinate_offset=coordinate_offset,
                    ),
                    image=analysis_image,
                )
                prepared_candidates = [
                    (
                        scale,
                        _remove_window_control_dot_artifacts(
                            _parse_tsv(
                                output.decode("utf-8", "replace"),
                                coordinate_scale=scale,
                                coordinate_offset=coordinate_offset,
                            ),
                            image=analysis_image,
                        ),
                    )
                    for output, (_prepared, scale) in zip(
                        outputs[1:],
                        prepared_images,
                        strict=True,
                    )
                ]
                primary_lines = next(
                    (
                        candidate_lines
                        for scale, candidate_lines in prepared_candidates
                        if scale == primary_upscale
                    ),
                    raw_lines,
                )
                lines = _choose_ocr_candidate(
                    raw_lines,
                    primary_lines,
                    syntax_aware=self.syntax_aware_selection,
                )
                selected_text = _candidate_text(lines)
                seen = {selected_text}
                if preserve_spacing:
                    all_line_reads = [
                        raw_lines,
                        *(item[1] for item in prepared_candidates),
                    ]
                    spacing_text = _consensus_spacing_candidate(
                        all_line_reads,
                        minimum_reads=2,
                    )
                    if spacing_text and spacing_text not in seen:
                        seen.add(spacing_text)
                        alternatives.append(
                            OCRCandidate(
                                text=spacing_text,
                                evidence_kind="spacing",
                            )
                        )
                    spacing_evidence = (
                        "verified"
                        if all(
                            _candidate_text(candidate_lines) == selected_text
                            and _spacing_lines_verified(candidate_lines)
                            for candidate_lines in all_line_reads
                        )
                        else "uncertain"
                    )
                for candidate_lines in (
                    raw_lines,
                    *(item[1] for item in prepared_candidates),
                ):
                    candidate_text = _candidate_text(candidate_lines)
                    if candidate_text and candidate_text not in seen:
                        seen.add(candidate_text)
                        alternatives.append(
                            OCRCandidate(
                                text=candidate_text,
                                mean_confidence=_mean_candidate_confidence(
                                    candidate_lines
                                ),
                            )
                        )
            else:
                primary = next(
                    (
                        prepared
                        for prepared, scale in prepared_images
                        if scale == primary_upscale
                    ),
                    None,
                )
                selected = primary or src
                out = await _run_tesseract(
                    selected,
                    lang=self.lang,
                    psm=self.psm,
                )
                lines = _parse_tsv(
                    out.decode("utf-8", "replace"),
                    coordinate_scale=(
                        primary_upscale if primary is not None else 1.0
                    ),
                    coordinate_offset=coordinate_offset,
                )
                lines = _remove_window_control_dot_artifacts(
                    lines,
                    image=analysis_image,
                )
                if preserve_spacing:
                    spacing_text = _spacing_candidate_text(lines)
                    if spacing_text and spacing_text != _candidate_text(lines):
                        alternatives.append(
                            OCRCandidate(
                                text=spacing_text,
                                evidence_kind="spacing",
                            )
                        )
                    spacing_evidence = (
                        "verified"
                        if _spacing_lines_verified(lines)
                        else "uncertain"
                    )
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            for prepared, _scale in prepared_images:
                prepared.unlink(missing_ok=True)
        return OCRResult(
            lines=lines,
            alternatives=alternatives,
            spacing_evidence=spacing_evidence,
        )


def _readable_font(size: int):
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_text_image(text: str, size: tuple[int, int] = (720, 240), font_size: int = 30) -> bytes:
    """Render black-on-white text with a readable TrueType font — for tests/smoke
    without a real screenshot (so tesseract reads it reliably)."""
    from PIL import ImageDraw

    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _readable_font(font_size)
    y = 12
    for line in (text.splitlines() or [text]):
        draw.text((12, y), line, fill=(0, 0, 0), font=font)
        y += font_size + 12
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
