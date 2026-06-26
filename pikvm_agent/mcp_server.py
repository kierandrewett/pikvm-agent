"""MCP facade — a thin stdio server over the daemon's HTTP API.

This process owns no state. Every tool forwards to the FastAPI daemon, which
owns sessions, watchers, the operator loop, approvals, and execution. Raw HID
tools are intentionally NOT exposed as normal tools (see AGENTS.md); they can be
enabled for harness debugging only via PIKVM_AGENT_ENABLE_DEBUG_HID=1.

Run with:  pikvm-agent mcp   (or  python -m pikvm_agent.mcp_server)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

DAEMON_URL = os.environ.get("PIKVM_AGENT_DAEMON", "http://127.0.0.1:47615")

_INSTRUCTIONS = """\
Drive a physical computer through PiKVM raw video + HID. There is NO agent on the target,
no accessibility API, no clipboard, no SSH — only the same keyboard/mouse input a human
would send. YOU are the brain: look at the screenshot, decide the next few HID steps.

Default workflow (fast):
  1. pikvm_open() -> session_id + first screenshot (frame_id, world_version, control_epoch).
  2. Read the screenshot, then send ONE pikvm_run_burst(session_id, actions,
     based_on_world_version, based_on_control_epoch) covering a logical chunk (e.g. opening
     a file is one burst: Ctrl+P -> type path -> Enter -> wait_for_stable_screen). The daemon
     runs it locally and returns a fresh screenshot.
  3. Look, send the next burst. Repeat. Always pass world_version + control_epoch so a stale
     plan is refused instead of acting on a changed screen.
