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


if __name__ == "__main__":  # pragma: no cover
    app()
