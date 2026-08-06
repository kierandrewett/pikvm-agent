"""Command-line entry point for the PiKVM Agent.

Subcommands are added as the runtime grows (Phase 1+): ``daemon`` to run the
FastAPI daemon, ``mcp`` to run the stdio MCP facade, ``smoke-test`` to exercise
the vision pipeline against a still image. For now this is a thin Typer app so
the ``pikvm-agent`` console script resolves after install.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from pikvm_agent import __version__

app = typer.Typer(
    name="pikvm-agent",
    help="Transactional computer-use runtime driven through PiKVM.",
    no_args_is_help=True,
)

lab_app = typer.Typer(
    name="lab",
    help="Isolated VNC-backed MCP accuracy lab (never uses production PiKVM config).",
    no_args_is_help=True,
)
app.add_typer(lab_app, name="lab")

harness_app = typer.Typer(
    name="harness",
    help="Visible provider-neutral task harness over the guarded PiKVM MCP server.",
    no_args_is_help=True,
)
app.add_typer(harness_app, name="harness")


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
    uds: str = typer.Option(
        "",
        help="Listen on this unix socket instead of a host:port.",
    ),
) -> None:
    """Run the FastAPI daemon (owns sessions, watchers, execution)."""
    import os
    import uvicorn

    from pikvm_agent.config import load_config
    from pikvm_agent.daemon_access import DaemonAccess, DaemonAccessError

    try:
        DaemonAccess.from_environment()
    except DaemonAccessError as exc:
        typer.echo(f"daemon refused: {exc}", err=True)
        raise typer.Exit(2)
    cfg = load_config()
    if uds:
        # A socket file survives the process that made it. uvicorn will not bind
        # over an existing path, so a daemon killed rather than closed would
        # otherwise leave one that stops every later start - the socket version
        # of exactly the stale-port problem this move is meant to end.
        try:
            if os.path.exists(uds):
                os.unlink(uds)
        except OSError as exc:
            typer.echo(f"daemon refused: cannot clear {uds}: {exc}", err=True)
            raise typer.Exit(2)
        parent = os.path.dirname(uds)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Ours alone: the socket is the capability boundary now that there is no
        # port for the token to be the only thing guarding.
        os.umask(0o077)
        uvicorn.run(
            "pikvm_agent.daemon:app",
            uds=uds,
            log_level="info",
        )
        return
    uvicorn.run(
        "pikvm_agent.daemon:app",
        host=host or cfg.daemon.host,
        port=port or cfg.daemon.port,
        log_level="info",
    )


@app.command(hidden=True)
def mcp() -> None:
    """Internal raw-MCP child; public clients use harness managed/direct-mcp."""
    from pikvm_agent.config import require_daemon_url
    from pikvm_agent.mcp_server import main as mcp_main

    try:
        require_daemon_url()
    except (ValueError, SystemExit) as exc:
        typer.echo(f"MCP startup refused: {exc}", err=True)
        raise typer.Exit(2)
    try:
        mcp_main()
    except SystemExit as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2)


@harness_app.command("serve")
def harness_serve(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML (provider routes and env-var names; no secrets).",
    ),
) -> None:
    """Run the authenticated local chat workspace and control API."""
    import uvicorn

    from pikvm_agent.harness.config import (
        ensure_safe_bind,
        load_harness_settings,
    )
    from pikvm_agent.harness.server import build_harness_app

    settings = load_harness_settings(config)
    ensure_safe_bind(settings)
    host, port = settings.host_port()
    typer.echo(f"Chat workspace: http://{host}:{port}/app/")
    operator_app = build_harness_app(
        settings,
        settings_path=config,
    )

    class HarnessServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            operator_app.state.shutdown_requested.set()
            super().handle_exit(sig, frame)

    server = HarnessServer(
        uvicorn.Config(
            operator_app,
            host=host,
            port=port,
            log_level="info",
            # Event streams should exit from shutdown_requested within 200 ms.
            # This remains a hard upper bound if an adapter ignores cancellation.
            timeout_graceful_shutdown=3,
        )
    )
    server.run()


@harness_app.command("ui-fixture")
def harness_ui_fixture(
    listen: str = typer.Option(
        "127.0.0.1:47619",
        "--listen",
        help="Loopback-only host:port for the synthetic chat workspace.",
    ),
    prefill_events: int = typer.Option(
        1_200,
        "--prefill-events",
        min=32,
        max=100_000,
        help="Durable synthetic events present before the browser connects.",
    ),
    event_interval_ms: int = typer.Option(
        250,
        "--event-interval-ms",
        min=50,
        max=60_000,
        help="Synthetic provider/action transition interval.",
    ),
) -> None:
    """Run a deterministic chat-workspace audit with no computer target."""
    import ipaddress
    import secrets

    import uvicorn

    from pikvm_agent.harness.ui_fixture import build_fixture_app

    try:
        host, port_text = listen.rsplit(":", 1)
        host = host.strip("[]")
        port = int(port_text)
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except (AttributeError, ValueError) as exc:
        typer.echo(f"UI fixture refused: invalid loopback listen address: {exc}", err=True)
        raise typer.Exit(2)
    if not loopback or not 1 <= port <= 65535:
        typer.echo("UI fixture refused: --listen must be a loopback host:port", err=True)
        raise typer.Exit(2)
    if port == 47615:
        typer.echo(
            "UI fixture refused: production daemon port 47615 is reserved",
            err=True,
        )
        raise typer.Exit(2)
    token = secrets.token_hex(32)
    origin = f"http://{f'[{host}]' if ':' in host else host}:{port}"
    fixture_app = build_fixture_app(
        access_token=token,
        origin=origin,
        prefill_events=prefill_events,
        event_interval_ms=event_interval_ms,
    )
    typer.echo("Synthetic UI audit only: no VNC, PiKVM, or model API is used.")
    typer.echo(f"Chat workspace: {origin}/app/")
    typer.echo(f"One-time fixture token: {token}")
    uvicorn.run(
        fixture_app,
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=3,
    )


@harness_app.command("browser-audit")
def harness_browser_audit(
    browsers: str = typer.Option(
        "chromium,firefox,webkit",
        "--browsers",
        help="Comma-separated Playwright engines to audit.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        dir_okay=False,
        help="Optional JSON evidence path.",
    ),
    timeout_ms: int = typer.Option(
        30_000,
        "--timeout-ms",
        min=5_000,
        max=120_000,
        help="Per-browser Playwright action timeout.",
    ),
) -> None:
    """Audit the authenticated chat/tool UI without a computer or model."""
    from pikvm_agent.harness.browser_matrix import (
        BrowserAuditDependencyError,
        parse_browser_names,
        run_browser_matrix_audit,
        write_browser_audit_report,
    )

    try:
        names = parse_browser_names(browsers)
        report = run_browser_matrix_audit(names, timeout_ms=timeout_ms)
    except (BrowserAuditDependencyError, ValueError) as exc:
        typer.echo(f"Browser audit refused: {exc}", err=True)
        raise typer.Exit(2)
    if output is not None:
        try:
            write_browser_audit_report(output, report)
        except ValueError as exc:
            typer.echo(f"Browser audit refused: {exc}", err=True)
            raise typer.Exit(2)
        typer.echo(f"Evidence: {output}")
    summary = report["summary"]
    typer.echo(
        "Target-free browser audit: "
        f"{summary['passed']}/{summary['requested']} passed; "
        "no VNC, PiKVM, model, or production daemon contact."
    )
    if not summary["release_gate_passed"]:
        for name, result in report["browsers"].items():
            if result["status"] != "passed":
                typer.echo(
                    f"{name}: {result.get('failure', 'failed')}",
                    err=True,
                )
        raise typer.Exit(1)


@harness_app.command("smoke-lab")
def harness_smoke_lab(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness connection/config contract used by managed MCP clients.",
    ),
    root: Path = typer.Option(
        ...,
        "--root",
        help="Private state and frame directory for this smoke-lab run.",
    ),
    live_providers: bool = typer.Option(
        False,
        "--live-providers",
        help=(
            "Use the configured OAuth/API model routes against the synthetic "
            "computer instead of the deterministic offline provider."
        ),
    ),
    allow_provider_calls: bool = typer.Option(
        False,
        "--allow-provider-calls",
        help="Explicitly authorize billable or quota-consuming provider calls.",
    ),
) -> None:
    """Run a target-free full-stack managed-client acceptance lab."""
    import uvicorn

    from pikvm_agent.harness.config import (
        build_model_pool,
        ensure_safe_bind,
        ensure_provider_prerequisites,
        load_harness_settings,
    )
    from pikvm_agent.harness.lab import PRODUCTION_DAEMON_PORT
    from pikvm_agent.harness.smoke_lab import build_managed_smoke_app

    settings = load_harness_settings(config)
    ensure_safe_bind(settings)
    if live_providers and not allow_provider_calls:
        typer.echo(
            "Live provider calls require --allow-provider-calls.",
        )
        raise typer.Exit(2)
    host, port = settings.host_port()
    if port == PRODUCTION_DAEMON_PORT:
        typer.echo(
            "Managed smoke lab refused: production daemon port is reserved.",
            err=True,
        )
        raise typer.Exit(2)
    models = None
    if live_providers:
        ensure_provider_prerequisites(settings)
        models = build_model_pool(settings)
    operator_app = build_managed_smoke_app(
        root=root,
        access_token=settings.access_token(),
        agent_token=settings.agent_token(),
        allowed_origin=f"http://{host}:{port}",
        models=models,
    )
    if live_providers:
        typer.echo(
            "Target-free managed smoke lab: synthetic computer with configured "
            "live model routes; no VNC, PiKVM, daemon, or HID."
        )
    else:
        typer.echo(
            "Target-free managed smoke lab: no VNC, PiKVM, daemon, or model API."
        )
    typer.echo(f"Chat workspace: http://{host}:{port}/app/")
    uvicorn.run(
        operator_app,
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=3,
    )


@harness_app.command("mcp")
def harness_mcp() -> None:
    """Run the high-level non-approval task/status/control MCP facade."""
    from pikvm_agent.harness_mcp_server import main as harness_mcp_main

    harness_mcp_main()


@harness_app.command("managed-mcp")
def harness_managed_mcp(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML used by the running chat workspace.",
    ),
    require_ready: bool = typer.Option(
        False,
        "--require-ready",
        help=(
            "Fail startup unless the harness is already reachable; by default "
            "the safe high-level MCP stays available across harness restarts."
        ),
    ),
    caller_label: str = typer.Option(
        "mcp-client",
        "--caller-label",
        help="Human-readable client label shown in the conversation timeline.",
    ),
) -> None:
    """Run the safe high-level managed harness MCP facade."""
    from pikvm_agent.harness.client_setup import (
        managed_mcp_environment,
        verify_managed_harness_ready,
    )
    from pikvm_agent.harness.config import (
        ensure_safe_bind,
        load_harness_settings,
    )
    from pikvm_agent.harness_mcp_server import main as harness_mcp_main

    settings = load_harness_settings(config)
    ensure_safe_bind(settings)
    try:
        environment = managed_mcp_environment(
            settings,
            caller_label=caller_label,
        )
        if require_ready:
            verify_managed_harness_ready(settings)
    except Exception as exc:
        typer.echo(
            f"managed MCP startup refused: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    os.environ.update(environment)
    harness_mcp_main()


def _run_runtime_managed_mcp(
    runtime: Path,
    *,
    require_ready: bool,
    caller_label: str,
    refusal_label: str,
) -> None:
    from pikvm_agent.harness.client_setup import (
        managed_mcp_environment,
        verify_managed_harness_ready,
    )
    from pikvm_agent.harness.config import ensure_safe_bind
    from pikvm_agent.harness.managed_client_runtime import (
        load_managed_client_runtime,
    )
    from pikvm_agent.harness_mcp_server import main as harness_mcp_main

    try:
        loaded = load_managed_client_runtime(runtime)
        ensure_safe_bind(loaded.settings)
        environment = managed_mcp_environment(
            loaded.settings,
            caller_label=caller_label,
            environ=loaded.environment,
        )
        if require_ready:
            verify_managed_harness_ready(
                loaded.settings,
                environ=loaded.environment,
            )
    except Exception as exc:
        typer.echo(
            f"{refusal_label} startup refused: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    os.environ.update(environment)
    harness_mcp_main()


@harness_app.command("managed-runtime-mcp")
def harness_managed_runtime_mcp(
    runtime: Path = typer.Option(
        ...,
        "--runtime",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Owner-only runtime handoff published by the active managed "
            "harness."
        ),
    ),
    require_ready: bool = typer.Option(
        False,
        "--require-ready",
        help=(
            "Fail startup unless the harness is already reachable; by default "
            "the safe high-level MCP stays available across harness restarts."
        ),
    ),
    caller_label: str = typer.Option(
        "mcp-client",
        "--caller-label",
        help="Human-readable client label shown in the conversation timeline.",
    ),
) -> None:
    """Run managed MCP without requiring a token in the parent shell."""

    _run_runtime_managed_mcp(
        runtime,
        require_ready=require_ready,
        caller_label=caller_label,
        refusal_label="managed runtime MCP",
    )


@harness_app.command("active-managed-mcp")
def harness_active_managed_mcp(
    caller_label: str = typer.Option(
        "mcp-client",
        "--caller-label",
        help="Human-readable client label shown in the conversation timeline.",
    ),
    require_ready: bool = typer.Option(
        False,
        "--require-ready",
        help=(
            "Fail startup unless the harness is already reachable; by default "
            "the safe high-level MCP stays available across harness restarts."
        ),
    ),
) -> None:
    """Run managed MCP from the active desktop-owned runtime handoff."""

    from pikvm_agent.harness.managed_client_runtime import (
        active_managed_client_runtime_path,
    )

    _run_runtime_managed_mcp(
        active_managed_client_runtime_path(),
        require_ready=require_ready,
        caller_label=caller_label,
        refusal_label="active managed MCP",
    )


@harness_app.command("activate-managed-runtime")
def harness_activate_managed_runtime(
    runtime: Path = typer.Option(
        ...,
        "--runtime",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Owner-only source runtime published by a healthy harness.",
    ),
    output: Path | None = typer.Option(
        None,
        "--out",
        dir_okay=False,
        help=(
            "Optional active-runtime destination; the per-user runtime "
            "location is used by default."
        ),
    ),
) -> None:
    """Publish only the active agent capability for persistent MCP clients."""

    from pikvm_agent.harness.managed_client_runtime import (
        publish_active_managed_client_runtime,
    )

    try:
        destination = publish_active_managed_client_runtime(
            runtime,
            destination=output,
        )
    except Exception as exc:
        typer.echo(
            f"managed runtime activation refused: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(f"Activated managed client runtime: {destination}")


@harness_app.command("direct-mcp")
def harness_direct_mcp(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML used by the running chat workspace.",
    ),
    mode: str = typer.Option(
        "guarded",
        "--mode",
        help=(
            "guarded fails closed; observe degrades to read-only perception "
            "plus abort/panic-stop."
        ),
    ),
    caller_label: str = typer.Option(
        "mcp-client",
        "--caller-label",
        help="Human-readable client label shown in the conversation timeline.",
    ),
) -> None:
    """Run raw PiKVM MCP tools through the visible direct-call boundary."""
    from pikvm_agent.harness.client_setup import (
        direct_mcp_environment,
        verify_direct_harness_ready,
    )
    from pikvm_agent.harness.config import (
        ensure_safe_bind,
        load_harness_settings,
    )
    from pikvm_agent.mcp_server import main as mcp_main

    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"guarded", "observe"}:
        typer.echo("--mode must be guarded or observe", err=True)
        raise typer.Exit(2)
    settings = load_harness_settings(config)
    ensure_safe_bind(settings)
    try:
        environment = direct_mcp_environment(
            settings,
            mode=normalized_mode,  # type: ignore[arg-type]
            caller_label=caller_label,
        )
        if normalized_mode == "guarded":
            verify_direct_harness_ready(settings)
    except Exception as exc:
        typer.echo(
            f"{normalized_mode} direct MCP startup refused: "
            f"{type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    os.environ.update(environment)
    mcp_main()


@harness_app.command("client-config")
def harness_client_config(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML referenced by the guarded MCP launcher.",
    ),
    client: str = typer.Option(
        ...,
        "--client",
        help="Client format: codex, claude, gemini, or opencode.",
    ),
    control_mode: str = typer.Option(
        "managed",
        "--control-mode",
        help="managed uses the harness loop; direct preserves client tool control.",
    ),
    output: Path | None = typer.Option(
        None,
        "--out",
        help="Optional destination; omit to print the secret-free snippet.",
    ),
    server_name: str = typer.Option(
        "pikvm",
        "--server-name",
        help="MCP server name in the generated client configuration.",
    ),
    managed_runtime: Path | None = typer.Option(
        None,
        "--managed-runtime",
        dir_okay=False,
        help=(
            "Owner-only runtime handoff read by managed MCP at connection "
            "time; avoids requiring a shell token."
        ),
    ),
) -> None:
    """Generate a secret-free managed or guarded-direct MCP configuration."""
    from pikvm_agent.harness.client_setup import render_client_config
    from pikvm_agent.harness.config import load_harness_settings

    normalized_client = client.strip().lower()
    if normalized_client not in {"codex", "claude", "gemini", "opencode"}:
        typer.echo(
            "--client must be codex, claude, gemini, or opencode",
            err=True,
        )
        raise typer.Exit(2)
    normalized_control_mode = control_mode.strip().lower()
    if normalized_control_mode not in {"managed", "direct"}:
        typer.echo("--control-mode must be managed or direct", err=True)
        raise typer.Exit(2)
    settings = load_harness_settings(config)
    rendered = render_client_config(
        settings,
        client=normalized_client,  # type: ignore[arg-type]
        executable=os.path.abspath(sys.executable),
        harness_config=config,
        control_mode=normalized_control_mode,  # type: ignore[arg-type]
        server_name=server_name,
        managed_runtime=managed_runtime,
        active_runtime=(
            normalized_control_mode == "managed"
            and managed_runtime is None
        ),
    )
    if output is None:
        typer.echo(rendered, nl=False)
        return
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    typer.echo(
        f"Wrote secret-free {normalized_control_mode} "
        f"{normalized_client} MCP config: {destination}"
    )


@harness_app.command("active-client-config")
def harness_active_client_config(
    client: str = typer.Option(
        ...,
        "--client",
        help="Client format: codex, claude, gemini, or opencode.",
    ),
    output: Path | None = typer.Option(
        None,
        "--out",
        help="Optional destination; omit to print the path-free snippet.",
    ),
    server_name: str = typer.Option(
        "pikvm",
        "--server-name",
        help="MCP server name in the generated client configuration.",
    ),
) -> None:
    """Generate a path-free client registration for the active desktop."""

    from pikvm_agent.harness.client_setup import (
        render_active_managed_client_config,
    )

    normalized_client = client.strip().lower()
    if normalized_client not in {"codex", "claude", "gemini", "opencode"}:
        typer.echo(
            "--client must be codex, claude, gemini, or opencode",
            err=True,
        )
        raise typer.Exit(2)
    try:
        rendered = render_active_managed_client_config(
            client=normalized_client,  # type: ignore[arg-type]
            executable=os.path.abspath(sys.executable),
            server_name=server_name,
        )
    except ValueError as exc:
        typer.echo(f"active client config refused: {exc}", err=True)
        raise typer.Exit(2)
    if output is None:
        typer.echo(rendered, nl=False)
        return
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    typer.echo(
        f"Wrote path-free managed {normalized_client} MCP config: "
        f"{destination}"
    )


@harness_app.command("active-client-install")
def harness_active_client_install(
    client: str = typer.Option(
        ...,
        "--client",
        help="Client settings format; reviewed installation currently supports gemini.",
    ),
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Existing client settings file to preserve and update atomically.",
    ),
    server_name: str = typer.Option(
        "pikvm",
        "--server-name",
        help="MCP server name for the managed registration.",
    ),
    reviewed_sha256: str | None = typer.Option(
        None,
        "--reviewed-sha256",
        help=(
            "Apply only the exact candidate emitted by the planning invocation; "
            "omit to inspect the plan without changing settings."
        ),
    ),
) -> None:
    """Plan or atomically install the active managed MCP registration."""

    from pikvm_agent.harness.managed_client_install import (
        ManagedClientInstallError,
        install_active_managed_registration,
        plan_active_managed_install,
    )

    normalized_client = client.strip().lower()
    if normalized_client != "gemini":
        typer.echo(
            "managed client installation refused: supported client is gemini",
            err=True,
        )
        raise typer.Exit(2)
    try:
        plan = plan_active_managed_install(
            client="gemini",
            config_path=config,
            executable=sys.executable,
            server_name=server_name,
        )
        if reviewed_sha256 is None:
            payload = plan.summary()
            payload["applied"] = False
        else:
            receipt = install_active_managed_registration(
                plan=plan,
                reviewed_sha256=reviewed_sha256,
            )
            payload = receipt.summary()
            payload["applied"] = True
    except ManagedClientInstallError as exc:
        typer.echo(f"managed client installation refused: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(payload, indent=2))


@harness_app.command("active-client-rollback")
def harness_active_client_rollback(
    receipt: Path = typer.Option(
        ...,
        "--receipt",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Owner-only receipt emitted by active-client-install.",
    ),
) -> None:
    """Restore exact pre-install settings when no later edits exist."""

    from pikvm_agent.harness.managed_client_install import (
        ManagedClientInstallError,
        rollback_active_managed_registration,
    )

    try:
        payload = rollback_active_managed_registration(receipt)
    except ManagedClientInstallError as exc:
        typer.echo(f"managed client rollback refused: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(payload, indent=2))


@harness_app.command("client-audit")
def harness_client_audit(
    client: str = typer.Option(
        ...,
        "--client",
        help="Client format: codex, claude, gemini, or opencode.",
    ),
    config: list[Path] = typer.Option(
        [],
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Client config to inspect; repeat from lowest to highest "
            "precedence. Reports use anonymous config-N labels."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--out",
        dir_okay=False,
        help=(
            "Optional new mode-0600 JSON report; existing files are never "
            "overwritten."
        ),
    ),
    native_inventory: bool = typer.Option(
        False,
        "--native-inventory",
        help=(
            "Ask Codex for its resolved JSON MCP inventory without launching "
            "the configured servers."
        ),
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        readable=True,
        help="Trusted project directory used by Codex inventory resolution.",
    ),
    codex_executable: str = typer.Option(
        "codex",
        "--codex-executable",
        help="Codex CLI executable used only with --native-inventory.",
    ),
) -> None:
    """Fail unless exactly one managed PiKVM registration is configured."""
    from pikvm_agent.harness.client_config_audit import (
        ClientConfigDocument,
        audit_client_configs,
        read_codex_effective_inventory,
        write_client_config_audit_report,
    )

    normalized_client = client.strip().lower()
    if normalized_client not in {"codex", "claude", "gemini", "opencode"}:
        typer.echo(
            "--client must be codex, claude, gemini, or opencode",
            err=True,
        )
        raise typer.Exit(2)
    if native_inventory and normalized_client != "codex":
        typer.echo(
            "--native-inventory is currently available only for Codex",
            err=True,
        )
        raise typer.Exit(2)
    if native_inventory and config:
        typer.echo(
            "--native-inventory and --config are mutually exclusive",
            err=True,
        )
        raise typer.Exit(2)
    if not native_inventory and not config:
        typer.echo("at least one --config is required", err=True)
        raise typer.Exit(2)
    documents = []
    if native_inventory:
        try:
            documents.append(
                read_codex_effective_inventory(
                    executable=codex_executable,
                    project_dir=project,
                )
            )
        except Exception as exc:
            typer.echo(
                f"Codex inventory refused: {type(exc).__name__}",
                err=True,
            )
            raise typer.Exit(2)
    else:
        for index, path in enumerate(config, start=1):
            try:
                rendered = path.read_text(encoding="utf-8")
            except OSError:
                typer.echo(
                    f"could not read config-{index}",
                    err=True,
                )
                raise typer.Exit(2)
            documents.append(
                ClientConfigDocument(
                    source_label=f"config-{index}",
                    rendered=rendered,
                )
            )
    report = audit_client_configs(
        client=normalized_client,  # type: ignore[arg-type]
        documents=documents,
    )
    if output is None:
        typer.echo(report.model_dump_json(indent=2))
    else:
        try:
            write_client_config_audit_report(output, report)
        except ValueError as exc:
            typer.echo(f"Client audit refused: {exc}.", err=True)
            raise typer.Exit(2)
        except OSError as exc:
            typer.echo(
                f"Client audit refused: {type(exc).__name__}.",
                err=True,
            )
            raise typer.Exit(2)
        status = "passed" if report.safe else "failed"
        typer.echo(f"Client audit {status}; report written.")
    if not report.safe:
        raise typer.Exit(1)


@harness_app.command("client-launch")
def harness_client_launch(
    config: Path | None = typer.Option(
        None,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Optional Harness YAML; omit it to use the active desktop-owned "
            "runtime."
        ),
    ),
    client: str = typer.Option(
        ...,
        "--client",
        help="Stable isolated client: codex, claude, gemini, or opencode.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        readable=True,
        help="Trusted working directory for the coding client.",
    ),
    client_executable: str | None = typer.Option(
        None,
        "--client-executable",
        help="Override the coding-client executable.",
    ),
    managed_runtime: Path | None = typer.Option(
        None,
        "--managed-runtime",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Owner-only managed runtime handoff; keeps the agent capability "
            "out of the coding-client environment."
        ),
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Launch after isolation audit and local harness readiness checks.",
    ),
) -> None:
    """Audit and optionally launch a client with managed-only PiKVM control."""
    import json

    from pikvm_agent.harness.client_setup import verify_managed_harness_ready
    from pikvm_agent.harness.config import load_harness_settings
    from pikvm_agent.harness.managed_client_launcher import (
        ClientIsolationError,
        audit_managed_client_launch,
        build_managed_client_launch,
        run_managed_client_launch,
    )
    from pikvm_agent.harness.managed_client_runtime import (
        active_managed_client_runtime_path,
        load_managed_client_runtime,
    )

    normalized = client.strip().lower()
    executable = client_executable or normalized
    try:
        if config is None:
            selected_runtime = (
                managed_runtime
                if managed_runtime is not None
                else active_managed_client_runtime_path()
            )
            loaded_runtime = load_managed_client_runtime(selected_runtime)
            settings = loaded_runtime.settings
            harness_config = loaded_runtime.harness_config
            runtime_environment = loaded_runtime.environment
            runtime_for_plan = selected_runtime
        else:
            settings = load_harness_settings(config)
            harness_config = config
            runtime_for_plan = managed_runtime
            runtime_environment = (
                load_managed_client_runtime(
                    managed_runtime,
                    expected_harness_config=config,
                ).environment
                if managed_runtime is not None
                else None
            )
        plan = build_managed_client_launch(
            settings,
            client=normalized,
            client_executable=executable,
            mcp_executable=os.path.abspath(sys.executable),
            harness_config=harness_config,
            project_dir=project,
            managed_runtime=runtime_for_plan,
        )
        report = audit_managed_client_launch(
            plan,
            environ=runtime_environment,
        )
    except ClientIsolationError as exc:
        typer.echo(f"Managed client launch refused: {exc}.", err=True)
        raise typer.Exit(2)
    except Exception as exc:
        typer.echo(
            f"Managed client launch preflight refused: {type(exc).__name__}.",
            err=True,
        )
        raise typer.Exit(2)

    if not execute:
        typer.echo(
            json.dumps(
                plan.summary(report=report, would_launch=False),
                indent=2,
            )
        )
        return

    try:
        verify_managed_harness_ready(
            settings,
            environ=runtime_environment,
        )
    except Exception as exc:
        typer.echo(
            f"Managed client launch refused: harness {type(exc).__name__}.",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(
        f"Launching {normalized} with one verified managed PiKVM surface."
    )
    exit_code = run_managed_client_launch(
        plan,
        environ=runtime_environment,
    )
    if exit_code:
        raise typer.Exit(exit_code)


@harness_app.command("client-task")
def harness_client_task(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML used by the managed MCP task runner.",
    ),
    client: str = typer.Option(
        ...,
        "--client",
        help="Stable isolated client: codex, claude, gemini, or opencode.",
    ),
    lab_runtime: Path | None = typer.Option(
        None,
        "--lab-runtime",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Owner-only managed-client runtime emitted by `lab up`; "
            "loads only the agent-scoped credential."
        ),
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        readable=True,
        help="Trusted read-only working directory for the coding client.",
    ),
    client_executable: str | None = typer.Option(
        None,
        "--client-executable",
        help="Override the coding-client executable.",
    ),
    max_runtime_s: float = typer.Option(
        1_800,
        "--max-runtime-s",
        min=1,
        max=86_400,
        help="Hard wall-clock limit for the outer coding-client task.",
    ),
) -> None:
    """Run one stdin task through an audited managed-only PiKVM surface."""
    import json

    from pikvm_agent.harness.client_setup import (
        harness_base_url,
        verify_managed_harness_ready,
    )
    from pikvm_agent.harness.config import load_harness_settings
    from pikvm_agent.harness.managed_client_launcher import (
        ClientIsolationError,
        HarnessTaskCompletionWatch,
        ManagedTaskCompletionError,
        audit_managed_client_launch,
        build_managed_client_launch,
        run_managed_client_task,
    )
    from pikvm_agent.harness.lab import load_lab_client_environment

    normalized = client.strip().lower()
    executable = client_executable or normalized
    settings = load_harness_settings(config)
    try:
        runtime_environment = (
            load_lab_client_environment(
                lab_runtime,
                settings=settings,
                harness_config=config,
            )
            if lab_runtime is not None
            else None
        )
        plan = build_managed_client_launch(
            settings,
            client=normalized,
            client_executable=executable,
            mcp_executable=os.path.abspath(sys.executable),
            harness_config=config,
            project_dir=project,
            managed_runtime=lab_runtime,
        )
        report = audit_managed_client_launch(
            plan,
            environ=runtime_environment,
        )
        if runtime_environment is None:
            verify_managed_harness_ready(settings)
        else:
            verify_managed_harness_ready(
                settings,
                environ=runtime_environment,
            )
        completion_watch = HarnessTaskCompletionWatch(
            base_url=harness_base_url(settings),
            agent_token=settings.agent_token(
                validate_distinct=False,
                environ=runtime_environment,
            ),
            caller_label=f"{normalized}-cli",
        )
    except ClientIsolationError as exc:
        typer.echo(f"Managed client task refused: {exc}.", err=True)
        raise typer.Exit(2)
    except Exception as exc:
        typer.echo(
            f"Managed client task preflight refused: {type(exc).__name__}.",
            err=True,
        )
        raise typer.Exit(2)

    task = sys.stdin.read(32 * 1024 + 1)
    if len(task.encode("utf-8")) > 32 * 1024:
        typer.echo("Managed client task refused: task exceeds 32 KiB.", err=True)
        raise typer.Exit(2)
    try:
        result = run_managed_client_task(
            plan,
            task=task,
            timeout_s=max_runtime_s,
            environ=runtime_environment,
            completion_watch=completion_watch,
        )
    except ManagedTaskCompletionError as exc:
        typer.echo(f"Managed client task incomplete: {exc}.", err=True)
        raise typer.Exit(1)
    except (ClientIsolationError, ValueError) as exc:
        typer.echo(f"Managed client task refused: {exc}.", err=True)
        raise typer.Exit(2)
    summary = plan.summary(report=report, would_launch=True)
    summary["task"] = result.summary()
    typer.echo(json.dumps(summary, indent=2))
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@harness_app.command("init")
def harness_init(
    output: Path = typer.Option(
        Path("config.harness.yaml"),
        "--out",
        help="Destination for the secret-free harness configuration.",
    ),
    oauth_clis: str = typer.Option(
        "auto",
        "--oauth-clis",
        help=(
            "OAuth CLI adapters: auto, all, both, none, or a comma-separated "
            "selection of codex, claude, and gemini."
        ),
    ),
    gemini_cli_home_env: str = typer.Option(
        "PIKVM_GEMINI_CLI_HOME",
        "--gemini-cli-home-env",
        help=(
            "Environment variable containing a dedicated Gemini CLI profile "
            "root; required for the Gemini OAuth adapter."
        ),
    ),
    web_search: bool = typer.Option(
        True,
        "--web-search/--no-web-search",
        help=(
            "Expose the packaged read-only web search MCP tools to normal "
            "assistant chats."
        ),
    ),
    listen: str = typer.Option(
        "127.0.0.1:47616",
        "--listen",
        help="Loopback operator UI/API bind.",
    ),
    openai_model: str | None = typer.Option(
        None,
        "--openai-model",
        help="Include the native OpenAI Responses adapter with this model.",
    ),
    openai_base_url: str | None = typer.Option(
        None,
        "--openai-base-url",
        help="Optional OpenAI-compatible Responses API base URL.",
    ),
    openai_api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--openai-key-env",
        help="Environment variable name for the OpenAI credential.",
    ),
    azure_model: str | None = typer.Option(
        None,
        "--azure-model",
        help="Include an Azure OpenAI Responses deployment.",
    ),
    azure_base_url: str | None = typer.Option(
        None,
        "--azure-base-url",
        help=(
            "Azure Responses base URL ending in /openai/v1."
        ),
    ),
    azure_auth: str = typer.Option(
        "api-key",
        "--azure-auth",
        help="Azure authentication: api-key, entra-env, or azure-cli.",
    ),
    azure_credential_env: str | None = typer.Option(
        None,
        "--azure-credential-env",
        help=(
            "Optional Azure key/token environment variable name; defaults "
            "from --azure-auth."
        ),
    ),
    anthropic_model: str | None = typer.Option(
        None,
        "--anthropic-model",
        help="Include the Anthropic Messages adapter with this model.",
    ),
    anthropic_api_key_env: str = typer.Option(
        "ANTHROPIC_API_KEY",
        "--anthropic-key-env",
        help="Environment variable name for the Anthropic credential.",
    ),
    gemini_model: str | None = typer.Option(
        None,
        "--gemini-model",
        help="Include the Gemini generateContent adapter with this model.",
    ),
    gemini_api_key_env: str = typer.Option(
        "GEMINI_API_KEY",
        "--gemini-key-env",
        help="Environment variable name for the Gemini credential.",
    ),
    vertex_model: str | None = typer.Option(
        None,
        "--vertex-model",
        help="Include a Gemini model through Vertex AI.",
    ),
    vertex_base_url: str | None = typer.Option(
        None,
        "--vertex-base-url",
        help=(
            "Vertex publisher base URL ending in /publishers/google."
        ),
    ),
    vertex_auth: str = typer.Option(
        "gcloud",
        "--vertex-auth",
        help="Vertex authentication: gcloud or token-env.",
    ),
    vertex_credential_env: str | None = typer.Option(
        None,
        "--vertex-credential-env",
        help=(
            "Optional Vertex access-token environment variable name."
        ),
    ),
    compatible_model: str | None = typer.Option(
        None,
        "--compatible-model",
        help="Include an OpenAI-compatible gateway model.",
    ),
    compatible_base_url: str | None = typer.Option(
        None,
        "--compatible-base-url",
        help="Base URL for the OpenAI-compatible gateway.",
    ),
    compatible_api_key_env: str = typer.Option(
        "MODEL_GATEWAY_KEY",
        "--compatible-key-env",
        help="Environment variable name for the gateway credential.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace an existing destination intentionally.",
    ),
) -> None:
    """Create a validated provider-neutral harness config without secrets."""
    from pikvm_agent.harness.onboarding import (
        build_initial_harness_settings,
        render_initial_harness_config,
    )

    destination = output.expanduser().resolve()
    if destination.exists() and not force:
        typer.echo(
            f"harness config already exists: {destination}; use --force to replace it",
            err=True,
        )
        raise typer.Exit(1)
    try:
        settings = build_initial_harness_settings(
            oauth_clis=oauth_clis,
            gemini_cli_home_env=gemini_cli_home_env,
            web_search=web_search,
            listen=listen,
            openai_model=openai_model,
            openai_base_url=openai_base_url,
            openai_api_key_env=openai_api_key_env,
            azure_model=azure_model,
            azure_base_url=azure_base_url,
            azure_auth=azure_auth,
            azure_credential_env=azure_credential_env,
            anthropic_model=anthropic_model,
            anthropic_api_key_env=anthropic_api_key_env,
            gemini_model=gemini_model,
            gemini_api_key_env=gemini_api_key_env,
            vertex_model=vertex_model,
            vertex_base_url=vertex_base_url,
            vertex_auth=vertex_auth,
            vertex_credential_env=vertex_credential_env,
            compatible_model=compatible_model,
            compatible_base_url=compatible_base_url,
            compatible_api_key_env=compatible_api_key_env,
        )
    except ValueError as exc:
        typer.echo(f"harness init failed: {exc}", err=True)
        raise typer.Exit(2)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_initial_harness_config(settings))
    credential_envs = sorted(
        {
            env_name
            for spec in settings.providers.values()
            for env_name in (
                spec.api_key_env,
                spec.credential_env,
                spec.profile_home_env,
            )
            if env_name
        }
    )
    typer.echo(f"Wrote secret-free harness config: {destination}")
    typer.echo(
        "Required to start chat: PIKVM_HARNESS_TOKEN"
    )
    typer.echo(
        "Required only for computer control: PIKVM_AGENT_DAEMON, "
        "PIKVM_HARNESS_AGENT_TOKEN, PIKVM_HARNESS_OBSERVER_TOKEN"
    )
    if credential_envs:
        typer.echo(
            "Selected API credential environment variables: "
            + ", ".join(credential_envs)
        )
    typer.echo(
        f"Next: pikvm-agent harness check --config {destination}"
    )


@harness_app.command("check")
def harness_check(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    require_computer: bool = typer.Option(
        False,
        "--require-computer",
        help=(
            "Also require an explicitly selected computer daemon and the "
            "managed/direct client credentials."
        ),
    ),
) -> None:
    """Validate chat readiness and optionally require computer control."""
    import json

    from pikvm_agent.harness.config import (
        check_provider_prerequisites,
        ensure_safe_bind,
        ensure_provider_prerequisites,
        load_harness_settings,
    )

    try:
        settings = load_harness_settings(config)
        ensure_safe_bind(settings)
        # Resolve required env-owned values, but never print their contents.
        settings.access_token()
        daemon_url = settings.optional_daemon_url()
        if daemon_url is not None:
            from pikvm_agent.daemon_access import DaemonAccess

            DaemonAccess.from_environment()
            settings.agent_token()
            settings.observer_token()
        elif settings.agent_token_env in os.environ:
            # The server accepts an optional managed-client token in
            # target-free mode, but a supplied token must still be valid.
            settings.agent_token()
        if require_computer and daemon_url is None:
            raise ValueError(
                f"{settings.daemon_url_env} is required by "
                "--require-computer"
            )
        provider_statuses = check_provider_prerequisites(settings)
        ensure_provider_prerequisites(settings)
    except ValueError as exc:
        typer.echo(f"Harness check failed: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "listen": settings.listen,
                "computer": {
                    "configured": daemon_url is not None,
                    "ready": daemon_url is not None,
                },
                "providers": provider_statuses,
                "routes": settings.routes.model_dump(),
                "state_path": str(settings.state_path),
                "artifact_dir": str(settings.artifact_dir),
            },
            indent=2,
        )
    )


@harness_app.command("support-bundle")
def harness_support_bundle(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML; its path and contents are never copied to the bundle.",
    ),
    output: Path = typer.Option(
        ...,
        "--out",
        dir_okay=False,
        help="New mode-0600 JSON file; existing files are never overwritten.",
    ),
) -> None:
    """Create an offline, redacted operator-support health bundle."""
    from pikvm_agent.harness.config import load_harness_settings
    from pikvm_agent.harness.support_bundle import (
        build_support_bundle,
        write_support_bundle,
    )

    try:
        settings = load_harness_settings(config)
        bundle = build_support_bundle(
            settings,
            config_bytes=config.read_bytes(),
        )
        write_support_bundle(output, bundle)
    except Exception as exc:
        # Arbitrary configuration values, paths, provider output, and target
        # URLs are not safe support-channel diagnostics.
        typer.echo(
            f"Support bundle refused: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(
        "Redacted offline support bundle created. Review it before sharing."
    )


@harness_app.command("showcase-run")
def harness_showcase_run(
    manifest: Path = typer.Option(
        ...,
        "--manifest",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Checked task manifest for the recorded campaign.",
    ),
    output_root: Path | None = typer.Option(
        None,
        "--output-root",
        file_okay=False,
        help=(
            "Durable showcase directory. Defaults to "
            "$XDG_DATA_HOME/pikvm-agent/showcases."
        ),
    ),
    harness_url: str = typer.Option(
        ...,
        "--harness-url",
        envvar="PIKVM_SHOWCASE_HARNESS_URL",
        help="Authenticated managed-harness origin.",
    ),
    adapter_url: str = typer.Option(
        ...,
        "--adapter-url",
        envvar="PIKVM_SHOWCASE_ADAPTER_URL",
        help="Runtime-supplied local VNC adapter origin.",
    ),
    agent_token_env: str = typer.Option(
        "PIKVM_HARNESS_AGENT_TOKEN",
        "--agent-token-env",
        help="Environment variable owning the managed task credential.",
    ),
    operator_token_env: str = typer.Option(
        "PIKVM_HARNESS_TOKEN",
        "--operator-token-env",
        help="Environment variable owning explicit approval authority.",
    ),
    operator_origin: str = typer.Option(
        ...,
        "--operator-origin",
        envvar="PIKVM_SHOWCASE_OPERATOR_ORIGIN",
        help="Allowed harness origin attached to exact approval requests.",
    ),
    task_timeout_s: float = typer.Option(
        300,
        "--task-timeout-s",
        min=30,
        max=3_600,
    ),
    reboot_timeout_s: float = typer.Option(
        300,
        "--reboot-timeout-s",
        min=30,
        max=900,
    ),
    frame_interval_s: float = typer.Option(
        0.5,
        "--frame-interval-s",
        min=0.2,
        max=5,
    ),
    max_same_run_recoveries: int = typer.Option(
        8,
        "--max-same-run-recoveries",
        min=1,
        max=50,
        help="Bounded paused-checkpoint recoveries before a task fails.",
    ),
    only_task_id: str | None = typer.Option(
        None,
        "--only-task",
        help=(
            "Run exactly this task from the manifest. Unlike "
            "--stop-after-task, earlier tasks are not executed."
        ),
    ),
    stop_after_task_id: str | None = typer.Option(
        None,
        "--stop-after-task",
        help=(
            "Pause after this task's recording and mandatory reboot are "
            "complete."
        ),
    ),
) -> None:
    """Run Codex tasks one-by-one, record them, and reboot after every task."""
    import asyncio
    import json

    from pikvm_agent.harness.showcase_runner import (
        default_showcase_output_root,
        run_showcase_campaign,
    )

    agent_token = os.environ.get(agent_token_env, "")
    operator_token = os.environ.get(operator_token_env, "")
    if len(agent_token) < 32 or len(operator_token) < 32:
        typer.echo(
            "showcase run requires separate agent and operator credentials",
            err=True,
        )
        raise typer.Exit(2)
    try:
        report = asyncio.run(
            run_showcase_campaign(
                manifest_path=manifest,
                output_root=output_root or default_showcase_output_root(),
                harness_url=harness_url,
                adapter_url=adapter_url,
                agent_token=agent_token,
                operator_token=operator_token,
                operator_origin=operator_origin,
                task_timeout_s=task_timeout_s,
                reboot_timeout_s=reboot_timeout_s,
                frame_interval_s=frame_interval_s,
                max_same_run_recoveries=max_same_run_recoveries,
                only_task_id=only_task_id,
                stop_after_task_id=stop_after_task_id,
            )
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        typer.echo(f"showcase run failed: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(report, indent=2))


@harness_app.command("client-acceptance")
def harness_client_acceptance(
    output: Path = typer.Option(
        ...,
        "--out",
        dir_okay=False,
        help="New mode-0600 JSON report; existing files are never overwritten.",
    ),
    clients: list[str] | None = typer.Option(
        None,
        "--client",
        help=(
            "Generated client format to exercise; repeat or omit for Codex, "
            "Claude, Gemini, and OpenCode."
        ),
    ),
    timeout_s: float = typer.Option(
        15.0,
        "--timeout",
        min=3.0,
        max=120.0,
        help="Per-stage timeout in seconds.",
    ),
) -> None:
    """Run synthetic target-free managed MCP launch and restart acceptance."""
    import asyncio

    if output.exists():
        typer.echo(
            "Managed client acceptance refused: --out already exists.",
            err=True,
        )
        raise typer.Exit(2)
    if not output.parent.is_dir():
        typer.echo(
            "Managed client acceptance refused: --out parent is missing.",
            err=True,
        )
        raise typer.Exit(2)

    from pikvm_agent.harness.client_acceptance import (
        run_managed_client_acceptance,
        write_managed_client_acceptance_report,
    )

    try:
        report = asyncio.run(
            run_managed_client_acceptance(
                clients=clients,
                timeout_s=timeout_s,
            )
        )
        write_managed_client_acceptance_report(output, report)
    except Exception as exc:
        typer.echo(
            f"Managed client acceptance refused: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)
    typer.echo(
        f"Managed client acceptance: {report.clients_passed}/"
        f"{report.clients_requested} clients passed."
    )
    if report.clients_failed:
        raise typer.Exit(1)


@harness_app.command("provider-conformance")
def harness_provider_conformance(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML containing the providers to compare.",
    ),
    output: Path | None = typer.Option(
        None,
        "--out",
        dir_okay=False,
        help=(
            "New mode-0600 JSON report; defaults to the harness report path "
            "consumed by the operator UI."
        ),
    ),
    providers: list[str] | None = typer.Option(
        None,
        "--provider",
        help="Configured provider to test; repeat this option or omit it for all.",
    ),
    cases: int = typer.Option(
        3,
        "--cases",
        min=1,
        max=100,
        help="Identical seeded blind-screen cases per ready provider.",
    ),
    seed: int = typer.Option(
        104_729,
        "--seed",
        help="Deterministic synthetic-screen seed.",
    ),
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        min=1,
        max=16,
        help="Maximum simultaneous provider calls across the comparison.",
    ),
    allow_provider_calls: bool = typer.Option(
        False,
        "--allow-provider-calls",
        help="Explicitly permit billable or subscription-backed model calls.",
    ),
) -> None:
    """Compare provider vision/schema behavior without contacting a computer."""
    import asyncio
    import json
    import tempfile

    if not allow_provider_calls:
        typer.echo(
            "Provider conformance refused: add --allow-provider-calls to "
            "explicitly permit model-provider traffic.",
            err=True,
        )
        raise typer.Exit(2)
    if output is not None and output.exists():
        typer.echo(
            "Provider conformance refused: --out already exists.",
            err=True,
        )
        raise typer.Exit(2)

    from pikvm_agent.harness.config import (
        build_model_pool,
        check_provider_prerequisites,
        load_harness_settings,
    )
    from pikvm_agent.harness.provider_conformance import (
        run_provider_conformance,
        write_provider_conformance_report,
    )

    settings = load_harness_settings(config)
    report_path = output or settings.provider_conformance_path
    if report_path.exists():
        typer.echo(
            "Provider conformance refused: report path already exists.",
            err=True,
        )
        raise typer.Exit(2)
    readiness = check_provider_prerequisites(settings)
    selected_names = list(providers or settings.providers)
    unknown = sorted(set(selected_names) - set(settings.providers))
    if unknown:
        typer.echo(
            "Provider conformance refused: unknown providers: "
            + ", ".join(unknown),
            err=True,
        )
        raise typer.Exit(2)
    if not selected_names:
        typer.echo(
            "Provider conformance refused: no providers selected.",
            err=True,
        )
        raise typer.Exit(2)

    metadata = {
        name: {
            **readiness[name],
            "model": settings.providers[name].model,
        }
        for name in selected_names
    }
    pool = build_model_pool(settings)

    async def run() -> object:
        try:
            with tempfile.TemporaryDirectory(
                prefix="pikvm-provider-conformance-"
            ) as temporary:
                return await run_provider_conformance(
                    providers=pool.providers,
                    provider_metadata=metadata,
                    provider_names=selected_names,
                    cases=cases,
                    seed=seed,
                    concurrency=concurrency,
                    workspace=Path(temporary),
                )
        finally:
            closed: set[int] = set()
            for provider in pool.providers.values():
                if id(provider) in closed:
                    continue
                closed.add(id(provider))
                close = getattr(provider, "aclose", None)
                if close is not None:
                    await close()

    try:
        report = asyncio.run(run())
        write_provider_conformance_report(report_path, report)
    except Exception as exc:
        # Individual provider responses and errors are already reduced to safe
        # result fields. Do not print arbitrary exception bodies here.
        typer.echo(
            f"Provider conformance failed: {type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(2)

    typer.echo(
        json.dumps(
            {
                "suite": report.suite,
                "computer_target_contacted": (
                    report.computer_target_contacted
                ),
                "providers_selected": report.providers_selected,
                "providers_exercised": report.providers_exercised,
                "providers_unavailable": report.providers_unavailable,
                "calls_attempted": report.calls_attempted,
                "calls_schema_valid": report.calls_schema_valid,
                "calls_exact": report.calls_exact,
                "calls_failed": report.calls_failed,
                "exact_accuracy": report.exact_accuracy,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "report": str(report_path.resolve()),
            },
            indent=2,
        )
    )


@harness_app.command("assistant-conformance")
def harness_assistant_conformance(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML containing the provider and assistant MCP tools.",
    ),
    provider: str = typer.Option(
        ...,
        "--provider",
        help="One configured OAuth or API provider to exercise.",
    ),
    cases: list[str] | None = typer.Option(
        None,
        "--case",
        help=(
            "Fixed case to run; repeat or omit for greeting, general question, "
            "research, computer hand-off, and consequential-tool approval."
        ),
    ),
    output: Path = typer.Option(
        ...,
        "--out",
        dir_okay=False,
        help="New mode-0600 JSON report; existing files are never overwritten.",
    ),
    allow_provider_calls: bool = typer.Option(
        False,
        "--allow-provider-calls",
        help="Explicitly permit billable or subscription-backed model calls.",
    ),
) -> None:
    """Prove normal chat, research, and hand-off without a target."""
    import asyncio
    import json

    from pikvm_agent.harness.assistant_conformance import (
        AssistantAcceptanceReport,
        run_assistant_acceptance,
        write_assistant_acceptance_report,
    )
    from pikvm_agent.harness.config import (
        build_model_budget_policy,
        build_model_pool,
        check_provider_prerequisites,
        load_harness_settings,
    )
    from pikvm_agent.harness.general_tools import (
        McpServerConnection,
        McpToolBroker,
    )

    if not allow_provider_calls:
        typer.echo(
            "Assistant conformance refused: add --allow-provider-calls to "
            "explicitly permit model-provider and public research traffic.",
            err=True,
        )
        raise typer.Exit(2)
    if output.exists():
        typer.echo(
            "Assistant conformance refused: --out already exists.",
            err=True,
        )
        raise typer.Exit(2)

    settings = load_harness_settings(config)
    readiness = check_provider_prerequisites(settings)
    if provider not in readiness:
        typer.echo(
            f"Assistant conformance refused: unknown provider: {provider}.",
            err=True,
        )
        raise typer.Exit(2)
    if not readiness[provider]["ready"]:
        typer.echo(
            "Assistant conformance refused: provider is not ready: "
            f"{readiness[provider].get('error', 'unknown')}.",
            err=True,
        )
        raise typer.Exit(2)

    pool = build_model_pool(settings)
    tools = McpToolBroker(
        [
            McpServerConnection(
                name=name,
                transport=spec.transport,
                command=spec.command,
                args=tuple(spec.args),
                cwd=spec.cwd,
                inherited_env=tuple(spec.inherited_env),
                url=spec.url,
                header_env=dict(spec.header_env),
                allowed_tools=frozenset(spec.allowed_tools),
                read_only_tools=frozenset(spec.read_only_tools),
                timeout_s=spec.timeout_s,
            )
            for name, spec in settings.assistant_tools.items()
        ]
    )

    async def run() -> AssistantAcceptanceReport:
        await tools.start()
        try:
            return await run_assistant_acceptance(
                models=pool,
                tools=tools,
                provider=provider,
                budget_policy=build_model_budget_policy(settings),
                case_ids=set(cases) if cases else None,
            )
        finally:
            await tools.close()
            closed: set[int] = set()
            for configured_provider in pool.providers.values():
                if id(configured_provider) in closed:
                    continue
                closed.add(id(configured_provider))
                close = getattr(configured_provider, "aclose", None)
                if close is not None:
                    await close()

    try:
        report = asyncio.run(run())
        write_assistant_acceptance_report(output, report)
    except Exception as exc:
        typer.echo(
            f"Assistant conformance failed: {type(exc).__name__}.",
            err=True,
        )
        raise typer.Exit(2)

    typer.echo(
        json.dumps(
            {
                "suite": report.suite,
                "provider": report.provider,
                "computer_target_contacted": report.computer_target_contacted,
                "cases_passed": report.cases_passed,
                "cases_requested": report.cases_requested,
                "provider_calls": report.provider_calls,
                "tool_requests": report.tool_requests,
                "tool_calls": report.tool_calls,
                "consequential_tool_executions": (
                    report.consequential_tool_executions
                ),
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "report": str(output.expanduser().resolve()),
            },
            indent=2,
        )
    )
    if not report.passed:
        raise typer.Exit(1)


@harness_app.command("analyze-transcript")
def harness_analyze_transcript(
    transcript: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Claude Code JSONL conversation export.",
    ),
    findings: bool = typer.Option(
        False,
        "--findings",
        help="Include privacy-preserving per-burst findings.",
    ),
) -> None:
    """Turn a prior direct-MCP conversation into regression metrics."""
    import json

    from pikvm_agent.harness.transcript import analyze_claude_transcript

    report = analyze_claude_transcript(transcript)
    body = report.model_dump(mode="json")
    if not findings:
        body.pop("findings", None)
    typer.echo(json.dumps(body, indent=2))


def _build_ocr_benchmark_provider(
    provider: str,
    jobs: int,
) -> tuple[Any, int, bool]:
    """Build one benchmark-owned OCR provider without exposing credentials."""

    precise = False
    if provider == "tesseract":
        from pikvm_agent.vision.tesseract_ocr import (
            TesseractOcrProvider,
            tesseract_available,
        )

        if not tesseract_available():
            typer.echo(
                "OCR benchmark requires the system tesseract binary",
                err=True,
            )
            raise typer.Exit(1)
        return TesseractOcrProvider(), jobs, precise
    if provider not in {"paddleocr", "hybrid"}:
        typer.echo(
            "--provider must be tesseract, paddleocr, or hybrid",
            err=True,
        )
        raise typer.Exit(2)

    from pikvm_agent.vision.paddleocr_client import (
        PaddleOCRProvider,
        paddleocr_available,
    )

    if not paddleocr_available():
        typer.echo(
            "PaddleOCR benchmark requires the optional vision dependencies",
            err=True,
        )
        raise typer.Exit(1)
    if provider == "paddleocr":
        if jobs != 1:
            typer.echo(
                "PaddleOCR uses one worker because one model instance is shared."
            )
        return PaddleOCRProvider(), 1, precise

    from pikvm_agent.vision.hybrid_ocr import HybridOcrProvider
    from pikvm_agent.vision.tesseract_ocr import (
        TesseractOcrProvider,
        tesseract_available,
    )

    if not tesseract_available():
        typer.echo(
            "Hybrid OCR benchmark requires the system tesseract binary",
            err=True,
        )
        raise typer.Exit(1)
    return (
        HybridOcrProvider(
            TesseractOcrProvider(),
            PaddleOCRProvider(),
        ),
        jobs,
        True,
    )


@harness_app.command("ocr-benchmark")
def harness_ocr_benchmark(
    output: Path = typer.Option(
        ...,
        "--out",
        help="Output directory for images, private ground truth, failures, and report.",
    ),
    cases: int = typer.Option(1_000, min=1, help="Number of blind examples."),
    seed: int = typer.Option(104_729, help="Deterministic corpus seed."),
    evaluation_seed: int = typer.Option(
        65_537,
        help="Independent seed used to shuffle the blind evaluation order.",
    ),
    jobs: int = typer.Option(4, min=1, max=32, help="Concurrent OCR processes."),
    provider: str = typer.Option(
        "tesseract",
        "--provider",
        help="OCR provider: tesseract, paddleocr, or precise hybrid.",
    ),
) -> None:
    """Run the deterministic randomized blind OCR release benchmark."""
    import asyncio
    import json

    from pikvm_agent.harness.ocr_blind_benchmark import (
        run_closing_blind_ocr_benchmark,
    )
    ocr_provider, jobs, precise = _build_ocr_benchmark_provider(provider, jobs)
    last_printed = 0

    def progress(done: int, total: int) -> None:
        nonlocal last_printed
        if done == total or done - last_printed >= max(10, total // 20):
            typer.echo(f"OCR blind test: {done}/{total}")
            last_printed = done

    report = asyncio.run(
        run_closing_blind_ocr_benchmark(
            ocr_provider,
            provider_name=provider,
            output_dir=output,
            count=cases,
            corpus_seed=seed,
            evaluation_seed=evaluation_seed,
            jobs=jobs,
            precise=precise,
            progress=progress,
        )
    )
    typer.echo(
        json.dumps(
            {
                "cases": report.cases,
                "exact_rate": report.exact_rate,
                "normalized_exact_rate": report.normalized_exact_rate,
                "mean_character_error_rate": report.mean_character_error_rate,
                "p95_latency_ms": report.p95_latency_ms,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "throughput_images_per_second": report.throughput_images_per_second,
                "provider_diagnostics": report.provider_diagnostics,
                "report": str((output / "report.json").resolve()),
            },
            indent=2,
        )
    )


@harness_app.command("ocr-spacing-benchmark")
def harness_ocr_spacing_benchmark(
    output: Path = typer.Option(
        ...,
        "--out",
        help="Output directory for opaque images, private truth, failures, and report.",
    ),
    cases: int = typer.Option(
        1_000,
        min=8,
        help="Balanced blind examples; must be divisible by eight.",
    ),
    seed: int = typer.Option(104_729, help="Deterministic corpus seed."),
    evaluation_seed: int = typer.Option(
        65_537,
        help="Independent seed used to shuffle the blind evaluation order.",
    ),
    jobs: int = typer.Option(4, min=1, max=32, help="Concurrent OCR processes."),
    shard_index: int = typer.Option(
        0,
        "--shard-index",
        min=0,
        help="Zero-based shard to execute.",
    ),
    shard_count: int = typer.Option(
        1,
        "--shard-count",
        min=1,
        help="Deterministic strided shard count.",
    ),
    provider: str = typer.Option(
        "tesseract",
        "--provider",
        help="OCR provider: tesseract, paddleocr, or precise hybrid.",
    ),
) -> None:
    """Blind-test silent single-space versus doubled-space verification."""
    import asyncio
    import json

    from pikvm_agent.harness.ocr_spacing_benchmark import (
        run_closing_blind_spacing_benchmark,
    )

    ocr_provider, jobs, _ = _build_ocr_benchmark_provider(provider, jobs)
    last_printed = 0

    def progress(done: int, total: int) -> None:
        nonlocal last_printed
        if done == total or done - last_printed >= max(10, total // 20):
            typer.echo(f"OCR spacing blind test: {done}/{total}")
            last_printed = done

    try:
        report = asyncio.run(
            run_closing_blind_spacing_benchmark(
                ocr_provider,
                provider_name=provider,
                output_dir=output,
                count=cases,
                corpus_seed=seed,
                evaluation_seed=evaluation_seed,
                jobs=jobs,
                shard_index=shard_index,
                shard_count=shard_count,
                progress=progress,
            )
        )
    except ValueError as exc:
        typer.echo(f"OCR spacing benchmark refused: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(
        json.dumps(
            {
                "cases": report.cases,
                "corpus_cases": report.corpus_cases,
                "shard_index": report.shard_index,
                "shard_count": report.shard_count,
                "controls": report.controls,
                "corruptions": report.corruptions,
                "control_verified_rate": report.control_verified_rate,
                "corruption_detection_rate": report.corruption_detection_rate,
                "false_verified_corruptions": (
                    report.false_verified_corruptions
                ),
                "false_spacing_alarms": report.false_spacing_alarms,
                "screen_exact_candidate_rate": (
                    report.screen_exact_candidate_rate
                ),
                "p95_latency_ms": report.p95_latency_ms,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "release_gate_passed": report.release_gate_passed,
                "release_gate_failures": report.release_gate_failures,
                "report": str((output / "report.json").resolve()),
            },
            indent=2,
        )
    )


@harness_app.command("ocr-spacing-merge")
def harness_ocr_spacing_merge(
    reports: list[Path] = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Complete set of spacing benchmark shard report.json files.",
    ),
    output: Path = typer.Option(
        ...,
        "--out",
        help="Fresh output directory for the merged report.",
    ),
) -> None:
    """Validate and merge a complete blind OCR spacing shard set."""
    import json

    from pikvm_agent.harness.ocr_spacing_benchmark import (
        merge_spacing_shard_reports,
    )

    try:
        report = merge_spacing_shard_reports(reports, output_dir=output)
    except ValueError as exc:
        typer.echo(f"OCR spacing merge refused: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(
        json.dumps(
            {
                "cases": report.cases,
                "source_shards": report.source_shards,
                "control_verified_rate": report.control_verified_rate,
                "corruption_detection_rate": report.corruption_detection_rate,
                "false_verified_corruptions": (
                    report.false_verified_corruptions
                ),
                "false_spacing_alarms": report.false_spacing_alarms,
                "release_gate_passed": report.release_gate_passed,
                "release_gate_failures": report.release_gate_failures,
                "report": str((output / "report.json").resolve()),
            },
            indent=2,
        )
    )


@harness_app.command("run-metrics")
def harness_run_metrics(
    state: Path = typer.Option(
        ...,
        "--state",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness SQLite checkpoint database.",
    ),
    run_id: str = typer.Option(..., "--run-id", help="Durable harness run ID."),
    human_baseline_ms: int | None = typer.Option(
        None,
        "--human-baseline-ms",
        min=1,
        help=(
            "Optional successful-human wall time for the same task and "
            "environment."
        ),
    ),
) -> None:
    """Report critical-path, model, HID, and optional human-relative latency."""
    import asyncio

    from pikvm_agent.harness.agent_store import SqliteRunStore
    from pikvm_agent.harness.performance import summarize_run_performance

    run = asyncio.run(SqliteRunStore(state).get(run_id))
    typer.echo(
        summarize_run_performance(
            run,
            human_baseline_ms=human_baseline_ms,
        ).model_dump_json(indent=2)
    )


@harness_app.command("scorecard")
def harness_scorecard(
    manifest: Path = typer.Option(
        Path("bench/scorecard.yaml"),
        "--manifest",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Scorecard manifest naming checked JSON benchmark reports.",
    ),
    document: Path = typer.Option(
        Path("bench/README.md"),
        "--document",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Markdown document containing the generated scorecard markers.",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Exit non-zero instead of updating when evidence and Markdown drift.",
    ),
) -> None:
    """Generate or verify the public benchmark scorecard from JSON evidence."""
    from pikvm_agent.harness.scorecard import update_scorecard

    try:
        current = update_scorecard(
            manifest_path=manifest,
            document_path=document,
            check=check,
        )
    except (OSError, ValueError) as exc:
        typer.echo(f"Scorecard refused: {exc}", err=True)
        raise typer.Exit(2)
    if not current:
        typer.echo(
            "Scorecard drift detected; run without --check to update it.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo("Scorecard evidence is current." if check else "Scorecard updated.")


@harness_app.command("inspect-wheel")
def harness_inspect_wheel(
    wheel: Path = typer.Option(
        ...,
        "--wheel",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Built pikvm-agent wheel to verify before signing or publishing.",
    ),
) -> None:
    """Verify wheel integrity, CLI wiring, and installed operator assets."""
    import json

    from pikvm_agent.harness.package_acceptance import (
        WheelAcceptanceError,
        inspect_operator_wheel,
    )

    try:
        report = inspect_operator_wheel(wheel)
    except (OSError, WheelAcceptanceError) as exc:
        typer.echo(f"Wheel rejected: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(report, indent=2, sort_keys=True))


@harness_app.command("verify-office-artifact")
def harness_verify_office_artifact(
    suite: Path = typer.Option(
        ...,
        "--suite",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Versioned Office acceptance-suite YAML.",
    ),
    task_id: str = typer.Option(
        ...,
        "--task-id",
        help="Stable task ID inside the acceptance suite.",
    ),
    artifact: Path = typer.Option(
        ...,
        "--artifact",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Saved DOCX/XLSX bytes captured from the test machine.",
    ),
) -> None:
    """Verify a saved Office artifact independently of the model's done claim."""
    from pikvm_agent.harness.office_acceptance import (
        load_office_suite,
        verify_office_artifact,
    )

    try:
        contract = load_office_suite(suite)
        task = contract.task(task_id)
        result = verify_office_artifact(task.artifact, artifact.read_bytes())
    except (KeyError, OSError, ValueError) as exc:
        typer.echo(f"Office artifact verification refused: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(result.model_dump_json(indent=2))
    if not result.passed:
        raise typer.Exit(1)


@harness_app.command("office-case")
def harness_office_case(
    vnc: str = typer.Option(
        ...,
        "--vnc",
        envvar="PIKVM_LAB_VNC",
        help="Runtime-only VNC endpoint for the disposable test machine.",
    ),
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Secret-free model-provider and route configuration.",
    ),
    suite: Path = typer.Option(
        ...,
        "--suite",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Versioned Office acceptance-suite YAML.",
    ),
    task_id: str = typer.Option(..., "--task-id"),
    output: Path = typer.Option(
        ...,
        "--output",
        help="New result directory for the run, logs, and captured artifact.",
    ),
    artifact_url: str | None = typer.Option(
        None,
        "--artifact-url",
        help=(
            "Caller-owned HTTPS URL for the Windows observer binary. "
            "Required unless --skip-provision is used."
        ),
    ),
    skip_provision: bool = typer.Option(
        False,
        "--skip-provision",
        help=(
            "Reuse the helper already installed at "
            "C:/PiKVM-Harness/observer.exe; restart it with this run's fresh "
            "artifact path instead of downloading it."
        ),
    ),
    keymap: str = typer.Option("en-us", "--keymap"),
    password_env: str = typer.Option(
        "PIKVM_LAB_VNC_PASSWORD",
        "--password-env",
    ),
    username_env: str = typer.Option(
        "PIKVM_LAB_VNC_USERNAME",
        "--username-env",
    ),
    max_cycles: int = typer.Option(
        100,
        "--max-cycles",
        min=1,
        max=10_000,
        help="Maximum bounded managed-harness continuation slices.",
    ),
    max_run_time_s: float = typer.Option(
        3_600,
        "--max-run-time-s",
        min=1,
        max=86_400,
    ),
) -> None:
    """Run one visible Office task and pass only on saved-artifact proof."""
    import asyncio

    from pikvm_agent.harness.office_runner import run_live_office_case

    try:
        result = asyncio.run(
            run_live_office_case(
                endpoint=vnc,
                harness_config=config,
                suite_path=suite,
                task_id=task_id,
                output_dir=output,
                artifact_url=artifact_url,
                skip_provision=skip_provision,
                keymap=keymap,
                password=os.environ.get(password_env),
                username=os.environ.get(username_env),
                max_continuation_cycles=max_cycles,
                max_run_time_s=max_run_time_s,
                status_sink=typer.echo,
            )
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"Office case failed: {exc}", err=True)
        raise typer.Exit(2)
    typer.echo(result.model_dump_json(indent=2))
    if result.status != "passed":
        raise typer.Exit(1)


