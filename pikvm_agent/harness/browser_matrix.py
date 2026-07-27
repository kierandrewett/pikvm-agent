"""Target-free cross-browser acceptance for the first-party chat workspace."""

from __future__ import annotations

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


def _current_commit() -> str | None:
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
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return None


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


def _select_desktop_task(page: Any, title: str) -> None:
    page.get_by_text(title, exact=True).first.click()
    page.locator("header").get_by_text(title, exact=True).wait_for()


def _select_mobile_task(page: Any, title: str) -> None:
    page.get_by_role("button", name="Open tasks").click()
    sheet = page.get_by_role("dialog")
    sheet.get_by_text(title, exact=True).click()
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
        stage = "expand computer action group"
        group = page.locator(
            'button[aria-label^="12 computer actions"]'
        )
        group.wait_for()
        group.click()
        action_rows = page.locator(".computer-action-step")
        stage = "render twelve computer action rows"
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'.computer-action-step'"
            ").length === 12"
        )
        stage = "expand individual computer actions"
        for index in range(action_rows.count()):
            trigger = action_rows.nth(index).locator(":scope > button").first
            trigger.scroll_into_view_if_needed()
            if trigger.get_attribute("aria-expanded") == "false":
                trigger.click()
        first_details = action_rows.first.get_by_role(
            "button", name="Details"
        )
        first_details.click()
        page.get_by_label("Computer action receipt").first.wait_for()
        preview_images = action_rows.locator("img")
        stage = "load action-bound screen previews"
        page.wait_for_function(
            """() => {
              const images = Array.from(document.querySelectorAll(
                '.computer-action-step img'
              ));
              return images.length >= 10 &&
                images.every((image) => image.complete && image.naturalWidth > 0);
            }"""
        )
        body_text = page.locator("body").inner_text()
        if "fast-controller-fixture" not in body_text:
            raise BrowserAuditFailure("controller model route is not visible")
        if "pikvm_run_burst" not in body_text:
            raise BrowserAuditFailure("exact MCP tool name is not visible")
        desktop = {
            "viewport": "1440x900",
            "actions": action_rows.count(),
            "screen_previews_loaded": preview_images.count(),
            "model_route_visible": True,
            "exact_tool_visible": True,
            **_overflow_measurements(page),
        }
        if any(desktop[key] for key in desktop if key.endswith("overflow_pixels")):
            raise BrowserAuditFailure("desktop workspace has horizontal overflow")

        stage = "open model connections"
        page.get_by_role("button", name="Open model connections").click()
        models_sheet = page.get_by_role("dialog")
        models_sheet.get_by_role("heading", name="Models").wait_for()
        stage = "wait for configured model routes"
        for route_name in ("claude-account", "fast-controller"):
            models_sheet.get_by_text(
                route_name,
                exact=False,
            ).first.wait_for()
        models_text = models_sheet.inner_text()
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
        page.keyboard.press("Escape")
        models_sheet.wait_for(state="hidden")

        stage = "measure responsive timeline"
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(100)
        responsive = {
            "viewport": "390x844",
            **_overflow_measurements(page),
        }
        if any(
            responsive[key]
            for key in responsive
            if key.endswith("overflow_pixels")
        ):
            raise BrowserAuditFailure("responsive workspace has horizontal overflow")

        stage = "select approval task"
        _select_mobile_task(page, APPROVAL_TASK)
        stage = "inspect approval boundary"
        page.get_by_text(APPROVAL_TEXT, exact=True).wait_for()
        page.get_by_text("ENTER", exact=True).wait_for()
        page.get_by_text("external side effect", exact=False).wait_for()
        allow = page.get_by_role("button", name="Allow once")
        deny = page.get_by_role("button", name="Deny")
        allow.scroll_into_view_if_needed()
        deny.scroll_into_view_if_needed()
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
            "models": models,
            "approval": approval,
            "direct": direct,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "external_requests": external_requests,
        }
    except Exception as cause:
        return {
            "status": "failed",
            "duration_ms": round((time.perf_counter() - started) * 1_000),
            "failure_stage": stage,
            "failure": _clean_failure(cause),
            "console_errors": console_errors,
            "page_errors": page_errors,
            "external_requests": external_requests,
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
