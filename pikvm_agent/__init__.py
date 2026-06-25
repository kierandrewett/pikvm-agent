"""PiKVM Agent — a transactional computer-use runtime.

This package owns the daemon, the MCP facade, the PiKVM client, the policy
engine, action execution, verification, and the Atlas memory loop. Third-party
libraries (OmniParser, PaddleOCR, LangGraph, OpenRouter) are used as bounded
adapters that produce *evidence*; only this runtime makes decisions, executes
actions, and declares an outcome verified.
"""

__version__ = "0.1.0"
