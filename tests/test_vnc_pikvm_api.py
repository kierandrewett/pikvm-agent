"""The lab API must look like PiKVM while touching only the supplied VNC VM."""

from __future__ import annotations

import asyncio
import io
import threading

import httpx
import pytest
from PIL import Image

from pikvm_agent.harness import vnc_pikvm_api
from pikvm_agent.harness.vnc_pikvm_api import (
    VncDotoolTransport,
    code_to_character,
    code_to_vnc_key,
    create_vnc_pikvm_app,
    dispatch_hid_event,
    initial_state_messages,
    shifted_code_to_character,
)
from pikvm_agent.harness.lab import build_lab_config


def _jpeg(width: int = 640, height: int = 400) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 80)).save(out, "JPEG")
    return out.getvalue()


def test_lab_config_is_isolated_and_contains_no_vnc_target(tmp_path) -> None:
    config = build_lab_config(
        api_host="127.0.0.1",
        api_port=48640,
        daemon_host="127.0.0.1",
        daemon_port=48641,
        state_dir=tmp_path,
        keymap="en-gb",
    )

    assert config.pikvm.base_url == "http://127.0.0.1:48640"
    assert config.pikvm.layout == "uk"
    assert config.daemon.listen == "127.0.0.1:48641"
    assert config.daemon.sqlite_path.startswith(str(tmp_path))
    assert "vnc" not in config.model_dump_json().lower()


class FakeVncTransport:
    width = 640
    height = 400

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def connect(self) -> None:
        self.calls.append(("connect", None))

    async def close(self) -> None:
        self.calls.append(("close", None))

    async def screenshot(self) -> bytes:
        self.calls.append(("screenshot", None))
        return _jpeg(self.width, self.height)

    async def key(self, code: str, down: bool) -> None:
        self.calls.append(("key", (code, down)))

    async def mouse_move(self, x: int, y: int) -> None:
        self.calls.append(("mouse_move", (x, y)))

    async def mouse_button(self, button: str, down: bool) -> None:
        self.calls.append(("mouse_button", (button, down)))

    async def mouse_wheel(self, dx: int, dy: int) -> None:
        self.calls.append(("mouse_wheel", (dx, dy)))

    async def print_text(self, text: str) -> None:
        self.calls.append(("print_text", text))

    def input_transport_diagnostics(self) -> dict[str, object]:
        return {
            "strategy_version": "fake-rfb-print-v1",
            "print_sequence": 0,
        }

    async def ocr(self, *, left: int, top: int, right: int, bottom: int) -> str:
        self.calls.append(("ocr", (left, top, right, bottom)))
        return "observer target"

async def test_http_snapshot_print_and_ocr_match_pikvm_contract() -> None:
    vnc = FakeVncTransport()
    transport = httpx.ASGITransport(app=create_vnc_pikvm_app(vnc, keymap="en-gb"))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        info = await client.get("/api/info")
        assert info.status_code == 200
        assert (
            info.json()["result"]["extras"]["vnc_lab"][
                "atomic_shifted_print"
            ]
            is True
        )
        assert info.json()["result"]["extras"]["vnc_lab"]["keymap"] == "en-gb"
        assert info.json()["result"]["extras"]["vnc_lab"][
            "input_transport"
        ] == {
            "strategy_version": "fake-rfb-print-v1",
            "print_sequence": 0,
        }

        snap = await client.get("/api/streamer/snapshot")
        assert snap.status_code == 200
        assert snap.headers["x-ustreamer-width"] == "640"
        assert snap.headers["x-ustreamer-height"] == "400"
        assert Image.open(io.BytesIO(snap.content)).size == (640, 400)

        printed = await client.post(
            "/api/hid/print?limit=0&slow=1",
            content="long prose without clipboard",
            headers={"content-type": "text/plain"},
        )
        assert printed.status_code == 200
        assert ("print_text", "long prose without clipboard") in vnc.calls

        boundary = await client.post(
            "/api/hid/print?limit=0&slow=1",
            content="hello  same-line \n next",
            headers={"content-type": "text/plain"},
        )
        assert boundary.status_code == 200
        assert ("print_text", "hello  same-line next") in vnc.calls

        ocr = await client.get(
            "/api/streamer/snapshot",
            params={
                "ocr": 1,
                "ocr_left": 10,
                "ocr_top": 20,
                "ocr_right": 300,
                "ocr_bottom": 100,
            },
        )
        assert ocr.text == "observer target"
        assert ("ocr", (10, 20, 300, 100)) in vnc.calls

        # Ground truth must never share the controller's adapter surface.
        assert (await client.post("/lab/oracle/snapshot")).status_code == 404


async def test_adapter_does_not_expose_an_oracle_route() -> None:
    transport = httpx.ASGITransport(app=create_vnc_pikvm_app(FakeVncTransport()))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        response = await client.post("/lab/oracle/snapshot")

    assert response.status_code == 404


