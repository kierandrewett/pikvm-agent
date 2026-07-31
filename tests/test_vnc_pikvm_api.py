"""The lab API must look like PiKVM while touching only the supplied VNC VM."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

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

    async def ocr(self, *, left: int, top: int, right: int, bottom: int) -> str:
        self.calls.append(("ocr", (left, top, right, bottom)))
        return "observer target"

async def test_http_snapshot_print_and_ocr_match_pikvm_contract() -> None:
    vnc = FakeVncTransport()
    transport = httpx.ASGITransport(app=create_vnc_pikvm_app(vnc, keymap="en-gb"))
    async with httpx.AsyncClient(transport=transport, base_url="http://lab") as client:
        assert (await client.get("/api/info")).status_code == 200

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


async def test_windows_transport_uses_semantic_shifted_punctuation() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

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

    await transport.key("ShiftLeft", True)
    await transport.key("Semicolon", True)
    await transport.key("Semicolon", False)
    await transport.key("ShiftLeft", False)

    assert client.calls == [
        ("down", ":"),
        ("up", ":"),
    ]


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
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp4"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp6"),
        ("up", "alt"),
    ]


async def test_windows_transport_prints_shifted_punctuation_semantically() -> None:
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
        ("down", ":"),
        ("up", ":"),
        ("down", "alt"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp4"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp9"),
        ("press", "kp2"),
        ("up", "alt"),
        ("down", "alt"),
        ("press", "kp1"),
        ("press", "kp2"),
        ("press", "kp6"),
        ("up", "alt"),
    ]


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
