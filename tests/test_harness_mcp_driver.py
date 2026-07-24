from __future__ import annotations

import base64

from mcp.types import CallToolResult, ImageContent, TextContent

from pikvm_agent.harness.mcp_driver import unpack_tool_result


def test_unpack_tool_result_extracts_state_and_inline_image(tmp_path) -> None:
    result = CallToolResult(
        content=[
            ImageContent(type="image", data=base64.b64encode(b"jpeg").decode(), mimeType="image/jpeg"),
            TextContent(type="text", text='{"status":"completed","frame_id":4}'),
        ]
    )
    unpacked = unpack_tool_result(result, tmp_path)

    assert unpacked["state"] == {"status": "completed", "frame_id": 4}
    assert len(unpacked["images"]) == 1
    assert (tmp_path / "tool-image-0.jpg").read_bytes() == b"jpeg"