@harness_app.command("screenspot-pro")
def harness_screenspot_pro(
    config: Path = typer.Option(
        ...,
        "--config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Harness YAML containing the selected model provider.",
    ),
    provider: str = typer.Option(
        ...,
        "--provider",
        help="Configured provider name to evaluate; no route fallback is hidden.",
    ),
    verifier_provider: str | None = typer.Option(
        None,
        "--verifier-provider",
        help=(
            "Optional independent configured provider that verifies the "
            "candidate crosshair. Suggested corrections are diagnostic unless "
            "--verifier-mode correct is explicitly selected."
        ),
    ),
    verifier_mode: str = typer.Option(
        "veto",
        "--verifier-mode",
        help=(
            "veto lets the verifier accept or abstain; correct explicitly "
            "enables experimental replacement coordinates."
        ),
    ),
    dataset: Path = typer.Option(
        ...,
        "--dataset",
        exists=True,
        file_okay=False,
        readable=True,
        help="Official ScreenSpot-Pro root containing annotations/ and images/.",
    ),
    output: Path = typer.Option(
        ...,
        "--out",
        help="Output directory for the durable JSON report.",
    ),
    suite_revision: str = typer.Option(
        ...,
        "--suite-revision",
        help="Pinned upstream ScreenSpot-Pro evaluator git revision.",
    ),
    dataset_revision: str = typer.Option(
        ...,
        "--dataset-revision",
        help="Pinned official dataset revision.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Deterministic subset size; omit for every discovered case.",
    ),
    seed: int = typer.Option(104_729, help="Deterministic case-order seed."),
    jobs: int = typer.Option(
        1,
        min=1,
        max=32,
        help="Concurrent provider calls.",
    ),
) -> None:
    """Run a live configured model on official ScreenSpot-Pro images."""
    import asyncio
    import json

    from pikvm_agent.harness.config import (
        build_model_pool,
        check_provider_prerequisites,
        load_harness_settings,
    )
    from pikvm_agent.harness.public_benchmarks import run_screenspot_pro

    settings = load_harness_settings(config)
    normalized_verifier_mode = verifier_mode.strip().lower()
    if normalized_verifier_mode not in {"veto", "correct"}:
        typer.echo("--verifier-mode must be veto or correct", err=True)
        raise typer.Exit(2)
    readiness = check_provider_prerequisites(settings)
    selected_names = [provider]
    if verifier_provider:
        selected_names.append(verifier_provider)
    for selected_name in selected_names:
        if selected_name not in readiness:
            typer.echo(f"unknown provider: {selected_name}", err=True)
            raise typer.Exit(2)
        if not readiness[selected_name]["ready"]:
            typer.echo(
                f"provider {selected_name} is not ready: "
                f"{readiness[selected_name].get('error', 'unknown')}",
                err=True,
            )
            raise typer.Exit(2)
    pool = build_model_pool(settings)
    selected = pool.providers[provider]
    verifier = (
        pool.providers[verifier_provider] if verifier_provider else None
    )

    async def run() -> object:
        try:
            return await run_screenspot_pro(
                selected,
                verifier_provider=verifier,
                verifier_mode=normalized_verifier_mode,  # type: ignore[arg-type]
                dataset_dir=dataset,
                output_dir=output,
                suite_revision=suite_revision,
                dataset_revision=dataset_revision,
                limit=limit,
                seed=seed,
                jobs=jobs,
            )
        finally:
            close = getattr(selected, "aclose", None)
            if close is not None:
                await close()
            if verifier is not None and verifier is not selected:
                verifier_close = getattr(verifier, "aclose", None)
                if verifier_close is not None:
                    await verifier_close()

    report = asyncio.run(run())
    typer.echo(
        json.dumps(
            {
                "suite": report.suite,
                "cases_evaluated": report.cases_evaluated,
                "initial_correct": report.initial_correct,
                "initial_accuracy": report.initial_accuracy,
                "verifier_mode": report.verifier_mode,
                "actionable_cases": report.actionable_cases,
                "abstained_cases": report.abstained_cases,
                "actionable_accuracy": report.actionable_accuracy,
                "correct": report.correct,
                "accuracy": report.accuracy,
                "model_calls": report.model_calls,
                "model_active_ms": report.model_active_ms,
                "usage_totals": report.usage_totals,
                "model_errors": report.model_errors,
                "median_latency_ms": report.median_latency_ms,
                "p95_latency_ms": report.p95_latency_ms,
                "evaluation_wall_ms": report.evaluation_wall_ms,
                "throughput_cases_per_second": report.throughput_cases_per_second,
                "report": str((output / "report.json").resolve()),
            },
            indent=2,
        )
    )


