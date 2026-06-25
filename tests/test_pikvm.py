"""PiKVM client invariants: key mapping, HID coords, frame processing, backend."""

from __future__ import annotations

import io

from PIL import Image

from pikvm_agent.config import PikvmConfig
from pikvm_agent.core.models import Region
from pikvm_agent.core.ports import ComputerBackend
from pikvm_agent.pikvm import keyboard_state as ks
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.pikvm.hid import clamp_norm, to_norm
from pikvm_agent.pikvm.screenshot import MAX_SCREENSHOT_DIM, crop, downscale, jpeg_size


def _jpeg(w: int, h: int, color=(40, 80, 120)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, format="JPEG", quality=85)
    return b.getvalue()


# ---- keyboard state ------------------------------------------------------- #

def test_key_for_us_vs_uk_symbols() -> None:
    assert ks.key_for("|", "us").code == "Backslash"
    assert ks.key_for("|", "uk").code == "IntlBackslash"
    assert ks.key_for('"', "us").code == "Quote" and ks.key_for('"', "us").shift
    assert ks.key_for('"', "uk").code == "Digit2" and ks.key_for('"', "uk").shift
    assert ks.key_for("@", "uk").code == "Quote"


def test_caps_lock_compensation_flips_letters_only() -> None:
    strokes = [
        {"code": "KeyA", "shift": True},
        {"code": "Digit1", "shift": False},
        {"code": "KeyB", "shift": False},
    ]
    ks.compensate_caps_lock(strokes, True)
    assert [s["shift"] for s in strokes] == [False, False, True]
    # caps off is a no-op
    again = [{"code": "KeyA", "shift": True}]
    assert ks.compensate_caps_lock(again, False)[0]["shift"] is True


def test_keymap_to_layout() -> None:
    assert ks.keymap_to_layout("en-gb") == "uk"
    assert ks.keymap_to_layout("en-us") == "us"
    assert ks.keymap_to_layout("de") is None


def test_kvmd_state_merge_and_getters() -> None:
    st = ks.KvmdState()
    ks.merge_kvmd_event(st, "hid", {"online": True, "keyboard": {"leds": {"caps": True}}})
    ks.merge_kvmd_event(st, "hid_keymaps", {"keymaps": {"default": "en-gb"}})
    ks.merge_kvmd_event(st, "streamer", {"source": {"resolution": {"width": 1920, "height": 1080}}})
    ks.merge_kvmd_event(st, "loop", None)
    assert ks.caps_lock_of(st) is True
    assert ks.keymap_default_of(st) == "en-gb"
    assert ks.native_resolution_of(st) == (1920, 1080)
    assert ks.hid_online_of(st) is True and st.ready is True
    # partial delta merges, doesn't clobber
    ks.merge_kvmd_event(st, "hid", {"busy": True})
    assert ks.caps_lock_of(st) is True and st.hid["busy"] is True


def test_hid_online_tri_state() -> None:
    detached = ks.KvmdState()
    ks.merge_kvmd_event(detached, "hid", {"connected": False})
    assert ks.hid_online_of(detached) is False
    assert ks.hid_online_of(ks.KvmdState()) is None  # unknown


# ---- HID coordinate math -------------------------------------------------- #

def test_to_norm_maps_corners_and_centre() -> None:
    assert to_norm(0, 1920) == -32767
    assert to_norm(1919, 1920) == 32767
    assert abs(to_norm(959.5, 1920)) <= 1
    assert clamp_norm(99999) == 32767 and clamp_norm(-99999) == -32768


# ---- screenshot processing ------------------------------------------------ #

def test_jpeg_size_and_downscale_and_crop() -> None:
    raw = _jpeg(1920, 1080)
    assert jpeg_size(raw) == (1920, 1080)
    d, w, h = downscale(raw, 1920, 1080)
    assert max(w, h) == MAX_SCREENSHOT_DIM and w == 1280
    # already small -> no-op
    assert downscale(d, w, h)[1:] == (w, h)
    cb, cw, ch = crop(raw, 1920, 1080, Region(x=100, y=100, width=400, height=200))
    assert jpeg_size(cb) == (400, 200)


# ---- backend -------------------------------------------------------------- #

def test_backend_conforms_and_derives_origins() -> None:
    b = PiKVMBackend(PikvmConfig(base_url="https://pikvm.local", layout="uk"))
    assert isinstance(b, ComputerBackend)
    assert b._http_base() == "https://pikvm.local"
    assert b._ws_url() == "wss://pikvm.local/api/ws"
    assert b.get_layout() == "uk"
    b2 = PiKVMBackend(PikvmConfig(base_url="http://10.0.0.5:8080"))
    assert b2._ws_url() == "ws://10.0.0.5:8080/api/ws"


def test_fake_backend_conforms_and_records() -> None:
    fb = FakeBackend()
    assert isinstance(fb, ComputerBackend)
