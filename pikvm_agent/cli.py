"""Command-line entry point for the PiKVM Agent.

Subcommands are added as the runtime grows (Phase 1+): ``daemon`` to run the
FastAPI daemon, ``mcp`` to run the stdio MCP facade, ``smoke-test`` to exercise
the vision pipeline against a still image. For now this is a thin Typer app so
the ``pikvm-agent`` console script resolves after install.
"""

from __future__ import annotations

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


if __name__ == "__main__":  # pragma: no cover
    app()