async def test_websocket_translates_hid_and_normalised_mouse_to_vnc() -> None:
    vnc = FakeVncTransport()
    initial = initial_state_messages(vnc.width, vnc.height, "en-gb")
    assert [message["event_type"] for message in initial] == [
        "hid",
        "hid_keymaps",
        "streamer",
        "ocr",
        "clients",
        "loop",
    ]
    assert initial[1]["event"]["keymaps"]["default"] == "en-gb"

    await dispatch_hid_event(
        vnc, {"event_type": "key", "event": {"key": "KeyA", "state": True}}
    )
    await dispatch_hid_event(
        vnc,
        {
            "event_type": "mouse_move",
            "event": {"to": {"x": 32767, "y": -32768}},
        },
    )
    await dispatch_hid_event(
        vnc,
        {
            "event_type": "mouse_button",
            "event": {"button": "left", "state": True},
        },
    )

    assert ("key", ("KeyA", True)) in vnc.calls
    assert ("mouse_move", (639, 0)) in vnc.calls
    assert ("mouse_button", ("left", True)) in vnc.calls


def test_shifted_codes_are_mapped_semantically_without_capslock() -> None:
    assert shifted_code_to_character("KeyR") == "R"
    assert shifted_code_to_character("Digit1") == "!"
    assert shifted_code_to_character("BracketLeft") == "{"
    assert shifted_code_to_character("KeyR", "unknown-layout") == "R"
    assert shifted_code_to_character("Digit1", "unknown-layout") is None
    assert code_to_character("Backslash", shifted=False, keymap="en-gb") == "#"
    assert code_to_character("Digit2", shifted=True, keymap="en-gb") == '"'
    assert code_to_character("Quote", shifted=True, keymap="en-gb") == "@"
    assert code_to_character("KeyR", shifted=False, keymap="en-gb") == "r"


def test_lock_and_system_keys_use_vncdotool_names() -> None:
    assert code_to_vnc_key("CapsLock") == "caplk"
    assert code_to_vnc_key("NumLock") == "numlk"
    assert code_to_vnc_key("ScrollLock") == "scrlk"
    assert code_to_vnc_key("Pause") == "pause"
    assert code_to_vnc_key("PrintScreen") == "sysrq"
    assert code_to_vnc_key("NumpadEnter") == "kpenter"
    assert ord(code_to_vnc_key("NumpadAdd")) == 0xFFAB
    assert ord(code_to_vnc_key("NumpadSubtract")) == 0xFFAD
    assert ord(code_to_vnc_key("NumpadMultiply")) == 0xFFAA
    assert ord(code_to_vnc_key("NumpadDivide")) == 0xFFAF
    assert ord(code_to_vnc_key("NumpadDecimal")) == 0xFFAE


def test_unknown_multicharacter_key_never_reaches_vncdotool() -> None:
    with pytest.raises(ValueError, match="unsupported VNC key code"):
        code_to_vnc_key("ctrl+End")


async def test_transport_uses_semantic_uppercase_for_shifted_letters() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport("unused:5900")
    client = Client()
    transport._client = client

    await transport.key("ShiftLeft", True)
    await transport.key("KeyR", True)
    await transport.key("KeyR", False)
    await transport.key("ShiftLeft", False)

    assert client.calls == [
        ("down", "R"),
        ("up", "R"),
    ]


async def test_windows_transport_uses_physical_chord_for_invariant_punctuation() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("ShiftLeft", True)
    await transport.key("Semicolon", True)
    await transport.key("Semicolon", False)
    await transport.key("ShiftLeft", False)

    assert client.calls == [
        ("down", "shift"),
        ("down", ";"),
        ("up", ";"),
        ("up", "shift"),
    ]


async def test_windows_transport_keeps_each_plain_space_tap_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    sleeps: list[float] = []
    monkeypatch.setattr(vnc_pikvm_api.time, "sleep", sleeps.append)
    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    for _ in range(4):
        await transport.key("Space", True)
        await transport.key("Space", False)

    assert client.calls == [
        (event, "space")
        for _ in range(4)
        for event in ("down", "up")
    ]
    assert sleeps == [0.075, 0.075] * 4


async def test_windows_transport_keeps_each_plain_letter_tap_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    sleeps: list[float] = []
    monkeypatch.setattr(vnc_pikvm_api.time, "sleep", sleeps.append)
    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("KeyU", True)
    await transport.key("KeyU", False)

    assert client.calls == [("down", "u"), ("up", "u")]
    assert sleeps == [0.075, 0.075]


