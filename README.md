# PiKVM Agent

A **transactional computer-use runtime** for a physical machine controlled
through [PiKVM](https://pikvm.org). PiKVM exposes only **raw video, raw keyboard,
and raw mouse** — no DOM, no accessibility APIs, no OS/application APIs. This
runtime is built to be robust to that nondeterminism.

## Core invariant

```text
No action is valid unless the world still matches the frame it was planned against.
No success is real unless our verifier proves it.
No consequential action happens without explicit approval.
```

## What we own vs. what we use

We own the **daemon, MCP server, PiKVM client, policy engine, action execution,
verification, the Atlas memory loop, session logs, and the human approval flow.**

Third-party libraries are bounded adapters that produce *evidence*, never
decisions:

| Library | Role |
| --- | --- |
| OmniParser V2 | screenshot → structured UI element map |
| PaddleOCR (PP-OCRv5) | OCR text + boxes for read-back / verification |
| LangGraph | state graph, routing, checkpointing, interrupts/resume |
| MCP Python SDK | MCP protocol plumbing only |
| FastAPI | local daemon API |
| OpenRouter | structured multimodal operator decisions only |

## Architecture

```text
Claude Code / Codex
  ├── atlas MCP          durable knowledgebase (before/after a task)
  └── pikvm MCP          thin stdio facade
        ↓
PiKVM Agent Daemon (FastAPI + LangGraph)
  observe_frame → parse_screen → detect_state → operator_decide
    → validate_decision → policy_gate → [human_interrupt]
    → execute_transaction → verify_result → continue / recover / finalise
        ↓
PiKVM   raw video + raw keyboard + raw mouse
```

The MCP server is a thin facade. The **daemon** owns long-running state,
background watchers, the frame store, the operator loop, approvals, and
execution.

## Install

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
cd ~/dev/pikvm-agent
uv venv --python 3.12
uv pip install -e '.[dev]'
```

The **core** install needs no native ML toolchain — the runtime defaults to
PiKVM's built-in tesseract OCR (`/api/streamer/snapshot?ocr=1`) and degrades
gracefully when the OmniParser server and PaddleOCR are not running.

To enable the local ML vision stack (optional):

```bash
uv pip install -e '.[vision]'
# PaddleOCR also needs a matching paddlepaddle wheel installed by hand:
#   uv pip install paddlepaddle            # CPU
#   uv pip install paddlepaddle-gpu        # CUDA (see PaddleOCR docs for the index URL)
```

## Configuration

Copy `config.example.yaml` to `config.yaml` (or set `PIKVM_AGENT_CONFIG`) and set
PiKVM credentials via the `PIKVM_USER` / `PIKVM_PASSWORD` environment variables.
See [`docs/PLAN.md`](docs/PLAN.md) for the full design and build order.

## Status

Built in phases (see `docs/PLAN.md` → *Build order*). This repository tracks that
plan closely; see `AGENTS.md` for the implementation directive.
