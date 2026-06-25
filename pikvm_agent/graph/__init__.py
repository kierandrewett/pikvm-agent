"""LangGraph orchestration: state, nodes, routing, interrupts, checkpointing.

LangGraph owns control flow (the StateGraph, conditional edges, interrupt/resume,
checkpointing). It does NOT contain PiKVM logic — nodes delegate to the owned
services (backend, screen parser, operator, policy, executor) via GraphDeps.
"""