@harness_app.command("suite-inventory")
def harness_suite_inventory(
    suite: str = typer.Option(
        ...,
        "--suite",
        help="osworld-verified or windows-agent-arena",
    ),
    repo: Path = typer.Option(
        ...,
        "--repo",
        exists=True,
        file_okay=False,
        readable=True,
        help="Pinned upstream repository checkout.",
    ),
    revision: str = typer.Option(
        ...,
        "--revision",
        help="Pinned upstream git revision represented by the checkout.",
    ),
    output: Path = typer.Option(
        ...,
        "--out",
        help="Destination for the complete normalized inventory JSON.",
    ),
    split: str = typer.Option(
        "test_all.json",
        "--split",
        help="Official split filename beneath the suite evaluation directory.",
    ),
) -> None:
    """Validate and preserve a pinned OSWorld or Windows Agent Arena inventory."""
    import json

    from pikvm_agent.harness.public_desktop_suites import (
        discover_desktop_suite,
        verify_checkout_revision,
    )

    if suite not in {"osworld-verified", "windows-agent-arena"}:
        typer.echo(f"unsupported suite: {suite}", err=True)
        raise typer.Exit(2)
    verify_checkout_revision(repo, revision)
    inventory = discover_desktop_suite(
        suite,
        repo,
        revision=revision,
        split=split,
    )
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        inventory.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    typer.echo(
        json.dumps(
            {
                "suite": inventory.suite,
                "revision": inventory.revision,
                "tasks_discovered": inventory.tasks_discovered,
                "domains": inventory.domains,
                "integrity_warnings": inventory.integrity_warnings,
                "report": str(output),
            },
            indent=2,
        )
    )


