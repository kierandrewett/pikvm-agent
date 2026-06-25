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


_CONFIG_TEMPLATE = """\
# PiKVM Agent config (XDG). Secrets are NOT stored here — set them as env vars
# (the desktop app forwards them to the daemon automatically).

daemon:
  listen: "127.0.0.1:8765"

pikvm:
  base_url: "https://pikvm.local"   # <-- your PiKVM address
  verify_tls: false
  username_env: "PIKVM_USER"        # export PIKVM_USER=...
  password_env: "PIKVM_PASSWORD"    # export PIKVM_PASSWORD=...
  layout: "uk"

# OmniParser is the primary perception (grounded clickable elements). Required:
# the runtime will NOT silently fall back to OCR-only when it is down.
omniparser:
  enabled: true
  required: true
  mode: "managed_child_process"
  base_url: "http://127.0.0.1:8000"
  health_url: "http://127.0.0.1:8000/probe"
  timeout_s: 60
  command:
    - "/home/kieran/dev/OmniParser/.venv/bin/python"
    - "-m"
    - "omniparserserver"
    - "--port"
    - "8000"
  cwd: "/home/kieran/dev/OmniParser/omnitool/omniparserserver"

# OCR is for text read-back/verification (complements OmniParser's elements).
# PaddleOCR preferred; tesseract is the last-resort fallback.
ocr:
  provider: "paddleocr"
  lang: "en"
  device: "cpu"

operator:
  provider: "openrouter"            # set OPENROUTER_API_KEY in the environment
  api_key_env: "OPENROUTER_API_KEY"
  lanes:
    cheap: { model: "qwen/qwen3-vl-8b-instruct" }
    default: { model: "qwen/qwen3-vl-32b-instruct" }
    hard: { model: "qwen/qwen3-vl-235b-a22b-thinking" }
"""


_ENV_TEMPLATE = """\
# Secrets for the PiKVM Agent daemon. Loaded automatically from this folder.
# (The desktop app forwards these at spawn time; this file is for standalone runs.)
PIKVM_USER=
PIKVM_PASSWORD=
# Or a PiKVM session cookie instead of user/pass:
# PIKVM_TOKEN=
# Override the PiKVM address without editing config.yaml:
# PIKVM_BASE_URL=https://pikvm.local
OPENROUTER_API_KEY=
"""


@app.command("config-path")
def config_path() -> None:
    """Show where the config + .env are read from (XDG by default)."""
    from pikvm_agent.config import DEFAULT_CONFIG_PATH, _find_config_file

    active = _find_config_file(None)
    typer.echo(f"active config: {active or '(built-in defaults — no file found)'}")
    typer.echo(f"default config: {DEFAULT_CONFIG_PATH}")
    typer.echo(f".env:           {DEFAULT_CONFIG_PATH.parent / '.env'}")


@app.command("config-init")
def config_init(force: bool = typer.Option(False, "--force", help="Overwrite if present.")) -> None:
    """Scaffold the XDG config + .env (~/.config/pikvm-agent/)."""
    from pikvm_agent.config import DEFAULT_CONFIG_PATH

    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env_path = DEFAULT_CONFIG_PATH.parent / ".env"
    for path, body in ((DEFAULT_CONFIG_PATH, _CONFIG_TEMPLATE), (env_path, _ENV_TEMPLATE)):
        if path.exists() and not force:
            typer.echo(f"exists: {path}  (use --force to overwrite)")
            continue
        path.write_text(body)
        if path is env_path:
            path.chmod(0o600)  # secrets file — owner-only
        typer.echo(f"wrote {path}")


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