Use pikvm_run_playbook for common canned sequences. Use pikvm_parse_screen / find_text only
when you can't read the screen yourself (they're slow). pikvm_panic_stop halts everything.
Risky/irreversible steps (send, delete, run a mutating command): draft up to that point,
show the screenshot, and confirm before committing. pikvm_autonomous_* is opt-in and slow —
avoid unless the user explicitly wants hands-off operation.
"""

mcp = FastMCP("pikvm", json_response=True, instructions=_INSTRUCTIONS)


def _daemon_client(timeout: float) -> httpx.AsyncClient:
    """Factory for the daemon HTTP client. Patched in tests to talk to an
    in-process ASGI app instead of a live port."""
    return httpx.AsyncClient(base_url=DAEMON_URL, timeout=timeout)


async def _get(path: str, timeout: float = 60.0) -> dict[str, Any]:
    async with _daemon_client(timeout) as client:
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, json: dict[str, Any] | None = None, timeout: float = 60.0) -> dict[str, Any]:
    async with _daemon_client(timeout) as client:
        resp = await client.post(path, json=json or {})
        resp.raise_for_status()
        return resp.json()


# Strong refs to in-flight abort tasks so the GC doesn't drop a detached one.
_pending_aborts: set[asyncio.Task[Any]] = set()


async def _abort_quietly(session_id: str) -> None:
    """Best-effort abort used when a blocking call is cancelled. Never raises."""
    with contextlib.suppress(Exception):
        await _post(f"/sessions/{session_id}/abort", {"reason": "cancelled by caller"}, timeout=15.0)


def _fire_abort(session_id: str) -> asyncio.Task[None]:
    task = asyncio.ensure_future(_abort_quietly(session_id))
    _pending_aborts.add(task)
    task.add_done_callback(_pending_aborts.discard)
    return task


async def _run_or_abort(session_id: str, coro: Any) -> dict[str, Any]:
    """Await a loop-running daemon call; if the CALLER cancels it (e.g. you press
    Esc in Claude), abort the session too — so interrupting the agent actually
    halts the machine instead of leaving the daemon driving on its own."""
    try:
        return await coro
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(_fire_abort(session_id))
        raise


# ============================ LAYER 1 — fast direct control (the default) ===== #
# You look at the screen and drive it yourself with HID bursts. No OmniParser, no OCR,
# no operator LLM — the daemon just executes your input locally through PiKVM.


@mcp.tool()
async def pikvm_open(label: str = "direct control") -> dict:
    """FAST PATH — open a direct-control session and return its session_id + first
    screenshot (frame_id, world_version, control_epoch, screenshot_path).

    Use this when YOU are driving: look at the screenshot, then send HID bursts with
    pikvm_run_burst. No autonomous operator loop, no OmniParser/OCR — you are the brain
    and the daemon just executes your HID locally."""
    s = await _post("/sessions", {"task": label})
    sid = s.get("session_id")
    shot = await _get(f"/sessions/{sid}?capture=true") if sid else {}
    keep = ("frame_id", "world_version", "control_epoch", "screenshot_path", "width", "height")
    return {**s, **{k: shot.get(k) for k in keep}}


@mcp.tool()
async def pikvm_screenshot(session_id: str) -> dict:
    """Capture the current screen. Returns frame_id, world_version, control_epoch and
    screenshot_path. Pass world_version + control_epoch into pikvm_run_burst so the
    daemon refuses the burst if the screen changed under you (a popup, etc.)."""
    return await _get(f"/sessions/{session_id}?capture=true")


@mcp.tool()
async def pikvm_run_burst(session_id: str, actions: list[dict],
                          based_on_world_version: int | None = None,
                          based_on_control_epoch: int | None = None,
                          max_runtime_ms: int = 4000) -> dict:
    """FAST PATH — run a short HID burst LOCALLY in one shot, then return one screenshot.
    One call covers several steps (e.g. open a file: Ctrl+P → type path → Enter → wait),
    instead of a model round-trip per keystroke.

    Each action in `actions` is one of:
      {"type":"key","keys":["CTRL","P"]}                 — a chord (friendly names or PiKVM codes)
      {"type":"type_text","text":"...","method":"print"}  — print = fast PiKVM HID print; omit for humanized typing; add "verify":true to read-back
      {"type":"click","x":840,"y":300,"button":"left"}   — raw coordinate click (WindMouse)
      {"type":"double_click","x":840,"y":300}
      {"type":"move","x":840,"y":300}
      {"type":"scroll","direction":"down","amount":3}
      {"type":"wait","ms":250}
      {"type":"wait_for_stable_screen","stable_ms":300,"timeout_ms":1500}  — wait for the screen to settle

    Pass based_on_world_version + based_on_control_epoch (from pikvm_screenshot/pikvm_open)
    so a stale plan is refused (status "stale_world"/"control_changed") instead of acting on
    a changed screen. The burst stops mid-sequence on abort/panic/steer or the deadline and
    reports completed/remaining. Cancelling this call aborts the session."""
    body = {"actions": actions, "max_runtime_ms": max_runtime_ms,
            "based_on_world_version": based_on_world_version,
            "based_on_control_epoch": based_on_control_epoch}
    return await _run_or_abort(session_id, _post(f"/sessions/{session_id}/burst", body, timeout=120.0))


@mcp.tool()
async def pikvm_run_playbook(session_id: str, name: str, args: dict | None = None,
                            based_on_world_version: int | None = None,
                            based_on_control_epoch: int | None = None) -> dict:
    """FAST PATH — run a named burst macro (a canned HID sequence for a common task) with
    your args filled in. e.g. name="vscode.quick_open_file", args={"path":"src/app.ts"}.
    Built-ins include vscode.quick_open_file / command_palette / find_replace / save /
    focus_terminal, terminal.type_command / submit, windows.start_search, browser.goto_url.
    An unknown name returns the available list. Same freshness/control gates as a burst."""
    body = {"name": name, "args": args or {},
            "based_on_world_version": based_on_world_version,
            "based_on_control_epoch": based_on_control_epoch}
    return await _run_or_abort(session_id, _post(f"/sessions/{session_id}/playbook", body))


@mcp.tool()
async def pikvm_key(session_id: str, keys: list[str]) -> dict:
    """Send one key chord now (e.g. keys=["CTRL","S"]). Sugar over pikvm_run_burst."""
    return await pikvm_run_burst(session_id, [{"type": "key", "keys": keys}])


@mcp.tool()
async def pikvm_type_text(session_id: str, text: str, method: str = "") -> dict:
    """Type text now via PiKVM HID (method="print" for the fast HID printer, else
    humanized per-key). Never submits — send a separate Enter key. Sugar over a burst."""
    return await pikvm_run_burst(session_id, [{"type": "type_text", "text": text, "method": method}])


@mcp.tool()
async def pikvm_click(session_id: str, x: int, y: int, button: str = "left") -> dict:
    """Click at a raw screen coordinate now (WindMouse path). Sugar over a burst."""
    return await pikvm_run_burst(session_id, [{"type": "click", "x": x, "y": y, "button": button}])


@mcp.tool()
async def pikvm_scroll(session_id: str, direction: str = "down", amount: int = 3) -> dict:
    """Scroll the wheel now (direction up|down|left|right). Sugar over a burst."""
    return await pikvm_run_burst(session_id, [{"type": "scroll", "direction": direction, "amount": amount}])


# ============================ LAYER 2 — on-demand perception (only when stuck) = #
# Heavy (OmniParser GPU / OCR) — NOT for every step. Call these only when you can't tell
# what's on screen or need exact coordinates for a click.


@mcp.tool()
async def pikvm_parse_screen(session_id: str) -> dict:
    """ON DEMAND (slow). Run OmniParser + OCR on the current screen and return grounded
    elements (id, kind, text, bbox, center) + full OCR text. Use only when you can't read
    the screenshot yourself — then click element centers with pikvm_click."""
    return await _post(f"/sessions/{session_id}/parse", timeout=120.0)


@mcp.tool()
async def pikvm_ocr_region(session_id: str, x: int, y: int, w: int, h: int) -> dict:
    """ON DEMAND. OCR just one rectangle of the screen (cheaper than the whole frame) —
    e.g. to read a status line or confirm typed text landed."""
    return await _post(f"/sessions/{session_id}/ocr-region", {"x": x, "y": y, "w": w, "h": h})


@mcp.tool()
async def pikvm_find_text(session_id: str, text: str) -> dict:
    """ON DEMAND (slow). Find on-screen text and return matching elements with click
    centers. Convenience over pikvm_parse_screen when you just need 'where is X'."""
    return await _post(f"/sessions/{session_id}/find-text", {"text": text})


@mcp.tool()
async def pikvm_abort(session_id: str, reason: str = "") -> dict:
    """Stop a session (also drops any held keys/buttons)."""
    return await _post(f"/sessions/{session_id}/abort", {"reason": reason})


@mcp.tool()
async def pikvm_panic_stop() -> dict:
    """EMERGENCY BRAKE — halt every session immediately and release all held HID. Out of
    band; no agent involved. Use if anything is acting unexpectedly."""
    return await _post("/panic-stop")


# ============================ LAYER 3 — autonomous mode (OPT-IN, slow) ======== #
# A self-driving perceive->plan->act loop using OmniParser + OCR + a separate operator
# LLM. Only use these when the user EXPLICITLY asks for autonomous/hands-off operation —
# they are much slower and less reliable than driving it yourself with bursts above.


@mcp.tool()
async def pikvm_autonomous_start(task: str, policy: dict | None = None,
                                 operator: dict | None = None) -> dict:
    """OPT-IN, SLOW. Start a self-driving session for a high-level task (uses the operator
    LLM + OmniParser/OCR loop). Prefer driving it yourself with pikvm_open + bursts."""
    return await _post("/sessions", {"task": task, "policy": policy or {}, "operator": operator or {}})


@mcp.tool()
async def pikvm_autonomous_continue(session_id: str, max_transactions: int = 1,
                                    max_runtime_ms: int = 2500) -> dict:
    """OPT-IN, SLOW. Advance an autonomous session until the next checkpoint/approval/
    completion or the per-call budget is spent (then status="paused"; call again).
    Cancelling aborts the session."""
    return await _run_or_abort(
        session_id,
        _post(f"/sessions/{session_id}/continue",
              {"max_transactions": max_transactions, "max_runtime_ms": max_runtime_ms},
              timeout=900.0),
    )


@mcp.tool()
async def pikvm_autonomous_approve(session_id: str, approval_id: str, decision: dict) -> dict:
    """OPT-IN. Resolve an autonomous session's pending approval (approve/edit/reject/
    respond). Cancelling aborts the session."""
    return await _run_or_abort(
        session_id, _post(f"/sessions/{session_id}/approvals/{approval_id}", decision)
    )


@mcp.tool()
async def pikvm_export_memory_update(session_id: str) -> dict:
    """Export a safe Atlas memory-update proposal from a session's trace."""
    return await _get(f"/sessions/{session_id}/memory-update")


if os.environ.get("PIKVM_AGENT_ENABLE_DEBUG_HID") == "1":  # pragma: no cover
    @mcp.tool()
    async def debug_pikvm_raw(session_id: str, action: dict) -> dict:
        """DEBUG-ONLY raw HID passthrough (disabled unless explicitly enabled)."""
        return await _post(f"/sessions/{session_id}/debug/hid", action)


def main() -> None:  # pragma: no cover - stdio entrypoint
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
