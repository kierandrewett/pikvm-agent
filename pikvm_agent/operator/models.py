"""Model-lane selection.

The operator config names model "lanes" (cheap / default / hard / ...). A caller
passes a hint; this resolves it to a concrete model id, degrading gracefully to
the default lane and then to any configured lane.
"""

from __future__ import annotations

from pikvm_agent.config import OperatorConfig
from pikvm_agent.core.errors import OperatorError

__all__ = ["select_lane"]


def select_lane(operator_cfg: OperatorConfig, hint: str = "default") -> str:
    """Return the model id for the lane named by ``hint``.

    Falls back to the ``default`` lane, then to any configured lane. Raises
    :class:`OperatorError` if no lanes are configured at all.
    """
    lanes = operator_cfg.lanes
    if not lanes:
        raise OperatorError("operator config has no model lanes")
    lane = lanes.get(hint) or lanes.get("default") or next(iter(lanes.values()))
    return lane.model
