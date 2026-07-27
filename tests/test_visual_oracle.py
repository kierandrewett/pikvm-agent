from __future__ import annotations

import io
import pytest
import random

from PIL import Image

from pikvm_agent.harness.visual_oracle import (
    VisualOracleError,
    assemble_pages,
    decode_page,
    encode_pages,
    render_page,
)
from pikvm_agent.harness.live_benchmark import VisualTrialOracle
from pikvm_agent.harness.protocol import OracleSnapshot


@pytest.mark.parametrize("compress", [False, True])
def test_visual_oracle_round_trips_multiple_jpeg_pages(compress: bool) -> None:
    payload = random.Random(104729).randbytes(5376)
    packets = encode_pages(payload, snapshot_id=104729, compress=compress)
    pages = [
        decode_page(render_page(packet, jpeg_quality=82))
        for packet in packets
    ]

    assert len(pages) >= 2
    assert assemble_pages(list(reversed(pages))) == payload


def test_visual_oracle_v3_uses_the_windows_40_byte_header_contract() -> None:
    packet = encode_pages(b"A", snapshot_id=25, compress=False)[0]

    assert packet[:8] == b"PKVMVX3\0"
    assert packet[40:43] == b"AAA"


def test_visual_oracle_corrects_one_persistently_bad_matrix_cell() -> None:
    payload = b"x" * 300
    packets = encode_pages(payload, snapshot_id=25, compress=False)
    damaged = bytearray(packets[0])
    damaged[40 + 190] ^= 0b00000100
    pages = [
        decode_page(render_page(bytes(damaged), jpeg_quality=82)),
        *[
            decode_page(render_page(packet, jpeg_quality=82))
            for packet in packets[1:]
        ],
    ]

    assert assemble_pages(pages) == payload


def test_visual_oracle_fails_closed_when_page_is_missing() -> None:
    payload = b"x" * 5000
    pages = [
        decode_page(render_page(packet))
        for packet in encode_pages(payload, snapshot_id=7, compress=False)
    ]

    with pytest.raises(VisualOracleError, match="missing"):
        assemble_pages(pages[:-1])


def test_visual_oracle_fails_closed_without_matrix() -> None:
    output = io.BytesIO()
    Image.new("RGB", (1280, 800), "white").save(output, "JPEG")

    with pytest.raises(VisualOracleError, match="border not found"):
        decode_page(output.getvalue())


def test_visual_oracle_decodes_a_matrix_scaled_by_the_windows_desktop() -> None:
    payload = b'{"protocol":"pikvm-observer.v1","text":"dpi-scaled exact"}'
    packet = encode_pages(payload, snapshot_id=25, compress=False)[0]
    source = Image.open(io.BytesIO(render_page(packet))).convert("RGB")
    outer = source.crop((280, 200, 1000, 600)).resize(
        (450, 250),
        Image.Resampling.NEAREST,
    )
    screenshot = Image.new("RGB", (1280, 800), (24, 24, 24))
    screenshot.paste(outer, (190, 160))
    output = io.BytesIO()
    screenshot.save(output, "JPEG", quality=90, subsampling=0)

    page = decode_page(output.getvalue())

    assert assemble_pages([page]) == payload


async def test_visual_trial_oracle_pages_only_through_driver_screenshots() -> None:
    payload = (
        b'{"protocol":"pikvm-observer.v1","sequence":9,"text":"exact",'
        b'"events":[],"dangerous_commits":[]}'
    )
    frames = [
        render_page(packet, jpeg_quality=84)
        for packet in encode_pages(payload, snapshot_id=9, compress=False)
    ]

    class Driver:
        def __init__(self) -> None:
            self.index = 0
            self.last_image = None
            self.actions = []

        async def screenshot(self):
            self.last_image = frames[self.index]
            return {"status": "completed"}

        async def burst(self, actions, *, key):
            self.actions.append((actions, key))
            keys = actions[0]["keys"]
            if "F8" in keys:
                self.index += 1
            return {"status": "completed"}

    driver = Driver()
    snapshot = await VisualTrialOracle()._collect(driver, key="oracle")

    assert snapshot.text == "exact"
    assert driver.index == len(frames) - 1
    assert "F12" in driver.actions[-1][0][0]["keys"]


async def test_visual_trial_oracle_recovers_when_one_next_page_input_is_duplicated() -> None:
    payload = (
        b'{"protocol":"pikvm-observer.v1","sequence":9,"text":"'
        + b"x" * 5000
        + b'","events":[],"dangerous_commits":[]}'
    )
    frames = [
        render_page(packet, jpeg_quality=84)
        for packet in encode_pages(payload, snapshot_id=9, compress=False)
    ]

    class Driver:
        def __init__(self) -> None:
            self.index = 0
            self.last_image = None
            self.duplicated_once = False

        async def screenshot(self):
            self.last_image = frames[self.index]
            return {"status": "completed"}

        async def burst(self, actions, *, key):
            keys = actions[0]["keys"]
            if "F8" in keys:
                distance = 2 if not self.duplicated_once else 1
                self.duplicated_once = True
                self.index = min(len(frames) - 1, self.index + distance)
            elif "F7" in keys:
                self.index = max(0, self.index - 1)
            return {"status": "completed"}

    snapshot = await VisualTrialOracle()._collect(
        Driver(),
        key="oracle-duplicate-navigation",
    )

    assert snapshot.text == "x" * 5000


async def test_visual_trial_oracle_republishes_after_corrupt_pixels() -> None:
    class Driver:
        def __init__(self) -> None:
            self.publishes = 0

        async def publish_observer(self, *, include_file, key):
            self.publishes += 1
            return {"status": "completed"}

    oracle = VisualTrialOracle()
    attempts = 0

    async def collect(driver, *, key):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise VisualOracleError("visual oracle checksum does not match")
        return OracleSnapshot(
            protocol="pikvm-observer.v1",
            sequence=2,
            text="exact after retry",
        )

    oracle._collect = collect
    driver = Driver()
    score, snapshot = await oracle.seal(
        driver,
        object(),
        intended="exact after retry",
        key="retry",
    )

    assert driver.publishes == 2
    assert snapshot.text == "exact after retry"
    assert score["exact_match"] is True
