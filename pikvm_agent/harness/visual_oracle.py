"""Exact observer snapshots transported through ordinary screen pixels.

The disposable Windows observer paints a deliberately simple black/white bit
matrix.  The harness obtains it only through ``pikvm_screenshot`` and decodes
the bytes locally.  This keeps the controller blind to helper APIs while still
providing an exact oracle for test scoring.
"""

from __future__ import annotations

import binascii
import io
import struct
import zlib
from dataclasses import dataclass

from PIL import Image, ImageDraw

MAGIC = b"PKVMVX3\0"
GRID_COLUMNS = 176
GRID_ROWS = 96
CELL_PIXELS = 4
BORDER_PIXELS = 8
REPEAT_CELLS = 2
DATA_COLUMNS = GRID_COLUMNS // REPEAT_CELLS
DATA_ROWS = GRID_ROWS // REPEAT_CELLS
PACKET_BYTES = DATA_COLUMNS * DATA_ROWS // 8
HEADER = struct.Struct("<8sIHHIIIB11x")
PAYLOAD_BYTES = PACKET_BYTES - HEADER.size
FLAG_ZLIB = 1
FLAG_TRIPLE = 2
TRIPLED_PAYLOAD_BYTES = PAYLOAD_BYTES - (PAYLOAD_BYTES % 3)


class VisualOracleError(ValueError):
    """The screen did not contain a complete, valid exact-oracle packet."""


@dataclass(frozen=True)
class VisualPage:
    snapshot_id: int
    page_index: int
    page_count: int
    total_bytes: int
    crc32: int
    flags: int
    chunk: bytes


def encode_pages(
    payload: bytes,
    *,
    snapshot_id: int,
    compress: bool = True,
) -> list[bytes]:
    """Encode one snapshot into bounded fixed-size screen packets."""
    source = zlib.compress(payload, 9) if compress else payload
    encoded = b"".join(bytes((byte, byte, byte)) for byte in source)
    flags = (FLAG_ZLIB if compress else 0) | FLAG_TRIPLE
    chunks = [
        encoded[offset : offset + TRIPLED_PAYLOAD_BYTES]
        for offset in range(0, len(encoded), TRIPLED_PAYLOAD_BYTES)
    ] or [b""]
    if len(chunks) > 0xFFFF:
        raise VisualOracleError("snapshot exceeds visual page limit")
    checksum = binascii.crc32(payload) & 0xFFFFFFFF
    pages: list[bytes] = []
    for index, chunk in enumerate(chunks):
        header = HEADER.pack(
            MAGIC,
            snapshot_id,
            index,
            len(chunks),
            len(payload),
            len(chunk),
            checksum,
            flags,
        )
        pages.append((header + chunk).ljust(PACKET_BYTES, b"\0"))
    return pages


