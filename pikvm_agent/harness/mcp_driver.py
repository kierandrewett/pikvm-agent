"""Small stdio MCP client used by the accuracy harness and CI scenarios."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent, TextContent


def unpack_tool_result(result: Any, image_dir: Path | None = None) -> dict[str, Any]:
    texts: list[str] = []
    images: list[str] = []
    if image_dir is not None:
        image_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(result.content):
        if isinstance(item, TextContent):
            texts.append(item.text)
        elif isinstance(item, ImageContent):
            if image_dir is None:
                images.append(f"inline:{item.mimeType}:{len(item.data)}")
            else:
                extension = "png" if item.mimeType == "image/png" else "jpg"
                path = image_dir / f"tool-image-{index}.{extension}"
                path.write_bytes(base64.b64decode(item.data))
                images.append(str(path))
    state: dict[str, Any] | None = None
    for text in reversed(texts):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            state = parsed
            break
    return {
        "is_error": bool(result.isError),
        "state": state,
        "texts": texts,
        "images": images,
    }


async def call_tool(
    *,
    daemon_url: str,
    name: str,
    arguments: dict[str, Any],
    image_dir: Path | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PIKVM_AGENT_DAEMON"] = daemon_url
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pikvm_agent.cli", "mcp"],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    async with stdio_client(server) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments=arguments)
            return unpack_tool_result(result, image_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args-json", default="{}")
    parser.add_argument("--image-dir", type=Path)
    args = parser.parse_args()
    arguments = json.loads(args.args_json)
    if not isinstance(arguments, dict):
        parser.error("--args-json must decode to an object")
    result = asyncio.run(
        call_tool(
            daemon_url=args.daemon,
            name=args.tool,
            arguments=arguments,
            image_dir=args.image_dir,
        )
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
