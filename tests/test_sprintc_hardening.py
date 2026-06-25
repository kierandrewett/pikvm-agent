"""Regression tests from the GPT-5.5 (Codex) Sprint C review."""

from __future__ import annotations

from pikvm_agent.config import AppConfig, PolicyConfig
from pikvm_agent.core.models import GuardedTransaction, RiskAssessment
from pikvm_agent.executor.recovery import Recovery
from pikvm_agent.executor.transactions import GuardedTransactionExecutor
from pikvm_agent.graph import nodes
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider
from pikvm_agent.vision.providers import build_screen_parser


def _tx(actions: list[dict]) -> GuardedTransaction:
    return GuardedTransaction(
        id="t", session_id="s", based_on_frame_id=1, based_on_world_version=1,
        intent="x", actions=actions,
        risk=RiskAssessment(level="low", category="navigation", requires_human=False),
    )


async def test_wait_for_mode_blocks_when_not_reached() -> None:
    # P2: a swallowed shortcut must NOT let later actions type into the wrong target.
    be = FakeBackend()
    be.ocr_text = "ordinary desktop wallpaper"  # never the requested mode
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    res = await ex.execute(_tx([
        {"type": "wait_for_mode", "mode": "vscode.quick_open", "timeout_ms": 200},
        {"type": "type_text", "text": "readme.md"},
    ]), {"frame_id": 1, "world_version": 1})
    assert res.status == "failed" and "not reached" in res.error
    assert not any(m == "type_text" for m, _ in be.calls)  # never typed into wrong target


async def test_wait_for_mode_proceeds_when_reached() -> None:
    be = FakeBackend()
    be.ocr_text = "Go to File"  # detect_mode -> vscode.quick_open
    ex = GuardedTransactionExecutor(be, PiKVMOcrProvider(be))
    res = await ex.execute(_tx([
        {"type": "wait_for_mode", "mode": "vscode.quick_open", "timeout_ms": 500},
        {"type": "keypress", "keys": ["KeyA"]},
    ]), {"frame_id": 1, "world_version": 1})
    assert res.status in ("executed", "verified")
    assert any(m == "keypress" for m, _ in be.calls)


async def test_recover_reobserves_before_acting(tmp_path) -> None:
    # P1: recovery must target the CURRENT screen, not the invalidated one.
    be = FakeBackend()
    deps = GraphDeps(
        backend=be, frames=FrameStore("s", tmp_path, be), trace=TraceLog("s", tmp_path),
        screen_parser=build_screen_parser(AppConfig(), be), operator=None,
        policy=SafetyPolicyEngine(PolicyConfig()), recovery=Recovery(be),
        detect_mode=lambda text, em: "terminal.pager",  # force the pager path
    )
    config = {"configurable": {"deps": deps}}
    # Stale incoming state claims a different screen; recover must re-observe.
    out = await nodes.recover({"mode": "vscode.editor", "frame_id": 0, "world_version": 1}, config)
    assert out["mode"] == "terminal.pager"
    assert out["frame_id"] >= 1  # a fresh frame was captured
    assert any(m == "press_key" and kw.get("code") == "KeyQ" for m, kw in be.calls)