@harness_app.command("osworld-case")
def harness_osworld_case(
    repo: Path = typer.Option(
        ...,
        "--repo",
        exists=True,
        file_okay=False,
        readable=True,
        help="Pinned official OSWorld checkout.",
    ),
    revision: str = typer.Option(..., "--revision", help="Pinned OSWorld git revision."),
    qcow: Path = typer.Option(
        ...,
        "--qcow",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Pinned official OSWorld VM image.",
    ),
    docker_image: str = typer.Option(
        ...,
        "--docker-image",
        help="Explicit OSWorld container image tag or digest.",
    ),
    task_id: str = typer.Option(..., "--task-id", help="Task from official test_all.json."),
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Provider-neutral harness configuration.",
    ),
    output: Path = typer.Option(..., "--out", help="Durable trajectory/report directory."),
    startup_timeout_s: float = typer.Option(
        900,
        "--startup-timeout",
        min=30,
        help="Maximum wait for the unaccelerated official VM.",
    ),
    max_cycles: int = typer.Option(
        20,
        "--max-cycles",
        min=1,
        max=100,
        help="Maximum checkpointed harness advances.",
    ),
    max_run_time_s: float = typer.Option(
        900,
        "--max-run-seconds",
        min=30,
        max=3600,
        help="Maximum model/harness wall time before a scored abort.",
    ),
    interactive_approvals: bool = typer.Option(
        False,
        "--interactive-approvals",
        help=(
            "Keep the isolated VM alive at approval gates and ask the local "
            "operator to approve or reject each exact action."
        ),
    ),
    operator_console: bool = typer.Option(
        False,
        "--operator-console",
        help=(
            "Serve the authenticated live UI for this benchmark and wait for "
            "approval decisions made in that UI."
        ),
    ),
) -> None:
    """Run one official OSWorld case through harness, MCP, daemon and evaluator."""
    import asyncio
    import json

    from pikvm_agent.harness.osworld_runner import run_osworld_case

    if interactive_approvals and operator_console:
        typer.echo(
            "--interactive-approvals and --operator-console are mutually exclusive",
            err=True,
        )
        raise typer.Exit(2)

    approval_resolver = None
    if interactive_approvals:

        async def resolve_interactive_approval(run: Any) -> dict[str, str]:
            pending = run.pending_approval or {}
            proposed = pending.get("proposed_action") or {}
            raw_actions = proposed.get("actions") or []
            visible_actions: list[dict[str, Any]] = []
            for raw_action in raw_actions:
                action = dict(raw_action)
                if action.get("secret") and "text" in action:
                    action["text"] = "<redacted secret>"
                visible_actions.append(action)
            typer.echo("\nOperator approval required in resettable OSWorld VM:")
            typer.echo(
                json.dumps(
                    {
                        "approval_id": pending.get("approval_id"),
                        "risk": pending.get("risk"),
                        "reason": pending.get("reason"),
                        "actions": visible_actions,
                        "idempotency_key": proposed.get("idempotency_key"),
                        "digest": proposed.get("digest"),
                    },
                    indent=2,
                )
            )
            approved = await asyncio.to_thread(
                typer.confirm,
                "Approve this exact isolated-VM action?",
                default=False,
            )
            return {
                "type": "approve" if approved else "reject",
                "reason": (
                    "approved interactively for the isolated OSWorld benchmark"
                    if approved
                    else "rejected interactively by the OSWorld operator"
                ),
            }

        approval_resolver = resolve_interactive_approval

    report = asyncio.run(
        run_osworld_case(
            repo=repo,
            suite_revision=revision,
            qcow=qcow,
            docker_image=docker_image,
            task_id=task_id,
            harness_config=config,
            output_dir=output,
            startup_timeout_s=startup_timeout_s,
            max_cycles=max_cycles,
            max_run_time_s=max_run_time_s,
            approval_resolver=approval_resolver,
            operator_console=operator_console,
        )
    )
    typer.echo(report.model_dump_json(indent=2))