async def test_windows_transport_clears_a_stale_chord_before_printable_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    sleeps: list[float] = []
    monkeypatch.setattr(vnc_pikvm_api.time, "sleep", sleeps.append)
    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("ControlLeft", True)
    await transport.key("KeyN", True)
    await transport.key("KeyN", False)
    await transport.key("ControlLeft", False)
    await transport.print_text("a")

    assert client.calls == [
        ("down", "ctrl"),
        ("down", "n"),
        ("up", "n"),
        ("up", "ctrl"),
        ("up", "shift"),
        ("up", "ctrl"),
        ("up", "alt"),
        ("up", "super"),
        ("down", "a"),
        ("up", "a"),
    ]
    assert sleeps == [0.075, 0.075, 0.075]


async def test_transport_uses_alt_codes_for_uk_symbols_windows_vnc_drops() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("ShiftLeft", True)
    await transport.key("IntlBackslash", True)
    await transport.key("IntlBackslash", False)
    await transport.key("Backslash", True)
    await transport.key("Backslash", False)
    await transport.key("ShiftLeft", False)

    assert client.calls == [
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp4"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp6"),
        ("up", "alt"),
    ]


async def test_windows_transport_types_uk_hash_semantically_across_wire_paths() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("Backslash", True)
    await transport.key("Backslash", False)

    expected = [
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp0"),
        ("press", "kp3"),
        ("press", "kp5"),
        ("up", "alt"),
    ]
    assert client.calls == expected

    client.calls.clear()
    await transport.print_text("#")

    assert client.calls == expected


async def test_windows_transport_reports_the_exercised_print_strategy() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    transport._client = Client()

    await transport.print_text("# Release 1.0")

    diagnostics = transport.input_transport_diagnostics()
    assert diagnostics["strategy_version"] == "windows-rfb-print-v2"
    assert diagnostics["keymap"] == "en-gb"
    assert diagnostics["keyboard_profile"] == "windows"
    assert diagnostics["print_sequence"] == 1
    assert diagnostics["print_history"] == [
        {
            "sequence": 1,
            "characters": 13,
            "text_sha256": (
                "a09f21ee71a9b858aefacb390fbac6f29ce0e0c7523918d4e4bb691c29b4e3db"
            ),
            "routes": {
                "windows_atomic_printable": 12,
                "windows_semantic_alt_code": 1,
            },
        }
    ]


async def test_windows_transport_types_unshifted_uk_backslash_as_backslash() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.key("IntlBackslash", True)
    await transport.key("IntlBackslash", False)

    assert client.calls == [
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp0"),
        ("press", "kp9"),
        ("press", "kp2"),
        ("up", "alt"),
    ]


