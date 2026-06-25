"""Typed errors for the runtime.

These name the failure modes the daemon must distinguish: a stale plan, a policy
block, a missing approval, an unactionable target, a verification failure. The
graph and executor branch on these rather than on string matching.
"""

from __future__ import annotations


class PikvmAgentError(Exception):
    """Base class for all runtime errors."""


class ConfigError(PikvmAgentError):
    """Configuration could not be loaded or is invalid."""


class BackendError(PikvmAgentError):
    """The PiKVM backend (HTTP/WS) failed or is unavailable."""


class StaleFrameError(PikvmAgentError):
    """A decision was planned against a frame/world that no longer matches.

    The cardinal invariant: no action is valid unless the world still matches the
    frame it was planned against.
    """

    def __init__(self, reason: str = "frame changed") -> None:
        super().__init__(reason)
        self.reason = reason


class PolicyBlockedError(PikvmAgentError):
    """A hard policy rule blocked the action outright (not approvable here)."""

    def __init__(self, reason: str, category: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category


class ApprovalRequiredError(PikvmAgentError):
    """A consequential action requires explicit human approval before executing."""

    def __init__(self, reason: str, category: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.category = category


class ActionabilityError(PikvmAgentError):
    """A target failed an actionability check (not visible/stable/unique/...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FocusLostError(PikvmAgentError):
    """The intended typing target lost focus (nothing landed on screen)."""


class VerificationError(PikvmAgentError):
    """A postcondition verifier proved the action did not produce its result."""

    def __init__(self, reason: str, status: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class OperatorError(PikvmAgentError):
    """The operator returned a malformed or unvalidatable decision."""


class SessionNotFoundError(PikvmAgentError):
    """No session exists for the given id."""