@lab_app.command("up")
def lab_up(
    vnc: str = typer.Option(
        ...,
        "--vnc",
        envvar="PIKVM_LAB_VNC",
        help="RFB endpoint supplied at runtime (host:port).",
    ),
    root: Path = typer.Option(
        Path(".pikvm-lab"),
        "--root",
        help="Lab-only config, state, traces, and generated MCP file.",
    ),
    adapter_port: int = typer.Option(47640, help="Local PiKVM API emulator port."),
    daemon_port: int = typer.Option(47641, help="Isolated agent daemon port."),
    harness_port: int = typer.Option(
        47642,
        help="Visible managed-harness UI/API port.",
    ),
    harness_config: Path | None = typer.Option(
        None,
        "--harness-config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help=(
            "Optional secret-free provider/routes config. Lab target, bind, "
            "and state paths are always replaced with isolated values."
        ),
    ),
    keymap: str = typer.Option("en-us", help="Target keymap advertised to the MCP runtime."),
    keyboard_profile: str = typer.Option(
        "generic",
        "--keyboard-profile",
        help="Runtime-only VNC compatibility profile: generic or windows.",
    ),
    password_env: str = typer.Option(
        "PIKVM_LAB_VNC_PASSWORD", help="Environment variable holding the VNC password."
    ),
    username_env: str = typer.Option(
        "PIKVM_LAB_VNC_USERNAME", help="Environment variable holding the VNC username."
    ),
) -> None:
    """Start an isolated VNC adapter, daemon, and visible managed harness."""
    from pikvm_agent.harness.lab import LabPorts, run_lab

    # Keep the virtualenv launcher path intact. Resolving the symlink would
    # produce the base interpreter, which may not have this package installed.
    executable = os.path.abspath(sys.executable)
    try:
        run_lab(
            endpoint=vnc,
            root=root,
            ports=LabPorts(
                adapter=adapter_port,
                daemon=daemon_port,
                harness=harness_port,
            ),
            executable=executable,
            keymap=keymap,
            password_env=password_env,
            username_env=username_env,
            keyboard_profile=keyboard_profile,
            harness_config=harness_config,
        )
    except KeyboardInterrupt:
        typer.echo("PiKVM MCP lab stopped.")
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"lab failed: {exc}", err=True)
        raise typer.Exit(1)


