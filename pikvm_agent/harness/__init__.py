"""Isolated VNC-backed accuracy lab for the PiKVM MCP runtime."""

from pikvm_agent.harness.protocol import OracleSnapshot
from pikvm_agent.harness.scoring import AccuracyScore, score_snapshot

__all__ = ["AccuracyScore", "OracleSnapshot", "score_snapshot"]
