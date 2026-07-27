"""Target-free cross-browser acceptance for the first-party chat workspace."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

import uvicorn

from pikvm_agent.harness.ui_fixture import build_fixture_app


SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")
TIMELINE_TASK = (
    "Audit a long provider name, exact MCP arguments, sustained frame "
    "updates, a 1,200-event timeline, and 200% browser reflow"
)
APPROVAL_TASK = "Review a Teams message before the final send input"
DIRECT_TASK = "Inspect a direct Claude computer-control trace"
APPROVAL_TEXT = "Quarterly figures are attached for your review."
PROGRESS_TASK = "Audit singular task progress ownership"


class BrowserAuditDependencyError(RuntimeError):
    """Raised when the optional browser-audit dependency is unavailable."""


class BrowserAuditFailure(RuntimeError):
    """Raised when the local fixture cannot provide a trustworthy audit."""


def parse_browser_names(value: str | Sequence[str]) -> tuple[str, ...]:
    """Return a stable, unique browser matrix from CLI or Python input."""

    raw = value.split(",") if isinstance(value, str) else value
    names = tuple(str(name).strip().lower() for name in raw if str(name).strip())
    if not names:
        raise ValueError("at least one browser is required")
    unknown = [name for name in names if name not in SUPPORTED_BROWSERS]
    if unknown:
        raise ValueError(
            "unsupported browser(s): "
            + ", ".join(unknown)
            + "; choose from "
            + ", ".join(SUPPORTED_BROWSERS)
        )
    if len(set(names)) != len(names):
        raise ValueError("browser names must be unique")
    return names


def _git_output(*arguments: str) -> str | None:
    """Run one read-only git query against the source checkout, if available."""

    roots = tuple(
        dict.fromkeys(
            (
                Path.cwd(),
                Path(__file__).resolve().parents[2],
            )
        )
    )
    for root in roots:
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _current_commit() -> str | None:
    return _git_output("rev-parse", "HEAD")


def _source_worktree_clean() -> bool | None:
    output = _git_output("status", "--porcelain")
    return None if output is None else not output


def write_browser_audit_report(
    path: Path,
    report: dict[str, Any],
) -> None:
    """Create private evidence and refuse accidental replacement."""

    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError("browser audit output already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _clean_failure(cause: BaseException) -> str:
    """Keep public evidence useful without retaining local paths or tokens."""

    lines = [
        line.strip()
        for line in str(cause).splitlines()
        if line.strip()
    ]
    message = lines[0] if lines else type(cause).__name__
    diagnostic = next(
        (
            line
            for line in lines[1:]
            if "missing" in line.lower()
            or "error while loading shared libraries" in line.lower()
        ),
        "",
    )
    if diagnostic:
        message = f"{message}: {diagnostic}"
    message = re.sub(r"/(?:home|tmp|run)/[^\s]+", "<local-path>", message)
    return message[:480]


def _clean_messages(values: Sequence[str]) -> list[str]:
    """Sanitize browser diagnostics before they enter durable evidence."""

    return [_clean_failure(RuntimeError(value)) for value in values[:20]]


def _is_fixture_request(url: str, port: int) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"blob", "data"}:
        return True
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname == "127.0.0.1"
        and parsed.port == port
    )


@contextmanager
def _fixture_server(
    *,
    prefill_events: int,
    event_interval_ms: int,
) -> Iterator[tuple[str, str, int]]:
    """Serve the authenticated fixture on an OS-selected loopback port."""

    token = secrets.token_hex(32)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2_048)
    port = int(listener.getsockname()[1])
    origin = f"http://127.0.0.1:{port}"
    app = build_fixture_app(
        access_token=token,
        origin=origin,
        prefill_events=prefill_events,
        event_interval_ms=event_interval_ms,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            timeout_graceful_shutdown=3,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="pikvm-browser-audit-fixture",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.025)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        raise BrowserAuditFailure("loopback UI fixture did not start")
    try:
        yield origin, token, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise BrowserAuditFailure("loopback UI fixture did not stop")


def _overflow_measurements(page: Any) -> dict[str, int]:
    return page.evaluate(
        """() => {
          const overflow = (element) =>
            element ? Math.max(0, element.scrollWidth - element.clientWidth) : 0;
          const documentElement = document.documentElement;
          const conversation = document.querySelector(
            '[aria-label="Agent conversation"]'
          );
          const receipts = Array.from(document.querySelectorAll(
            '.computer-action-step'
          ));
          return {
            document_horizontal_overflow_pixels: overflow(documentElement),
            conversation_horizontal_overflow_pixels: overflow(conversation),
            maximum_action_horizontal_overflow_pixels: receipts.reduce(
              (largest, receipt) => Math.max(largest, overflow(receipt)),
              0
            ),
          };
        }"""
    )


def _overflow_summary(measurements: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in measurements.items()
        if key.endswith("overflow_pixels")
    )


def _horizontal_overflow_offenders(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """() => Array.from(document.body.querySelectorAll('*'))
          .map((element) => {
            const style = getComputedStyle(element);
            const bounds = element.getBoundingClientRect();
            return {
              tag: element.tagName.toLowerCase(),
              slot: element.getAttribute('data-slot') || '',
              aria: element.getAttribute('aria-label') || '',
              position: style.position,
              display: style.display,
              visibility: style.visibility,
              overflowX: style.overflowX,
              left: Math.round(bounds.left),
              right: Math.round(bounds.right),
              width: Math.round(bounds.width),
              clientWidth: element.clientWidth,
              scrollWidth: element.scrollWidth,
              excess: Math.round(
                Math.max(0, bounds.right - innerWidth, -bounds.left)
              ),
            };
          })
          .filter((item) =>
            item.display !== 'none' &&
            item.visibility !== 'hidden' &&
            (item.excess > 0 || item.scrollWidth > item.clientWidth + 1)
          )
          .sort((left, right) =>
            Math.max(right.excess, right.scrollWidth - right.clientWidth) -
            Math.max(left.excess, left.scrollWidth - left.clientWidth)
          )
          .slice(0, 8)"""
    )


def _wait_for_loaded_previews(
    page: Any,
    *,
    minimum: int,
    timeout_ms: int,
) -> int:
    deadline = time.monotonic() + timeout_ms / 1_000
    last = {"total": 0, "loaded": 0}
    while time.monotonic() < deadline:
        last = page.evaluate(
            """() => {
              const images = Array.from(document.querySelectorAll(
                '.computer-action-step img'
              ));
              return {
                total: images.length,
                loaded: images.filter(
                  (image) => image.complete && image.naturalWidth > 0
                ).length,
              };
            }"""
        )
        if last["total"] >= minimum and last["loaded"] == last["total"]:
            return int(last["loaded"])
        page.wait_for_timeout(100)
    raise BrowserAuditFailure(
        "action-bound previews did not load "
        f"({last['loaded']}/{last['total']})"
    )


def _wait_for_texts(
    locator: Any,
    expected: Sequence[str],
    *,
    timeout_ms: int,
) -> str:
    deadline = time.monotonic() + timeout_ms / 1_000
    last = ""
    while time.monotonic() < deadline:
        last = locator.inner_text()
        if all(value in last for value in expected):
            return last
        time.sleep(0.1)
    missing = ", ".join(value for value in expected if value not in last)
    raise BrowserAuditFailure(f"rendered text is missing: {missing}")


def _wait_for_hidden(locator: Any, *, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        if locator.count() == 0 or not locator.is_visible():
            return
        time.sleep(0.1)
    last = locator.evaluate_all(
        """(elements) => elements.map((element) => {
              const style = getComputedStyle(element);
              const bounds = element.getBoundingClientRect();
              return {
                attributes: Object.fromEntries(
                  Array.from(element.attributes).map(
                    (attribute) => [attribute.name, attribute.value]
                  ).filter(([name]) => name !== 'class' && name !== 'style')
                ),
                state: element.getAttribute('data-state'),
                open: element.hasAttribute('data-open'),
                closed: element.hasAttribute('data-closed'),
                hidden: element.hidden,
                ariaHidden: element.getAttribute('aria-hidden'),
                display: style.display,
                visibility: style.visibility,
                opacity: style.opacity,
                pointerEvents: style.pointerEvents,
                width: Math.round(bounds.width),
                height: Math.round(bounds.height),
              };
            })"""
    )
    raise BrowserAuditFailure(
        "UI surface did not close "
        f"({json.dumps(last, separators=(',', ':'))[:320]})"
    )


def _pointer_click(page: Any, locator: Any) -> None:
    """Issue a real pointer event at the measured centre of a UI control."""

    locator.evaluate(
        "(element) => element.scrollIntoView({"
        "behavior: 'instant', block: 'center', inline: 'center'"
        "})"
    )
    box = locator.bounding_box()
    if (
        box is None
        or not locator.is_visible()
        or not locator.is_enabled()
    ):
        raise BrowserAuditFailure("pointer target is not visible and enabled")
    page.mouse.click(
        box["x"] + box["width"] / 2,
        box["y"] + box["height"] / 2,
    )


def _select_desktop_task(page: Any, title: str) -> None:
    trigger = page.locator(
        'aside [data-slot="aui_thread-list-item-trigger"]'
    ).filter(has_text=title)
    _pointer_click(page, trigger)
    page.locator("header").get_by_text(title, exact=True).wait_for()


def _select_mobile_task(page: Any, title: str) -> None:
    _pointer_click(page, page.get_by_role("button", name="Open tasks"))
    sheet = page.get_by_role("dialog")
    trigger = sheet.locator(
        '[data-slot="aui_thread-list-item-trigger"]'
    ).filter(
        has_text=title
    ).first
    _pointer_click(page, trigger)
    page.locator("header").get_by_text(title, exact=True).wait_for()


def _audit_engine(
    playwright: Any,
    *,
    browser_name: str,
    origin: str,
    token: str,
    port: int,
    timeout_ms: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    stage = "launch browser"
    console_errors: list[str] = []
    page_errors: list[str] = []
    external_requests: list[str] = []
    browser = None
    context = None
    try:
        browser_type = getattr(playwright, browser_name)
        browser = browser_type.launch(headless=True, timeout=timeout_ms)
        stage = "create authenticated context"
        context = browser.new_context(
            viewport={"width": 1_440, "height": 900},
            reduced_motion="reduce",
        )
        context.add_init_script(
            script=(
                "sessionStorage.setItem("
                "'pikvm-harness-token', "
                f"{token!r}"
                ");"
            )
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def observe_request(request: Any) -> None:
            if not _is_fixture_request(request.url, port):
                parsed = urlparse(request.url)
                external_requests.append(
                    f"{parsed.scheme}://{parsed.netloc or '<unknown>'}"
                )

        page.on("request", observe_request)
        stage = "load authenticated workspace"
        page.goto(f"{origin}/app/", wait_until="domcontentloaded")
        conversation = page.get_by_label("Agent conversation")
        conversation.wait_for()
        stage = "wait for live updates"
        page.get_by_label(
            "Computer actions and model activity are updating live."
        ).wait_for()

        stage = "select timeline task"
        _select_desktop_task(page, TIMELINE_TASK)
        stage = "inspect configured computer identity"
        computer_connection = page.get_by_role(
            "button",
            name=re.compile(r"^Open managed computer"),
        )
        computer_connection.wait_for()
        computer_connection_text = computer_connection.inner_text()
        computer_connection_label = computer_connection.get_attribute(
            "aria-label"
        ) or ""
        connection = {
            "machine_visible": "Synthetic audit target" in computer_connection_text,
            "configured_state_visible": "configured" in computer_connection_text,
            "managed_mcp_visible": (
                "Managed PiKVM MCP is configured" in computer_connection_label
            ),
            "reachability_deferred_truthfully": (
                "reachability is checked when computer work begins"
                in computer_connection_label
            ),
        }
        if not all(connection.values()):
            missing = ", ".join(
                name for name, visible in connection.items() if not visible
            )
            raise BrowserAuditFailure(
                f"configured computer identity is incomplete: {missing}"
            )
        stage = "expand computer action group"
        group = page.locator(
            'button[aria-label^="12 computer actions"]'
        )
        group.wait_for()
        _pointer_click(page, group)
        action_rows = page.locator(".computer-action-step")
        stage = "render twelve computer action rows"
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'.computer-action-step'"
            ").length === 12"
        )
        stage = "expand individual computer actions"
        for index in range(action_rows.count()):
            stage = f"expand computer action {index + 1}"
            trigger = action_rows.nth(index).locator(":scope > button").first
            if trigger.get_attribute("aria-expanded") == "false":
                _pointer_click(page, trigger)
        stage = "open first computer action details"
        first_details = action_rows.first.get_by_role(
            "button", name="Details"
        )
        _pointer_click(page, first_details)
        stage = "wait for first computer action receipt"
        page.get_by_label("Computer action receipt").first.wait_for()
        stage = "load action-bound screen previews"
        previews_loaded = _wait_for_loaded_previews(
            page,
            minimum=10,
            timeout_ms=timeout_ms,
        )
        body_text = page.locator("body").inner_text()
        if "fast-controller-fixture" not in body_text:
            raise BrowserAuditFailure("controller model route is not visible")
        if "pikvm_run_burst" not in body_text:
            raise BrowserAuditFailure("exact MCP tool name is not visible")
        desktop = {
            "viewport": "1440x900",
            "actions": action_rows.count(),
            "screen_previews_loaded": previews_loaded,
            "model_route_visible": True,
            "exact_tool_visible": True,
            **_overflow_measurements(page),
        }
        if any(desktop[key] for key in desktop if key.endswith("overflow_pixels")):
            raise BrowserAuditFailure(
                "desktop workspace has horizontal overflow "
                f"({_overflow_summary(desktop)})"
            )

        stage = "open model connections"
        _pointer_click(
            page,
            page.get_by_role("button", name="Open model connections"),
        )
        models_sheet = page.get_by_role("dialog")
        models_sheet.get_by_role("heading", name="Models").wait_for()
        stage = "wait for configured model routes"
        models_text = _wait_for_texts(
            models_sheet,
            ("claude-account", "fast-controller"),
            timeout_ms=timeout_ms,
        )
        models = {
            "claude_oauth_visible": (
                "claude-account" in models_text
                and "Provider-owned sign-in" in models_text
            ),
            "api_provider_visible": (
                "fast-controller" in models_text
                and "Harness environment" in models_text
            ),
        }
        if not all(models.values()):
            missing = ", ".join(
                name for name, visible in models.items() if not visible
            )
            raise BrowserAuditFailure(
                f"model connection sheet is missing: {missing}"
            )
        stage = "close model connections"
        _pointer_click(
            page,
            models_sheet.get_by_role("button", name="Close"),
        )
        stage = "wait for model connections to close"
        _wait_for_hidden(models_sheet, timeout_ms=timeout_ms)

        stage = "measure responsive timeline"
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        responsive = {
            "viewport": "390x844",
            "computer_connection_visible": computer_connection.is_visible(),
            "computer_state_visible": "configured"
            in computer_connection.inner_text(),
            **_overflow_measurements(page),
        }
        if not responsive["computer_connection_visible"]:
            raise BrowserAuditFailure(
                "responsive workspace hides configured computer identity"
            )
        if not responsive["computer_state_visible"]:
            raise BrowserAuditFailure(
                "responsive workspace hides configured computer state"
            )
        if any(
            responsive[key]
            for key in responsive
            if key.endswith("overflow_pixels")
        ):
            raise BrowserAuditFailure(
                "responsive workspace has horizontal overflow "
                f"({_overflow_summary(responsive)}; offenders="
                f"{json.dumps(_horizontal_overflow_offenders(page), separators=(',', ':'))})"
            )

        stage = "select approval task"
        _select_mobile_task(page, APPROVAL_TASK)
        stage = "inspect approval boundary"
        page.get_by_text(APPROVAL_TEXT, exact=True).wait_for()
        page.get_by_text("ENTER", exact=True).wait_for()
        page.get_by_text("external side effect", exact=False).wait_for()
        allow = page.get_by_role("button", name="Allow once")
        deny = page.get_by_role("button", name="Deny")
        allow.evaluate(
            "(element) => element.scrollIntoView({"
            "behavior: 'instant', block: 'center'"
            "})"
        )
        deny.evaluate(
            "(element) => element.scrollIntoView({"
            "behavior: 'instant', block: 'center'"
            "})"
        )
        allow_box = allow.bounding_box()
        deny_box = deny.bounding_box()
        conversation_box = conversation.bounding_box()
        if not allow_box or not deny_box or not conversation_box:
            raise BrowserAuditFailure("approval controls are not measurable")
        horizontally_inside = all(
            box["x"] >= conversation_box["x"]
            and box["x"] + box["width"]
            <= conversation_box["x"] + conversation_box["width"]
            for box in (allow_box, deny_box)
        )
        send = page.get_by_role("button", name="Send message")
        approval = {
            "exact_text_visible": True,
            "consequential_enter_visible": True,
            "hold_reason_visible": True,
            "allow_once_visible": allow.is_visible(),
            "deny_visible": deny.is_visible(),
            "controls_horizontally_inside_conversation": horizontally_inside,
            "composer_send_disabled": send.is_disabled(),
            "approval_submitted": False,
        }
        if not all(value for key, value in approval.items() if key != "approval_submitted"):
            raise BrowserAuditFailure(
                "responsive approval boundary is not fully reachable"
            )

        stage = "select guarded-direct task"
        _select_mobile_task(page, DIRECT_TASK)
        stage = "inspect guarded-direct ownership"
        page.get_by_text("Guarded direct", exact=True).wait_for()
        direct_text = conversation.inner_text()
        direct = {
            "guarded_direct_visible": True,
            "caller_identity_visible": "claude-cli" in direct_text,
            "caller_provider_visible": "anthropic-oauth" in direct_text,
            "caller_model_visible": "opus" in direct_text,
            "managed_assurance_hidden": "Independent verifier" not in direct_text,
            "composer_hidden": page.get_by_role(
                "button", name="Send message"
            ).count()
            == 0,
        }
        if not all(direct.values()):
            missing = ", ".join(
                name for name, visible in direct.items() if not visible
            )
            raise BrowserAuditFailure(
                f"direct-call ownership is ambiguous: {missing}"
            )

        stage = "create synthetic progress task"
        progress_create = page.evaluate(
            """async ({ task }) => {
              const token = sessionStorage.getItem('pikvm-harness-token');
              const response = await fetch('/api/runs', {
                method: 'POST',
                headers: {
                  authorization: `Bearer ${token}`,
                  'content-type': 'application/json',
                },
                body: JSON.stringify({
                  task,
                  mode: 'computer',
                  auto_start: false,
                  source_client: 'chat-workspace',
                }),
              });
              return { status: response.status, body: await response.json() };
            }""",
            {"task": PROGRESS_TASK},
        )
        if progress_create["status"] != 200:
            raise BrowserAuditFailure(
                "synthetic progress task could not be created"
            )
        stage = "reload synthetic progress task"
        page.reload(wait_until="domcontentloaded")
        conversation.wait_for()
        _select_mobile_task(page, PROGRESS_TASK)
        stage = "inspect singular task progress"
        progress_status = page.get_by_role("status")
        progress_status.wait_for()
        progress = page.evaluate(
            """() => {
              const statuses = Array.from(
                document.querySelectorAll('[role="status"]')
              );
              const status = statuses[0];
              return {
                count: statuses.length,
                text: status?.textContent?.trim() || '',
                thread_owned: Boolean(
                  status?.closest('[data-slot="aui_thread-run-activity"]')
                ),
                inside_assistant_message: Boolean(
                  status?.closest('[data-slot="aui_assistant-message-root"]')
                ),
                branch_controls: document.querySelectorAll(
                  '[data-slot*="branch"]'
                ).length,
              };
            }"""
        )
        if (
            progress["count"] != 1
            or not progress["thread_owned"]
            or progress["inside_assistant_message"]
            or progress["branch_controls"] != 0
        ):
            raise BrowserAuditFailure(
                "task progress is duplicated or owned by an assistant branch"
            )
        if console_errors or page_errors or external_requests:
            raise BrowserAuditFailure(
                "browser emitted runtime errors or external requests"
            )
        return {
            "status": "passed",
            "browser_version": browser.version,
            "duration_ms": round((time.perf_counter() - started) * 1_000),
            "desktop": desktop,
            "responsive": responsive,
            "connection": connection,
            "models": models,
            "approval": approval,
            "direct": direct,
            "progress": progress,
            "console_errors": _clean_messages(console_errors),
            "page_errors": _clean_messages(page_errors),
            "external_requests": _clean_messages(external_requests),
        }
    except Exception as cause:
        return {
            "status": "failed",
            "duration_ms": round((time.perf_counter() - started) * 1_000),
            "failure_stage": stage,
            "failure": _clean_failure(cause),
            "console_errors": _clean_messages(console_errors),
            "page_errors": _clean_messages(page_errors),
            "external_requests": _clean_messages(external_requests),
        }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def run_browser_matrix_audit(
    browsers: str | Sequence[str] = SUPPORTED_BROWSERS,
    *,
    timeout_ms: int = 30_000,
    prefill_events: int = 1_200,
    event_interval_ms: int = 60_000,
) -> dict[str, Any]:
    """Run the real authenticated workspace through the requested engines."""

    names = parse_browser_names(browsers)
    if not 5_000 <= timeout_ms <= 120_000:
        raise ValueError("timeout_ms must be between 5000 and 120000")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAuditDependencyError(
            "Playwright is required; install the project browser extra"
        ) from exc

    with _fixture_server(
        prefill_events=prefill_events,
        event_interval_ms=event_interval_ms,
    ) as (origin, token, port):
        with sync_playwright() as playwright:
            engine_results = {
                name: _audit_engine(
                    playwright,
                    browser_name=name,
                    origin=origin,
                    token=token,
                    port=port,
                    timeout_ms=timeout_ms,
                )
                for name in names
            }
    passed = sum(
        result["status"] == "passed" for result in engine_results.values()
    )
    external_request_count = sum(
        len(result.get("external_requests", []))
        for result in engine_results.values()
    )
    report = {
        "schema_version": 1,
        "suite": "cross-browser-chat-workspace-audit",
        "recorded_at": datetime.now(UTC).isoformat(),
        "source_commit": _current_commit(),
        "source_worktree_clean": _source_worktree_clean(),
        "environment": "ephemeral-loopback-target-free-fixture",
        "requested_browsers": list(names),
        "summary": {
            "requested": len(names),
            "passed": passed,
            "failed": len(names) - passed,
            "release_gate_passed": passed == len(names),
        },
        "browsers": engine_results,
        "safety": {
            "vnc_contacted": False,
            "pikvm_contacted": False,
            "production_daemon_contacted": False,
            "model_provider_contacted": False,
            "external_requests": external_request_count,
            "approval_submitted": False,
            "computer_input_committed": False,
        },
    }
    return report
