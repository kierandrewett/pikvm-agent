"""E1–E10 regression bench — Kieran's real incident log, turned into tests.

These are the canonical "must never regress" cases (docs/PLAN.md → *Regression
incidents*). Each exercises the owned component that defends the invariant, so a
future change that reopens an incident fails here.
"""

from __future__ import annotations

from pikvm_agent.config import PolicyConfig
from pikvm_agent.core.models import (
    GuardedTransaction,
    OperatorDecision,
    RiskAssessment,
    ScrollAction,
    TransactionResult,
)
from pikvm_agent.executor.recovery import Recovery
from pikvm_agent.executor.transactions import GuardedTransactionExecutor
from pikvm_agent.executor.typing import WatchedTyper
from pikvm_agent.executor.verification import classify_mismatch, verify_text
from pikvm_agent.graph import nodes
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.pikvm.keyboard_state import compensate_caps_lock
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider


# E1 — terminal find README symbol/case mismatch -------------------------------
def test_E1_find_readme_symbol_case_cannot_verify() -> None:
    r = verify_text(
        "find . -name 'README*' | sort && echo \"=== ROOT ===\"",
        "find . -name 'readme*' ~ sort && echo @=== root ===@",
        "terminal.readline",
    )
    assert r.status.startswith("failed_")
    assert r.safe_to_continue is False  # ⇒ caller must NOT press Enter


# E2 — Teams stale screen, double Enter ----------------------------------------
async def test_E2_stale_world_blocks_second_enter(tmp_path) -> None:
    backend = FakeBackend()
    fs = FrameStore("s", tmp_path, backend)
    await fs.capture()  # observe @ world 1 (baseline)
    executed: list[str] = []

    async def rec(tx: GuardedTransaction, _state) -> TransactionResult:
        executed.append("ran")
        return TransactionResult(status="executed")

    deps = GraphDeps(
        backend=backend, frames=fs, trace=TraceLog("s", tmp_path), screen_parser=None,
        operator=None, policy=SafetyPolicyEngine(PolicyConfig()), execute=rec,
    )
    decision = OperatorDecision(
        based_on_frame_id=1, based_on_world_version=1, intent="press enter",
        risk=RiskAssessment(level="low", category="navigation", requires_human=False),
        actions=[{"type": "keypress", "keys": ["Enter"]}],
    ).model_dump()
    backend.set_screen("a different screen appeared", bg=(200, 20, 20))  # world will bump
    out = await nodes.execute_transaction(
        {"session_id": "s", "operator_decision": decision, "frame_id": 1, "world_version": 1,
         "approved": False, "element_map": {"frame_id": 1, "world_version": 1, "elements": []}},
        {"configurable": {"deps": deps}},
    )
    assert out["transaction_result"]["status"] == "failed_stale_frame"
    assert executed == []  # the second Enter was refused against the changed world


# E3 — Caps Lock / Windows App background --------------------------------------
def test_E3_caps_lock_compensation_fixes_case() -> None:
    # "Wa" → KeyW(shift) + KeyA(no shift). With caps ON the shift bit inverts so
    # the OUTPUT case stays correct ("Windows App", not "wINDOWS aPP").
    strokes = [{"code": "KeyW", "shift": True}, {"code": "KeyA", "shift": False}]
    compensate_caps_lock(strokes, True)
    assert [s["shift"] for s in strokes] == [False, True]


async def test_E3_fast_print_disabled_when_caps_on() -> None:
    backend = FakeBackend()
    backend.caps_lock = True
    typer = WatchedTyper(backend, PiKVMOcrProvider(backend))
    backend.ocr_text = "a fairly long sentence that would normally take the fast path here"
    res = await typer.type_text(backend.ocr_text, region=None)
    assert res.used_fast_path is False  # caps on ⇒ humanized typer (per-letter shift)


# E4 — shell prompt false mismatch ---------------------------------------------
def test_E4_strips_shell_prompt_before_compare() -> None:
    assert verify_text("git status", "$ git status", "terminal.readline").verified
    assert verify_text("ls -la", "drewettk@HOST MINGW64 ~/p$ ls -la", "terminal.readline").verified


# E5 — truncated readback is unverified, not a destructive retype --------------
def test_E5_truncated_readback_is_unverified() -> None:
    r = verify_text(
        "clear; wc -l README.md; sed -n '1,45p' image-build/oel9-cis/README.md",
        "$ clear; wc -l README.md; sed -n '1,45p' image-build",
        "terminal.readline",
    )
    assert r.status == "unverified_truncated" and r.safe_to_continue is False


# E6 — VS Code Quick Open wrong OCR region -------------------------------------
def test_E6_wrong_region_is_unverified_not_mismatch() -> None:
    r = verify_text("oel9-cis/README.md", "® README.md image-build oel9-cis runner RS NE a")
    assert r.status in ("unverified_wrong_region", "unverified_ambiguous")
    assert not r.status.startswith("failed_")


# E7 — long Teams text uses the fast paste/print path --------------------------
async def test_E7_long_prose_uses_fast_print() -> None:
    backend = FakeBackend()
    # Genuinely long prose (> FAST_PRINT_MIN = 120) takes the fast (bursty) print path;
    # shorter sentences now stay on the fully-humanized per-key path.
    text = ("Hi team, just a quick note to confirm the rollout window is unchanged and on "
            "track for Thursday evening, and that the rollback plan is ready if we need it.")
    assert len(text) > 120
    backend.ocr_text = text
    typer = WatchedTyper(backend, PiKVMOcrProvider(backend))
    res = await typer.type_text(text, region=None)
    assert res.used_fast_path is True
    assert any(m == "print_text" for m, _ in backend.calls)


# E8 — Teams autoformat (reorder / reformat that keeps the words) -------------
def test_E8_prepend_autocorrect_is_classified() -> None:
    # Teams reflowed the same words into a different order (autoformat) — same
    # multiset of words, but not a clean substring ⇒ a correctable autoformat.
    kind = classify_mismatch("alpha beta gamma", "gamma alpha beta", precise=False)
    assert kind == "prepend-autocorrect"


# E9 — git pager trap -----------------------------------------------------------
def test_E9_pager_blocks_shell_typing() -> None:
    eng = SafetyPolicyEngine(PolicyConfig())
    ok, reason = eng.action_allowed_in_mode("type_text", "terminal.pager")
    assert ok is False and "pager" in reason


async def test_E9_pager_recovery_quits(tmp_path) -> None:
    backend = FakeBackend()
    await Recovery(backend).recover_pager()
    assert any(m == "press_key" and kw.get("code") == "KeyQ" for m, kw in backend.calls)


# E10 — scroll never collapses to (0,0) ----------------------------------------
def test_E10_scroll_keeps_real_amount() -> None:
    assert ScrollAction(type="scroll", direction="up").amount == 3  # default is non-zero


async def test_E10_scroll_executes_nonzero_delta() -> None:
    backend = FakeBackend()
    ex = GuardedTransactionExecutor(backend, PiKVMOcrProvider(backend))
    tx = GuardedTransaction(
        id="t", session_id="s", based_on_frame_id=1, based_on_world_version=1, intent="scroll",
        risk=RiskAssessment(level="low", category="navigation", requires_human=False),
        actions=[{"type": "scroll", "direction": "down", "amount": 4}],
    )
    await ex.execute(tx, {"frame_id": 1, "world_version": 1})
    delta = next(kw for m, kw in backend.calls if m == "scroll")
    assert (delta["dx"], delta["dy"]) == (0, -4) and delta != {"dx": 0, "dy": 0}