@lab_app.command("in-guest-up")
def in_guest_lab_up(
    endpoint: str = typer.Option(
        ...,
        "--endpoint",
        envvar="PIKVM_LAB_IN_GUEST",
        help="OSWorld-compatible in-guest HTTP endpoint supplied at runtime.",
    ),
    root: Path = typer.Option(
        Path(".pikvm-in-guest-lab"),
        "--root",
        help="Lab-only config, state, traces, and generated MCP file.",
    ),
    adapter_port: int = typer.Option(47640, help="Local PiKVM API emulator port."),
    daemon_port: int = typer.Option(47641, help="Isolated agent daemon port."),
    harness_port: int = typer.Option(
        47642,
        help="Visible managed-harness UI/API port.",
    ),
    harness_config: Path | None = typer.Option(
        None,
        "--harness-config",
        envvar="PIKVM_HARNESS_CONFIG",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional secret-free provider/routes config.",
    ),
    keymap: str = typer.Option("en-us", help="Target keymap advertised to the MCP runtime."),
) -> None:
    """Start a visible managed lab over a public benchmark's in-guest server."""
    from pikvm_agent.harness.lab import LabPorts, RunningLab

    executable = os.path.abspath(sys.executable)
    try:
        with RunningLab(
            endpoint=endpoint,
            root=root,
            ports=LabPorts(
                adapter=adapter_port,
                daemon=daemon_port,
                harness=harness_port,
            ),
            executable=executable,
            keymap=keymap,
            transport="in-guest",
            harness_config=harness_config,
            start_harness=True,
        ) as lab:
            assert lab.assets is not None
            typer.echo("In-guest PiKVM MCP lab is ready (production daemon untouched).")
            typer.echo(f"  adapter:   http://127.0.0.1:{adapter_port}")
            typer.echo(f"  daemon:    http://127.0.0.1:{daemon_port}")
            typer.echo(f"  harness:   http://127.0.0.1:{harness_port}/app/")
            typer.echo(f"  config:    {lab.assets.config}")
            typer.echo(f"  harness config: {lab.assets.harness_config}")
            typer.echo(f"  Claude/Gemini MCP: {lab.assets.mcp_config}")
            typer.echo(f"  Codex MCP:         {lab.assets.codex_mcp_config}")
            typer.echo(f"  OpenCode MCP:      {lab.assets.opencode_mcp_config}")
            typer.echo("Press Ctrl+C to stop the isolated lab.")
            assert (
                lab.adapter is not None
                and lab.daemon is not None
                and lab.harness is not None
            )
            while (
                lab.adapter.poll() is None
                and lab.daemon.poll() is None
                and lab.harness.poll() is None
            ):
                import time

                time.sleep(0.5)
            raise RuntimeError("a lab process exited unexpectedly")
    except KeyboardInterrupt:
        typer.echo("In-guest PiKVM MCP lab stopped.")
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"lab failed: {exc}", err=True)
        raise typer.Exit(1)


