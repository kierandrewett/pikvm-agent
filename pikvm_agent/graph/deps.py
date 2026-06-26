"""Per-session service bundle injected into graph nodes.

Nodes are pure control flow; everything that touches PiKVM, vision, the
operator, policy, or execution comes from here. ``GraphDeps`` is passed through
the LangGraph run config (``configurable.deps``) rather than the checkpointed
state, because services aren't serializable and the state must stay replayable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from pikvm_agent.core.models import ElementMap, GuardedTransaction, TransactionResult
    from pikvm_agent.policy.safety import SafetyPolicyEngine
    from pikvm_agent.store.frames import FrameStore
    from pikvm_agent.store.trace import TraceLog


@dataclass
class GraphDeps:
    backend: Any
    frames: "FrameStore"
    trace: "TraceLog"
    screen_parser: Any
    operator: Any
    policy: "SafetyPolicyEngine"
    detect_mode: Callable[[str, "ElementMap | None"], str] | None = None
    # execute(tx, state) -> TransactionResult. Phase 3 uses a record-only default
    # (see nodes.execute_transaction); Phase 4 injects the guarded executor.
    execute: Callable[["GuardedTransaction", dict], Awaitable["TransactionResult"]] | None = None
    recovery: Any = None  # Recovery (pager-quit / dismiss-modal / refocus)
    lane: str = "default"
    max_steps: int = 12
    # Reads the session's LIVE controller epoch. operator_decide captures it into the
    # state; execute_transaction refuses if it changed (abort/panic/steer happened).
    control_epoch_getter: Callable[[], int] | None = None


def get_deps(config: dict[str, Any] | None) -> GraphDeps:
    """Pull GraphDeps out of a LangGraph run config."""
    if not config or "configurable" not in config or "deps" not in config["configurable"]:
        raise RuntimeError("GraphDeps missing from run config (configurable.deps)")
    return config["configurable"]["deps"]