def render_page(
    packet: bytes,
    *,
    canvas: tuple[int, int] = (1280, 800),
    jpeg_quality: int | None = None,
) -> bytes:
    """Render a packet as the same high-contrast matrix used by the helper."""
    if len(packet) != PACKET_BYTES:
        raise VisualOracleError(f"packet must be exactly {PACKET_BYTES} bytes")
    matrix_width = GRID_COLUMNS * CELL_PIXELS
    matrix_height = GRID_ROWS * CELL_PIXELS
    outer_width = matrix_width + BORDER_PIXELS * 2
    outer_height = matrix_height + BORDER_PIXELS * 2
    left = (canvas[0] - outer_width) // 2
    top = (canvas[1] - outer_height) // 2
    if left < 0 or top < 0:
        raise VisualOracleError("canvas is too small for visual oracle")

    image = Image.new("RGB", canvas, (24, 24, 24))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (left, top, left + outer_width - 1, top + outer_height - 1),
        fill=(255, 0, 255),
    )
    origin_x = left + BORDER_PIXELS
    origin_y = top + BORDER_PIXELS
    draw.rectangle(
        (
            origin_x,
            origin_y,
            origin_x + matrix_width - 1,
            origin_y + matrix_height - 1,
        ),
        fill=(255, 255, 255),
    )
    for bit_index in range(PACKET_BYTES * 8):
        if packet[bit_index // 8] & (1 << (7 - bit_index % 8)):
            column = bit_index % DATA_COLUMNS
            row = bit_index // DATA_COLUMNS
            bit_pixels = CELL_PIXELS * REPEAT_CELLS
            x = origin_x + column * bit_pixels
            y = origin_y + row * bit_pixels
            draw.rectangle(
                (x, y, x + bit_pixels - 1, y + bit_pixels - 1),
                fill=(0, 0, 0),
            )

    output = io.BytesIO()
    if jpeg_quality is None:
        image.save(output, "PNG")
    else:
        image.save(output, "JPEG", quality=jpeg_quality, subsampling=0)
    return output.getvalue()


def _locate_matrix(image: Image.Image) -> tuple[int, int, int, int]:
    pixels = image.load()

    def is_magenta(x: int, y: int) -> bool:
        red, green, blue = pixels[x, y][:3]
        return red >= 180 and blue >= 160 and green <= 100

    matches: list[tuple[int, int]] = []
    for y in range(image.height):
        for x in range(image.width):
            if is_magenta(x, y):
                matches.append((x, y))
    if not matches:
        raise VisualOracleError("visual oracle magenta border not found")
    left = min(point[0] for point in matches)
    top = min(point[1] for point in matches)
    right = max(point[0] for point in matches)
    bottom = max(point[1] for point in matches)
    expected_width = GRID_COLUMNS * CELL_PIXELS + BORDER_PIXELS * 2
    expected_height = GRID_ROWS * CELL_PIXELS + BORDER_PIXELS * 2
    outer_width = right - left + 1
    outer_height = bottom - top + 1
    scale_x = outer_width / expected_width
    scale_y = outer_height / expected_height
    if scale_x < 0.5 or abs(scale_x - scale_y) > 0.05:
        raise VisualOracleError("visual oracle border has unexpected width")
    if scale_y < 0.5:
        raise VisualOracleError("visual oracle border has unexpected height")
    middle_x = (left + right) // 2
    middle_y = (top + bottom) // 2
    left_border = [
        x for x in range(left, middle_x) if is_magenta(x, middle_y)
    ]
    right_border = [
        x for x in range(middle_x + 1, right + 1) if is_magenta(x, middle_y)
    ]
    top_border = [
        y for y in range(top, middle_y) if is_magenta(middle_x, y)
    ]
    bottom_border = [
        y for y in range(middle_y + 1, bottom + 1) if is_magenta(middle_x, y)
    ]
    if not left_border or not right_border:
        raise VisualOracleError("visual oracle horizontal border is incomplete")
    if not top_border or not bottom_border:
        raise VisualOracleError("visual oracle vertical border is incomplete")
    origin_x = max(left_border) + 1
    origin_y = max(top_border) + 1
    matrix_width = min(right_border) - origin_x
    matrix_height = min(bottom_border) - origin_y
    if matrix_width < GRID_COLUMNS * 2:
        raise VisualOracleError("visual oracle matrix is too narrow to decode")
    if matrix_height < GRID_ROWS * 2:
        raise VisualOracleError("visual oracle matrix is too short to decode")
    return origin_x, origin_y, matrix_width, matrix_height


def decode_page(image_bytes: bytes) -> VisualPage:
    """Decode and validate one helper page from a screenshot."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    origin_x, origin_y, matrix_width, matrix_height = _locate_matrix(image)
    grid = image.crop(
        (
            origin_x,
            origin_y,
            origin_x + matrix_width,
            origin_y + matrix_height,
        )
    ).resize((DATA_COLUMNS, DATA_ROWS), Image.Resampling.BOX)
    packet = bytearray(PACKET_BYTES)
    for bit_index, (red, green, blue) in enumerate(grid.get_flattened_data()):
        if red + green + blue < 384:
            packet[bit_index // 8] |= 1 << (7 - bit_index % 8)
    try:
        (
            magic,
            snapshot_id,
            page_index,
            page_count,
            total_bytes,
            chunk_bytes,
            checksum,
            flags,
        ) = HEADER.unpack_from(packet)
    except struct.error as exc:
        raise VisualOracleError("visual oracle header is truncated") from exc
    if magic != MAGIC:
        raise VisualOracleError("visual oracle magic does not match")
    if page_count == 0 or page_index >= page_count:
        raise VisualOracleError("visual oracle page metadata is invalid")
    if chunk_bytes > PAYLOAD_BYTES:
        raise VisualOracleError("visual oracle chunk length is invalid")
    if flags & ~(FLAG_ZLIB | FLAG_TRIPLE):
        raise VisualOracleError("visual oracle flags are unsupported")
    return VisualPage(
        snapshot_id=snapshot_id,
        page_index=page_index,
        page_count=page_count,
        total_bytes=total_bytes,
        crc32=checksum,
        flags=flags,
        chunk=bytes(packet[HEADER.size : HEADER.size + chunk_bytes]),
    )


def assemble_pages(pages: list[VisualPage]) -> bytes:
    """Assemble pages, rejecting stale, mixed, duplicate, or corrupt evidence."""
    if not pages:
        raise VisualOracleError("no visual oracle pages supplied")
    first = pages[0]
    for page in pages:
        if (
            page.snapshot_id != first.snapshot_id
            or page.page_count != first.page_count
            or page.total_bytes != first.total_bytes
            or page.crc32 != first.crc32
            or page.flags != first.flags
        ):
            raise VisualOracleError("visual oracle pages belong to different snapshots")
    by_index = {page.page_index: page for page in pages}
    if len(by_index) != len(pages):
        raise VisualOracleError("visual oracle contains duplicate pages")
    if set(by_index) != set(range(first.page_count)):
        raise VisualOracleError("visual oracle snapshot is missing pages")
    encoded = b"".join(by_index[index].chunk for index in range(first.page_count))
    if first.flags & FLAG_TRIPLE:
        if len(encoded) % 3:
            raise VisualOracleError(
                "visual oracle redundant payload is misaligned"
            )
        encoded = bytes(
            (first_copy & second_copy)
            | (first_copy & third_copy)
            | (second_copy & third_copy)
            for first_copy, second_copy, third_copy in zip(
                encoded[0::3],
                encoded[1::3],
                encoded[2::3],
                strict=True,
            )
        )
    try:
        payload = zlib.decompress(encoded) if first.flags & FLAG_ZLIB else encoded
    except zlib.error as exc:
        raise VisualOracleError("visual oracle compressed payload is corrupt") from exc
    if len(payload) != first.total_bytes:
        raise VisualOracleError("visual oracle payload length does not match")
    if (binascii.crc32(payload) & 0xFFFFFFFF) != first.crc32:
        raise VisualOracleError("visual oracle checksum does not match")
    return payload