async def test_windows_transport_prints_invariant_punctuation_physically() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.print_text("Aa:|\\~")

    assert client.calls == [
        ("down", "A"),
        ("up", "A"),
        ("down", "a"),
        ("up", "a"),
        ("down", "shift"),
        ("down", ";"),
        ("up", ";"),
        ("up", "shift"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp4"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp0"),
        ("press", "kp9"),
        ("press", "kp2"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp6"),
        ("up", "alt"),
    ]


def test_windows_shifted_key_dwell_survives_remote_rfb_coalescing(
    monkeypatch,
) -> None:
    """The measured Windows path needs a full human Shift chord dwell."""

    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    sleeps = []
    monkeypatch.setattr(
        "pikvm_agent.harness.vnc_pikvm_api.time.sleep",
        sleeps.append,
    )
    client = Client()

    VncDotoolTransport._type_windows_physical_shifted_key(
        client,
        "0",
    )

    assert client.calls == [
        ("down", "shift"),
        ("down", "0"),
        ("up", "0"),
        ("up", "shift"),
    ]
    assert sleeps == [0.100, 0.075, 0.100]


def test_windows_alt_code_waits_for_guest_to_commit_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

    sleeps: list[float] = []
    monkeypatch.setattr(vnc_pikvm_api.time, "sleep", sleeps.append)
    client = Client()

    VncDotoolTransport._type_windows_alt_code(client, '"')

    assert client.calls == [
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp0"),
        ("press", "kp3"),
        ("press", "kp4"),
        ("up", "alt"),
    ]
    assert sleeps == [0.075, 0.035, 0.035, 0.035, 0.035, 0.075, 0.100]


async def test_windows_transport_prints_cp1252_unicode_with_alt_codes() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyPress(self, key) -> None:
            self.calls.append(("press", key))

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    client = Client()
    transport._client = client

    await transport.print_text("—“”")

    assert client.calls == [
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp5"),
        ("press", "kp1"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp4"),
        ("press", "kp7"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp0"),
        ("press", "kp1"),
        ("press", "kp4"),
        ("press", "kp8"),
        ("up", "alt"),
    ]


async def test_windows_transport_prints_each_plain_character_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyDown(self, key) -> None:
            self.calls.append(("down", key))

        def keyUp(self, key) -> None:
            self.calls.append(("up", key))

    sleeps: list[float] = []
    monkeypatch.setattr(vnc_pikvm_api.time, "sleep", sleeps.append)
    transport = VncDotoolTransport(
        "unused:5900",
        keymap="en-gb",
        keyboard_profile="windows",
    )
    transport._client = Client()

    await transport.print_text("ab")

    assert transport._client.calls == [
        ("down", "a"),
        ("up", "a"),
        ("down", "b"),
        ("up", "b"),
    ]
    assert sleeps == [0.075, 0.075, 0.075, 0.075]


async def test_transport_releases_stale_modifiers_on_connection() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def keyUp(self, key) -> None:
            self.calls.append(key)

    transport = VncDotoolTransport("unused:5900")
    client = Client()
    transport._client = client

    transport._release_client_modifiers()

    assert client.calls == ["shift", "ctrl", "alt", "super"]


async def test_transport_coalesces_frequent_read_only_frame_consumers() -> None:
    class Client:
        def __init__(self) -> None:
            self.captures = 0

        def captureScreen(self, output, *, format) -> None:
            assert format == "PNG"
            self.captures += 1
            Image.new("RGB", (640, 400), (20, 40, 80)).save(output, "PNG")

    transport = VncDotoolTransport(
        "unused:5900",
        frame_cache_ttl_s=60,
    )
    client = Client()
    transport._client = client

    first = await transport.screenshot()
    second = await transport.screenshot()

    assert first == second
    assert client.captures == 1


async def test_transport_reconnects_once_after_capture_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleClient:
        def __init__(self) -> None:
            self.disconnected = False

        def captureScreen(self, _output, *, format) -> None:
            assert format == "PNG"
            raise TimeoutError("guest reboot disconnected RFB capture")

        def disconnect(self) -> None:
            self.disconnected = True

    class ReplacementClient:
        def __init__(self) -> None:
            self.captures = 0
            self.released: list[str] = []

        def keyUp(self, key: str) -> None:
            self.released.append(key)

        def captureScreen(self, output, *, format) -> None:
            assert format == "PNG"
            self.captures += 1
            Image.new("RGB", (640, 400), (20, 40, 80)).save(output, "PNG")

    stale = StaleClient()
    replacement = ReplacementClient()
    reconnects = 0

    async def reconnect() -> ReplacementClient:
        nonlocal reconnects
        reconnects += 1
        return replacement

    transport = VncDotoolTransport("unused:5900")
    transport._client = stale
    monkeypatch.setattr(transport, "_connect_client", reconnect)

    frame = await transport.screenshot()

    assert Image.open(io.BytesIO(frame)).size == (640, 400)
    assert stale.disconnected is True
    assert reconnects == 1
    assert replacement.captures == 1
    assert replacement.released == ["shift", "ctrl", "alt", "super"]


async def test_transport_reconnects_before_a_stale_capture_strands_http_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_capture_released = threading.Event()
    replacement_png = io.BytesIO()
    Image.new("RGB", (640, 400), (20, 40, 80)).save(replacement_png, "PNG")

    class StaleClient:
        def __init__(self) -> None:
            self.disconnected = False

        def captureScreen(self, _output, *, format) -> None:
            assert format == "PNG"
            stale_capture_released.wait(timeout=1)

        def disconnect(self) -> None:
            self.disconnected = True
            stale_capture_released.set()

    class ReplacementClient:
        def __init__(self) -> None:
            self.released: list[str] = []

        def keyUp(self, key: str) -> None:
            self.released.append(key)

        def captureScreen(self, output, *, format) -> None:
            assert format == "PNG"
            output.write(replacement_png.getvalue())

    stale = StaleClient()
    replacement = ReplacementClient()
    transport = VncDotoolTransport("unused:5900", capture_timeout_s=0.05)
    transport._client = stale

    async def reconnect() -> ReplacementClient:
        return replacement

    monkeypatch.setattr(transport, "_connect_client", reconnect)

    frame = await asyncio.wait_for(transport.screenshot(), timeout=0.5)

    assert Image.open(io.BytesIO(frame)).size == (640, 400)
    assert stale.disconnected is True
    assert replacement.released == ["shift", "ctrl", "alt", "super"]


async def test_transport_invalidates_cached_frame_after_input() -> None:
    class Client:
        def __init__(self) -> None:
            self.captures = 0

        def captureScreen(self, output, *, format) -> None:
            assert format == "PNG"
            self.captures += 1
            Image.new("RGB", (640, 400), (20, 40, 80)).save(output, "PNG")

        def keyDown(self, _key) -> None:
            pass

        def keyUp(self, _key) -> None:
            pass

    transport = VncDotoolTransport(
        "unused:5900",
        frame_cache_ttl_s=60,
    )
    client = Client()
    transport._client = client

    await transport.screenshot()
    await transport.key("KeyA", True)
    await transport.screenshot()

    assert client.captures == 2