@app.command(name="panic-stop")
def panic_stop(
    daemon_url: str | None = typer.Option(
        None,
        "--daemon",
        envvar="PIKVM_AGENT_DAEMON",
        help=(
            "Explicitly selected agent daemon URL. Prefer the "
            "PIKVM_AGENT_DAEMON environment variable."
        ),
    ),
) -> None:
    """EMERGENCY STOP — halt the explicitly selected daemon without an agent."""
    import httpx

    from pikvm_agent.config import require_daemon_url

    try:
        url = require_daemon_url(daemon_url)
    except ValueError as exc:
        typer.echo(f"panic-stop refused: {exc}", err=True)
        raise typer.Exit(2)
    try:
        resp = httpx.post(f"{url}/panic-stop", timeout=10.0)
        resp.raise_for_status()
        result = resp.json()
        stopped = result.get("stopped", [])
        machine = result.get("machine") or {}
        target = " · ".join(
            str(value)
            for value in (machine.get("alias"), machine.get("fingerprint"))
            if value
        )
        if not result.get("ok"):
            typer.echo(
                "panic-stop incomplete: the selected daemon did not confirm "
                f"HID quiescence{f' ({target})' if target else ''}",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(
            f"PANIC STOP confirmed — halted {len(stopped)} session(s)"
            f"{f' · {target}' if target else ''}."
        )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            "panic-stop failed for the explicitly selected daemon: "
            f"{type(exc).__name__}",
            err=True,
        )
        raise typer.Exit(1)


_CONFIG_TEMPLATE = """\
# PiKVM Agent config (XDG). Secrets are NOT stored here — set them as env vars
# (the desktop app forwards them to the daemon automatically).

daemon:
  listen: "127.0.0.1:47615"

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
  base_url: "http://127.0.0.1:47625"
  health_url: "http://127.0.0.1:47625/probe"
  timeout_s: 60
  command:
    - "/home/kieran/dev/OmniParser/.venv/bin/python"
    - "-m"
    - "omniparserserver"
    - "--port"
    - "47625"
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
        using_paddle = False
        if paddleocr_available():
            from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

            ocr = PaddleOCRProvider(lang=cfg.ocr.lang, device=cfg.ocr.device)
            using_paddle = True
        elif tesseract_available():
            ocr = TesseractOcrProvider()
        else:
            typer.echo("No file OCR engine available (install tesseract or the [vision] extra).", err=True)
            raise typer.Exit(code=1)

        try:
            ocr_result = await ocr.ocr(screenshot)
        except RuntimeError:
            if not using_paddle or not tesseract_available():
                raise
            typer.echo(
                "PaddleOCR failed at runtime; using Tesseract for this smoke test.",
                err=True,
            )
            ocr = TesseractOcrProvider()
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
