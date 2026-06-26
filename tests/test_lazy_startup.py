"""The heavy autonomous/perception stack must NOT load with the daemon — burst mode needs
only the backend. OmniParser spawns + PaddleOCR's model load are deferred to first use."""

from __future__ import annotations

import pytest

from pikvm_agent.config import AppConfig
from pikvm_agent.runtime import Runtime
from pikvm_agent.vision.omniparser_manager import OmniParserManager


def test_paddleocr_provider_does_not_load_model_on_construction() -> None:
    # __init__ must not even import paddleocr — the model loads on the first ocr() call.
    from pikvm_agent.vision.paddleocr_client import PaddleOCRProvider

    p = PaddleOCRProvider(lang="en")
    assert p._ocr is None  # nothing loaded yet


async def test_omniparser_not_spawned_with_the_daemon(app_config: AppConfig,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    app_config.omniparser.enabled = True
    calls = {"ensure": 0}

    async def fake_ensure(self, **_kw):  # noqa: ANN001
        calls["ensure"] += 1
        return False

    monkeypatch.setattr(OmniParserManager, "ensure_running", fake_ensure)

    rt = await Runtime.from_config(app_config)
    try:
        assert rt._omniparser is not None        # the manager is constructed (cheap)…
        assert rt._omniparser_started is False    # …but NOT started with the daemon
        assert calls["ensure"] == 0               # ensure_running never ran at startup

        # First perception use spawns it exactly once; a second use doesn't re-spawn.
        sid = (await rt.start_session("direct"))["session_id"]
        await rt._ensure_omniparser()
        await rt._ensure_omniparser()
        assert calls["ensure"] == 1
        assert rt._omniparser_started is True
        assert sid  # session creation never needed OmniParser either
    finally:
        await rt.aclose()
