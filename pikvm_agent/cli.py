"""Command-line entry point for the PiKVM Agent.

Subcommands are added as the runtime grows (Phase 1+): ``daemon`` to run the
FastAPI daemon, ``mcp`` to run the stdio MCP facade, ``smoke-test`` to exercise
the vision pipeline against a still image. For now this is a thin Typer app so
the ``pikvm-agent`` console script resolves after install.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pikvm_agent import __version__

app = typer.Typer(
    name="pikvm-agent",
    help="Transactional computer-use runtime driven through PiKVM.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """PiKVM Agent — keep subcommands named even before more are added."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def daemon(
    host: str = typer.Option("", help="Override listen host (default: config)."),
    port: int = typer.Option(0, help="Override listen port (default: config)."),
) -> None:
    """Run the FastAPI daemon (owns sessions, watchers, execution)."""
    import uvicorn

    from pikvm_agent.config import load_config

    cfg = load_config()
    uvicorn.run(
        "pikvm_agent.daemon:app",
        host=host or cfg.daemon.host,
        port=port or cfg.daemon.port,
        log_level="info",
    )


@app.command()
def mcp() -> None:
    """Run the stdio MCP facade (forwards to the daemon)."""
    from pikvm_agent.mcp_server import main as mcp_main

    mcp_main()


@app.command(name="smoke-test")
def smoke_test(
    screenshot: Path = typer.Option(..., "--screenshot", help="Image to parse."),
    out: Path = typer.Option(Path("output"), "--out", help="Output directory for the overlay."),
) -> None:
    """Run the vision pipeline against a still image and report counts (Phase 2)."""
    import asyncio
    import json

    from pikvm_agent.config import load_config
    from pikvm_agent.vision.paddleocr_client import paddleocr_available
    from pikvm_agent.vision.providers import build_element_provider
    from pikvm_agent.vision.screen_parser import CompositeScreenParser
    from pikvm_agent.vision.set_of_marks import draw_set_of_marks
    from pikvm_agent.vision.tesseract_ocr import TesseractOcrProvider, tesseract_available

    async def run() -> None:
        cfg = load_config()
        element_provider = build_element_provider(cfg)
        # File OCR (boxes). The live PiKVM OCR can't read an arbitrary file.
        if paddleocr_available():
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            ocr = PaddleOCRProvider(lang=cfg.ocr.lang, device=cfg.ocr.device)
        elif tesseract_available():
            ocr = TesseractOcrProvider()
        else:
            typer.echo("No file OCR engine available (install tesseract or the [vision] extra).", err=True)
            raise typer.Exit(code=1)

        ocr_result = await ocr.ocr(screenshot)
        elements = await element_provider.parse_elements(screenshot, 1, 1)
        merged = await CompositeScreenParser(element_provider, ocr).parse(screenshot, 1, 1)

        out.mkdir(parents=True, exist_ok=True)
        marks = draw_set_of_marks(screenshot, merged, out / f"{screenshot.stem}.marks.png")

        typer.echo(json.dumps({
            "ocr_lines": len(ocr_result.lines),
            "omniparser_elements": len(elements.elements),
            "merged_elements": len(merged.elements),
            "set_of_marks_path": str(marks),
        }, indent=2))

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    app()
