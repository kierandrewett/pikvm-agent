# PiKVM Agent Runtime — Claude Code Implementation Prompt

You are building a PiKVM-backed computer-use runtime for Kieran.

This is **not** a generic `screenshot/click/type` MCP server. Build a **transactional computer-use runtime** for a physical machine controlled through PiKVM.

PiKVM gives only:

```text
raw video
raw keyboard
raw mouse
```

Do **not** assume DOM access, browser DevTools, accessibility APIs, Windows UI Automation, semantic OS APIs, or application APIs.

The runtime must be robust to nondeterminism:

```text
The screen can change between observation and action.
The keyboard state can be wrong.
The focused target can change.
OCR can lie.
A popup can appear over the target.
An app can launch in the background.
The LLM can be correct about the old frame and wrong about the current frame.
```

## Core invariant

```text
No action is valid unless the world still matches the frame it was planned against.
No success is real unless our verifier proves it.
No consequential action happens without explicit approval.
```

## Ownership rule

We own the daemon and MCP server.

Use external libraries aggressively, but do not let them own the runtime:

```text
Own:
  daemon
  MCP server
  PiKVM client
  policy engine
  action execution
  verification
  Atlas memory loop
  session logs
  human approval flow

Use libraries for:
  screen parsing
  OCR
  graph orchestration
  checkpointing
  MCP protocol plumbing
  HTTP API plumbing
  structured LLM calls
```

Do not use OmniTool as the main product. Do not let OmniParser or PaddleOCR execute anything. Do not let LangGraph contain raw PiKVM logic directly. Only our daemon talks to PiKVM and only our daemon executes keyboard/mouse.

---

# High-level architecture

```text
Claude Code / Codex
  ├── atlas MCP
  │     durable knowledgebase
  │
  └── pikvm MCP
        thin stdio facade
        ↓
PiKVM Agent Daemon
  ├── FastAPI local API
  ├── LangGraph orchestration
  ├── OmniParser screen parser
  ├── PaddleOCR OCR service
  ├── OpenRouter operator
  ├── policy / approval gate
  ├── visual locator / actionability checks
  ├── guarded transaction executor
  ├── PiKVM raw HID executor
  ├── session replay/eval store
  └── Atlas memory export
        ↓
PiKVM
  raw video + raw keyboard + raw mouse
```

The MCP server is a thin facade. The daemon owns long-running state, watchers, frame store, operator loop, approvals, and execution.

---

# Existing Atlas MCP server

Kieran will run his knowledgebase MCP server, Atlas, in Claude Code or Codex alongside this PiKVM MCP server.

Use Atlas as durable memory before and after PiKVM sessions. Do not query Atlas inside the fast click/type loop.

## Atlas MCP config

`.mcp.json` / Claude Code form:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "/home/kieran/dev/atlas-2/atlas-app-v16/target/release/atlas",
      "args": ["mcp", "/home/kieran/Documents/Atlas/Capgemini"]
    }
  }
}
```

CLI form:

```bash
claude mcp add atlas -- \
  /home/kieran/dev/atlas-2/atlas-app-v16/target/release/atlas \
  mcp /home/kieran/Documents/Atlas/Capgemini
```

Prerequisites:

```bash
cd ~/dev/atlas-2/atlas-app-v16
cargo build --release -p atlas
```

The Capgemini vault already exists at:

```text
/home/kieran/Documents/Atlas/Capgemini
```

## Atlas tools

Treat `tools/list` at startup as authoritative, but the expected tools are:

```text
atlas_page       create/update/edit markdown pages
atlas_search     keyword/full-text search; parameter is text, not query
atlas_query      structured query over pages/objects
atlas_object     read/write structured object
atlas_health     reindex / health-check
atlas_statement  write/structured surface
atlas_schema     write/structured surface
atlas_import     write/structured surface
atlas_propose    write/structured surface
atlas_command    write/structured surface
```

Write tools:

```text
atlas_page
atlas_object
atlas_statement
atlas_schema
atlas_import
atlas_propose
atlas_command
```

After any write tool, call:

```text
atlas_health
```

## Atlas gotchas

```text
Two memory layers:
  curated topic pages: memory/<topic>.md
  quick-capture notes: memory/notes/<slug>.md

Pages are page/<slug>.md with YAML frontmatter.

atlas_page create takes only path + title.
The body goes in a follow-up update/edit call.

Search needs the index built.
atlas_search / atlas_query may return stale or empty results until atlas_health / reindex runs.

Search param is text, not query.

The separately-running atlas web UI reads a shared on-disk index and may not watch external writes.
Pages created by MCP may not appear in an open UI until reindex.
```

## Atlas workflow

Before a PiKVM task:

```text
1. Use atlas_search / atlas_query for related prior context.
2. Prefer curated pages under memory/<topic>.md.
3. Distill only relevant incidents/playbooks into pikvm_start_task.
```

During a PiKVM task:

```text
1. Use pikvm_start_task / pikvm_continue / pikvm_observe / pikvm_approve.
2. Do not use raw debug HID tools unless debugging the harness itself.
3. If approval is requested, inspect screenshot/event log/reason before approving.
```

After a PiKVM task:

```text
1. Call pikvm_export_memory_update.
2. If useful, write durable lessons to Atlas with atlas_page.
3. Run atlas_health after Atlas write tools.
4. Do not store secrets, raw screenshots, credentials, private message bodies, or API keys in Atlas.
```

---

# Combined MCP config

Claude Code `.mcp.json` should eventually include both servers:

```json
{
  "mcpServers": {
    "atlas": {
      "command": "/home/kieran/dev/atlas-2/atlas-app-v16/target/release/atlas",
      "args": ["mcp", "/home/kieran/Documents/Atlas/Capgemini"]
    },
    "pikvm": {
      "command": "/home/kieran/dev/pikvm-agent/.venv/bin/python",
      "args": [
        "-m",
        "pikvm_agent.mcp_server"
      ]
    }
  }
}
```

Codex TOML example:

```toml
[mcp_servers.atlas]
command = "/home/kieran/dev/atlas-2/atlas-app-v16/target/release/atlas"
args = ["mcp", "/home/kieran/Documents/Atlas/Capgemini"]
startup_timeout_sec = 10
tool_timeout_sec = 120
enabled = true
default_tools_approval_mode = "prompt"

[mcp_servers.pikvm]
command = "/home/kieran/dev/pikvm-agent/.venv/bin/python"
args = ["-m", "pikvm_agent.mcp_server"]
startup_timeout_sec = 15
tool_timeout_sec = 900
enabled = true
default_tools_approval_mode = "prompt"
```

---

# Use these upstream patterns

## 1. OmniParser / OmniTool pattern

Use Microsoft OmniParser V2 for screen grounding. It is built to convert screenshots into structured UI elements, boxes, captions, and interactable regions for GUI agents.

Use the pattern:

```text
screenshot
  → OmniParser
  → element map
  → LLM receives screenshot + element IDs
  → LLM chooses element_id
  → harness resolves element_id to coordinates only if still valid
```

Prefer:

```json
{
  "type": "click_element",
  "element_id": "e17",
  "intent": "Dismiss the Teams update notification because it blocks the target region."
}
```

Avoid:

```json
{
  "type": "click",
  "x": 842,
  "y": 91
}
```

Do not use OmniTool as the product. Use OmniParser as a dependency/service. Our daemon owns PiKVM, policy, execution, verification, session logs, and approvals.

## 2. PaddleOCR / PP-OCRv5 pattern

Use PaddleOCR as the default OCR backend for:

```text
read-back verification
OCR overlays
popup detection
text-region monitoring
shell prompt comparison
Teams/Outlook/browser text detection
```

PaddleOCR provides text, confidence, boxes/polygons. Our verifier decides whether text was correctly typed.

## 3. LangGraph pattern

Use LangGraph for:

```text
StateGraph orchestration
conditional routing
checkpointing
interrupt/resume
human approval flow
recovery flow
```

Do not hand-roll the orchestration loop first. Use LangGraph nodes:

```text
observe_frame
parse_screen
detect_state
operator_decide
validate_decision
policy_gate
human_interrupt
execute_transaction
verify_result
recover
finalise
```

## 4. Playwright pattern

Copy the locator/actionability idea, not browser internals.

Playwright-style idea:

```text
Locator resolves an element at action time.
Action auto-waits for actionability.
Click happens only when visible, stable, unobscured, enabled, unique.
```

PiKVM equivalent:

```text
VisualLocator resolves VisualElement from ElementMap.
Actionability checks same_world, visible, stable, unobscured, unique, safe_target.
```

## 5. Magentic-UI / LangGraph HITL pattern

Use co-control and approval as first-class features:

```text
approve
edit
reject
respond
take_over
resume
abort
```

Human approval does not bypass freshness, policy, or actionability.

## 6. OSWorld / OSWorld-Human pattern

Build replay/eval fixtures from day one. Do not let the same failures regress.

Track:

```text
task_success
unsafe_action_blocked
human_escalation_count
stale_frame_refusals
typing_mismatch_count
wrong_region_count
operator_calls
screenshots_taken
transactions_executed
wall_clock_ms
operator_cost_estimate
```

## 7. OpenRouter pattern

Use OpenRouter for the inner multimodal operator with structured JSON output.

The operator proposes guarded transactions. It does not execute anything.

Validation pipeline:

```text
OpenRouter response
  → JSON parse
  → Pydantic schema validation
  → semantic validation
  → freshness validation
  → policy validation
  → visual actionability validation
  → execute
```

---

# Repository layout

Build this repo:

```text
pikvm-agent/
  pyproject.toml
  README.md
  AGENTS.md

  pikvm_agent/
    mcp_server.py              # official MCP SDK / FastMCP-style facade
    daemon.py                  # FastAPI app
    config.py
    runtime.py

    core/
      ports.py                 # owned interfaces
      models.py                # Pydantic models
      errors.py

    graph/
      state.py                 # LangGraph state schema
      graph.py                 # StateGraph construction
      nodes.py                 # graph node functions
      routing.py               # conditional edges
      interrupts.py            # approval / human takeover
      checkpoints.py           # SQLite checkpointer setup

    pikvm/
      client.py                # PiKVM HTTP/WebSocket client
      hid.py                   # raw keyboard/mouse execution
      keyboard_state.py        # keymap, Caps Lock, LEDs
      screenshot.py            # video frame capture

    vision/
      omniparser_client.py     # calls OmniParser server
      paddleocr_client.py      # OCR wrapper
      screen_parser.py         # merges OmniParser + PaddleOCR
      element_map.py           # VisualElement schema
      set_of_marks.py          # debug overlay image
      visual_locator.py        # Playwright-like visual locator
      actionability.py         # visible/stable/unobscured checks
      mode_detector.py         # VS Code / Teams / Outlook / terminal modes
      frame_diff.py            # hashes, region monitoring
      prompt_injection.py      # visible instruction scanner

    operator/
      openrouter.py            # OpenRouter API client
      schemas.py               # Pydantic decision schemas
      prompts.py               # operator prompt builder
      models.py                # model lane config

    policy/
      safety.py                # hard-coded risk policy
      approvals.py             # approval request/result models
      risk.py

    executor/
      transactions.py          # guarded transaction executor
      typing.py                # text injection + verification
      verification.py          # postcondition verifiers
      recovery.py              # pager, popup, lost focus recovery

    memory/
      atlas_export.py          # session → Atlas markdown updates
      templates.py

    store/
      sqlite.py
      trace.py
      frames.py

  webui/
    src/
      LiveView.tsx
      EventFeed.tsx
      ApprovalQueue.tsx
      TakeoverControls.tsx

  third_party/
    OmniParser/                # clone/submodule, use directly

  bench/
    incidents/
      E1_terminal_find_readme_symbol_case/
      E2_teams_devices_stale_screen/
      E3_capslock_windows_app_background/
      E4_shell_prompt_false_mismatch/
      E5_truncated_prompt_capslock/
      E6_vscode_quickopen_wrong_region/
      E7_slow_long_message_voice/
      E8_teams_autoformat/
      E9_git_pager/
      E10_scroll_noop/
```

---

# Dependencies

Use `uv` if possible.

`pyproject.toml`:

```toml
[project]
name = "pikvm-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "httpx",
  "websockets",
  "pydantic>=2",
  "pydantic-settings",
  "pillow",
  "opencv-python",
  "numpy",
  "imagehash",
  "paddleocr",
  "paddlex",
  "langgraph",
  "langchain-core",
  "langgraph-checkpoint-sqlite",
  "aiosqlite",
  "mcp[cli]",
  "python-multipart",
  "rich",
  "typer",
  "orjson",
  "pytest",
  "pytest-asyncio"
]
```

PaddleOCR may require installing the correct PaddlePaddle CPU/GPU package separately depending on the machine.

Example:

```bash
# CPU
python -m pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

# CUDA example; check current PaddleOCR/PaddlePaddle docs for exact CUDA package
python -m pip install paddlepaddle-gpu -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

python -m pip install paddleocr
```

---

# Core ports

Define owned interfaces first.

```python
# pikvm_agent/core/ports.py

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pikvm_agent.core.models import (
    FrameRecord,
    ElementMap,
    OCRResult,
    OperatorRequest,
    OperatorDecision,
    GuardedTransaction,
    TransactionResult,
)


class ComputerBackend(Protocol):
    async def screenshot(self) -> FrameRecord: ...
    async def keypress(self, keys: list[str]) -> None: ...
    async def type_text(self, text: str) -> None: ...
    async def click(self, x: int, y: int, button: str = "left") -> None: ...
    async def move_mouse(self, x: int, y: int) -> None: ...
    async def scroll(self, dx: int = 0, dy: int = 0) -> None: ...


class OCRProvider(Protocol):
    async def ocr(self, image_path: Path) -> OCRResult: ...


class ScreenElementProvider(Protocol):
    async def parse_elements(self, image_path: Path) -> ElementMap: ...


class OperatorProvider(Protocol):
    async def decide(self, request: OperatorRequest) -> OperatorDecision: ...


class TransactionExecutor(Protocol):
    async def execute(self, tx: GuardedTransaction) -> TransactionResult: ...
```

Adapters:

```text
ComputerBackend       → PiKVMBackend
OCRProvider           → PaddleOCRProvider
ScreenElementProvider → OmniParserProvider
OperatorProvider      → OpenRouterOperator
```

---

# OmniParser integration

Use Microsoft OmniParser V2 / OmniTool components directly as a screen parser.

## Setup sketch

```bash
mkdir -p ~/dev
cd ~/dev

git clone https://github.com/microsoft/OmniParser.git
cd OmniParser

conda create -n omni python==3.12 -y
conda activate omni
pip install -r requirements.txt

rm -rf weights/icon_detect weights/icon_caption weights/icon_caption_florence
for folder in icon_caption icon_detect; do
  huggingface-cli download microsoft/OmniParser-v2.0 \
    --local-dir weights \
    --repo-type model \
    --include "$folder/*"
done
mv weights/icon_caption weights/icon_caption_florence

cd omnitool/omniparserserver
python -m omniparserserver
```

Inspect the actual OmniParser server routes and normalize its output. Do not assume the endpoint below is exact until checked.

```python
# pikvm_agent/vision/omniparser_client.py

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel


class OmniParserElement(BaseModel):
    id: str | None = None
    bbox: list[float] | list[int]
    text: str | None = None
    caption: str | None = None
    type: str | None = None
    confidence: float | None = None
    raw: dict[str, Any] = {}


class OmniParserResult(BaseModel):
    elements: list[OmniParserElement]
    raw: dict[str, Any]


class OmniParserClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def parse_image(self, image_path: Path) -> OmniParserResult:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/parse",
                json={"image": image_b64},
            )
            resp.raise_for_status()
            raw = resp.json()

        return self._normalize(raw)

    def _normalize(self, raw: dict[str, Any]) -> OmniParserResult:
        elements: list[OmniParserElement] = []

        candidates = (
            raw.get("elements")
            or raw.get("parsed_content")
            or raw.get("boxes")
            or raw.get("detections")
            or []
        )

        for i, item in enumerate(candidates):
            bbox = item.get("bbox") or item.get("box") or item.get("coordinates")
            if not bbox:
                continue

            elements.append(
                OmniParserElement(
                    id=item.get("id") or f"omni_{i}",
                    bbox=bbox,
                    text=item.get("text"),
                    caption=item.get("caption") or item.get("description"),
                    type=item.get("type") or item.get("label"),
                    confidence=item.get("confidence") or item.get("score"),
                    raw=item,
                )
            )

        return OmniParserResult(elements=elements, raw=raw)
```

---

# PaddleOCR integration

Use PaddleOCR for OCR and read-back evidence.

```python
# pikvm_agent/vision/paddleocr_client.py

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel


class OCRLine(BaseModel):
    text: str
    confidence: float | None = None
    bbox: list[int] | list[list[int]] | None = None
    raw: dict[str, Any] | None = None


class OCRResult(BaseModel):
    lines: list[OCRLine]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class PaddleOCRClient:
    def __init__(self, lang: str = "en", device: str | None = None):
        from paddleocr import PaddleOCR

        kwargs: dict[str, Any] = {
            "lang": lang,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }

        if device:
            kwargs["device"] = device

        self.ocr = PaddleOCR(**kwargs)

    def predict(self, image_path: Path) -> OCRResult:
        output = self.ocr.predict(str(image_path))
        lines: list[OCRLine] = []

        for res in output:
            if hasattr(res, "json") and callable(res.json):
                data = res.json()
            elif hasattr(res, "to_json") and callable(res.to_json):
                data = res.to_json()
            else:
                data = getattr(res, "res", None) or {}

            raw_res = data.get("res", data) if isinstance(data, dict) else {}

            texts = raw_res.get("rec_texts") or []
            scores = raw_res.get("rec_scores") or []
            boxes = raw_res.get("rec_boxes") or raw_res.get("rec_polys") or []

            for i, text in enumerate(texts):
                box = boxes[i] if i < len(boxes) else None
                if hasattr(box, "tolist"):
                    box = box.tolist()

                lines.append(
                    OCRLine(
                        text=str(text),
                        confidence=float(scores[i]) if i < len(scores) else None,
                        bbox=box,
                        raw=None,
                    )
                )

        return OCRResult(lines=lines)
```

PaddleOCR is not the verifier. It produces OCR evidence. Our verifier classifies the result.

---

# Composite screen parser

Merge OmniParser + PaddleOCR into an `ElementMap`.

```python
# pikvm_agent/vision/screen_parser.py

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel
from typing import Literal


class BBox(BaseModel):
    x: int
    y: int
    w: int
    h: int


class VisualElement(BaseModel):
    id: str
    frame_id: int
    world_version: int
    bbox: BBox
    kind: Literal[
        "button",
        "input",
        "editor",
        "terminal_prompt",
        "menu_item",
        "tab",
        "toast",
        "modal",
        "notification",
        "close_button",
        "send_button",
        "text",
        "unknown",
    ]
    text: str | None = None
    caption: str | None = None
    confidence: float = 0.0
    source: list[str]
    app_hint: str | None = None


class ElementMap(BaseModel):
    frame_id: int
    world_version: int
    elements: list[VisualElement]
    ocr_text: str


class CompositeScreenParser:
    def __init__(self, omniparser, paddleocr):
        self.omniparser = omniparser
        self.paddleocr = paddleocr

    async def parse(self, image_path: Path, frame_id: int, world_version: int) -> ElementMap:
        omni = await self.omniparser.parse_image(image_path)
        ocr = self.paddleocr.predict(image_path)

        elements: list[VisualElement] = []

        for i, item in enumerate(omni.elements):
            bbox = self._bbox_from_omni(item.bbox)
            kind = self._kind_from_caption(item.type, item.caption, item.text)

            elements.append(
                VisualElement(
                    id=f"e{i}",
                    frame_id=frame_id,
                    world_version=world_version,
                    bbox=bbox,
                    kind=kind,
                    text=item.text,
                    caption=item.caption,
                    confidence=item.confidence or 0.5,
                    source=["omniparser"],
                )
            )

        start = len(elements)
        for j, line in enumerate(ocr.lines):
            bbox = self._bbox_from_ocr(line.bbox)
            if bbox is None:
                continue

            elements.append(
                VisualElement(
                    id=f"e{start + j}",
                    frame_id=frame_id,
                    world_version=world_version,
                    bbox=bbox,
                    kind="text",
                    text=line.text,
                    caption=None,
                    confidence=line.confidence or 0.5,
                    source=["paddleocr"],
                )
            )

        elements = self._merge_duplicates(elements)

        return ElementMap(
            frame_id=frame_id,
            world_version=world_version,
            elements=elements,
            ocr_text=ocr.text,
        )
```

Merge rule:

```text
OmniParser wins for interactable elements.
PaddleOCR wins for exact text.
If an OCR box overlaps an OmniParser button/input, attach the OCR text to that OmniParser element.
Keep OCR-only text as non-clickable evidence.
```

---

# LangGraph state

```python
# pikvm_agent/graph/state.py

from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    session_id: str
    task: str

    frame_id: int
    world_version: int
    frame_path: str
    frame_age_ms: int

    element_map: dict[str, Any]
    ocr_text: str

    active_app: str
    mode: str
    keyboard_state: dict[str, Any]

    recent_events: list[dict[str, Any]]
    recent_actions: list[dict[str, Any]]

    operator_decision: dict[str, Any]
    policy_result: dict[str, Any]
    transaction_result: dict[str, Any]
    verification_result: dict[str, Any]

    approval_request: dict[str, Any]
    approval_response: dict[str, Any]

    status: Literal[
        "running",
        "needs_approval",
        "human_takeover",
        "blocked",
        "failed",
        "done",
    ]

    error: str
```

---

# LangGraph graph

```python
# pikvm_agent/graph/graph.py

from langgraph.graph import StateGraph, START, END

from pikvm_agent.graph.state import AgentState
from pikvm_agent.graph.nodes import (
    observe_frame,
    parse_screen,
    detect_state,
    operator_decide,
    validate_decision,
    policy_gate,
    human_interrupt,
    execute_transaction,
    verify_result,
    recover,
    finalise,
)
from pikvm_agent.graph.routing import route_after_policy, route_after_verify


def build_graph(checkpointer):
    builder = StateGraph(AgentState)

    builder.add_node("observe_frame", observe_frame)
    builder.add_node("parse_screen", parse_screen)
    builder.add_node("detect_state", detect_state)
    builder.add_node("operator_decide", operator_decide)
    builder.add_node("validate_decision", validate_decision)
    builder.add_node("policy_gate", policy_gate)
    builder.add_node("human_interrupt", human_interrupt)
    builder.add_node("execute_transaction", execute_transaction)
    builder.add_node("verify_result", verify_result)
    builder.add_node("recover", recover)
    builder.add_node("finalise", finalise)

    builder.add_edge(START, "observe_frame")
    builder.add_edge("observe_frame", "parse_screen")
    builder.add_edge("parse_screen", "detect_state")
    builder.add_edge("detect_state", "operator_decide")
    builder.add_edge("operator_decide", "validate_decision")
    builder.add_edge("validate_decision", "policy_gate")

    builder.add_conditional_edges(
        "policy_gate",
        route_after_policy,
        {
            "approval": "human_interrupt",
            "blocked": "operator_decide",
            "allowed": "execute_transaction",
            "done": "finalise",
        },
    )

    builder.add_edge("human_interrupt", "execute_transaction")
    builder.add_edge("execute_transaction", "verify_result")

    builder.add_conditional_edges(
        "verify_result",
        route_after_verify,
        {
            "continue": "observe_frame",
            "recover": "recover",
            "approval": "human_interrupt",
            "done": "finalise",
            "failed": "finalise",
        },
    )

    builder.add_edge("recover", "observe_frame")
    builder.add_edge("finalise", END)

    return builder.compile(checkpointer=checkpointer)
```

---

# LangGraph human interrupt

```python
# pikvm_agent/graph/interrupts.py

from langgraph.types import interrupt


def approval_interrupt(payload: dict) -> dict:
    response = interrupt(payload)

    if not isinstance(response, dict):
        return {"type": "reject", "reason": "Invalid approval response"}

    return response
```

```python
# pikvm_agent/graph/nodes.py

from pikvm_agent.graph.interrupts import approval_interrupt


async def human_interrupt(state):
    request = state["approval_request"]

    response = approval_interrupt(
        {
            "session_id": state["session_id"],
            "frame_id": state["frame_id"],
            "world_version": state["world_version"],
            "risk": request.get("risk"),
            "reason": request.get("reason"),
            "proposed_action": request.get("action"),
            "screenshot_path": state.get("frame_path"),
            "allowed_decisions": ["approve", "edit", "reject", "respond"],
        }
    )

    if response["type"] == "approve":
        return {"approval_response": response, "status": "running"}

    if response["type"] == "edit":
        return {
            "approval_response": response,
            "recent_events": state.get("recent_events", [])
            + [{"type": "human_edit", "instruction": response.get("instruction")}],
            "status": "running",
        }

    if response["type"] == "respond":
        return {
            "approval_response": response,
            "recent_events": state.get("recent_events", [])
            + [{"type": "human_response", "message": response.get("message")}],
            "status": "running",
        }

    return {
        "approval_response": response,
        "status": "blocked",
        "error": response.get("reason", "Rejected by human"),
    }
```

After approval, re-check freshness, actionability, and policy. Approval is not a force-execute button.

---

# OpenRouter operator schema

The operator must return strict JSON only.

```python
# pikvm_agent/operator/schemas.py

from __future__ import annotations

from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field


class RiskAssessment(BaseModel):
    level: Literal["low", "medium", "high"]
    category: Literal[
        "navigation",
        "text_entry",
        "read_only_inspection",
        "local_file_edit",
        "terminal_read_only",
        "terminal_mutating",
        "communication_draft",
        "communication_send",
        "credential_entry",
        "sensitive_data_view",
        "sensitive_data_transmit",
        "account_or_permission_change",
        "software_installation",
        "system_setting_change",
        "power_or_firmware",
        "disk_or_partition",
        "financial_or_purchase",
        "legal_or_consent",
        "unknown",
    ]
    requires_human: bool
    reason: str = ""


class KeypressAction(BaseModel):
    type: Literal["keypress"]
    keys: list[str]


class TypeTextAction(BaseModel):
    type: Literal["type_text"]
    text: str


class ClickElementAction(BaseModel):
    type: Literal["click_element"]
    element_id: str | None = None
    locator: dict | None = None


class WaitAction(BaseModel):
    type: Literal["wait"]
    ms: int = Field(ge=50, le=5000)


class WaitForModeAction(BaseModel):
    type: Literal["wait_for_mode"]
    mode: str
    timeout_ms: int = Field(ge=100, le=10000)


Action = Annotated[
    Union[
        KeypressAction,
        TypeTextAction,
        ClickElementAction,
        WaitAction,
        WaitForModeAction,
    ],
    Field(discriminator="type"),
]


class OperatorDecision(BaseModel):
    based_on_frame_id: int
    based_on_world_version: int
    intent: str
    state_assessment: dict
    risk: RiskAssessment
    preconditions: dict
    actions: list[Action]
    postconditions: dict
    fallback: str | None = None
```

---

# Operator prompt rules

Every OpenRouter operator prompt must include:

```text
You are controlling a physical computer through PiKVM raw video, raw keyboard, and raw mouse.
You do not have DOM, accessibility APIs, browser DevTools, OS APIs, or application APIs.
Prefer keyboard shortcuts and visual element IDs over raw coordinates.
Return only valid JSON matching the schema.
Every decision must reference based_on_frame_id and based_on_world_version.
Never send, submit, delete, purchase, authenticate, change security settings, enter credentials, or perform destructive actions without human approval.
Escalate when uncertain.
```

The model gets:

```json
{
  "task": "...",
  "frame": {
    "id": 18429,
    "world_version": 702,
    "image": "<base64 png>",
    "age_ms": 86
  },
  "detected_state": {
    "active_app": "vscode",
    "mode": "vscode.editor",
    "keyboard": {
      "layout": "uk",
      "caps_lock": false,
      "num_lock": true
    },
    "blocking_events": []
  },
  "visual_elements": [],
  "recent_events": [],
  "retrieved_playbooks": [],
  "policy": {}
}
```

Example valid decision:

```json
{
  "based_on_frame_id": 18429,
  "based_on_world_version": 702,
  "intent": "Open VS Code Quick Open and type the README path.",
  "state_assessment": {
    "active_app": "vscode",
    "mode": "vscode.editor",
    "confidence": 0.88
  },
  "risk": {
    "level": "low",
    "category": "navigation",
    "requires_human": false,
    "reason": ""
  },
  "preconditions": {
    "active_app": ["vscode"],
    "mode": ["vscode.editor"],
    "no_blocking_popup": true,
    "keyboard_state_known": true
  },
  "actions": [
    {"type": "keypress", "keys": ["CTRL", "P"]},
    {"type": "wait_for_mode", "mode": "vscode.quick_open", "timeout_ms": 1000},
    {"type": "type_text", "text": "oel9-cis/readme.md"}
  ],
  "postconditions": {
    "verify_mode": "vscode.quick_open",
    "verify_target": "vscode_quick_open_input",
    "expected_text": "oel9-cis/readme.md"
  },
  "fallback": "If Quick Open does not appear, reobserve and check for modal or lost focus."
}
```

---

# Policy gate

Hard-code the safety policy. Do not make it prompt-only.

Always require human approval for:

```text
communication_send
credential_entry
sensitive_data_transmit
account_or_permission_change
software_installation
system_setting_change
power_or_firmware
disk_or_partition
financial_or_purchase
legal_or_consent
terminal_mutating
sudo
delete
file_external_upload
```

Always block unless explicitly in task scope:

```text
format_disk
partition_disk
erase
firmware_update
disable_security
copy_secret
submit_payment
```

Policy gate sketch:

```python
async def policy_gate(state):
    decision = state["operator_decision"]

    if decision["based_on_frame_id"] != state["frame_id"]:
        return {
            "policy_result": {"status": "blocked", "reason": "stale_frame"},
            "status": "blocked",
        }

    if decision["based_on_world_version"] != state["world_version"]:
        return {
            "policy_result": {"status": "blocked", "reason": "stale_world"},
            "status": "blocked",
        }

    risk = classify_local_risk(decision, state)

    if risk["requires_human"]:
        return {
            "policy_result": {"status": "approval_required", **risk},
            "approval_request": {
                "risk": risk["category"],
                "reason": risk["reason"],
                "action": decision,
            },
            "status": "needs_approval",
        }

    if risk["blocked"]:
        return {
            "policy_result": {"status": "blocked", **risk},
            "status": "blocked",
        }

    return {
        "policy_result": {"status": "allowed", **risk},
        "status": "running",
    }
```

---

# VisualLocator and actionability

Implement a Playwright-style visual locator.

```python
class VisualLocator(BaseModel):
    element_id: str | None = None
    text: str | None = None
    kind: str | None = None
    region: str | None = None
    app_hint: str | None = None
```

Actionability checks:

```text
unique
visible
stable for N frames
unobscured by modal/toast
same frame/world version
safe target
```

Execution sketch:

```python
async def click_element(state, action):
    element = await visual_locator.resolve(
        action.element_id or action.locator,
        frame_id=state["frame_id"],
        world_version=state["world_version"],
    )

    check = await actionability.check(element)
    if not check.ok:
        return {"status": "blocked", "reason": check.reason}

    await pikvm.hid.click(element.bbox.center())
```

Raw coordinate click should be debug-only or last resort.

---

# Frame freshness and world versioning

Every screenshot gets:

```json
{
  "frame_id": 18429,
  "world_version": 702,
  "captured_at": "2026-06-25T14:03:12.481Z",
  "monotonic_ms": 93881712,
  "image_sha256": "...",
  "screen_hash": "...",
  "active_app_guess": "vscode",
  "mode_guess": "vscode.editor",
  "keyboard_state": {
    "caps_lock": false,
    "num_lock": true,
    "layout": "uk"
  }
}
```

Increment `world_version` when:

```text
active app changed
modal appeared
toast appeared over target
keyboard state changed
terminal entered pager
screen went black
focused mode changed
```

Before executing any action:

```python
def assert_fresh(decision, current_state):
    if decision.based_on_frame_id != current_state.frame_id:
        raise StaleFrameError("frame changed")
    if decision.based_on_world_version != current_state.world_version:
        raise StaleFrameError("world changed")
```

---

# Background watchers

Run watchers in the daemon, not in the MCP server.

Watcher loop:

```python
async def watcher_loop():
    while True:
        frame = await pikvm.capture_frame()
        previous = store.latest_frame()

        changes = diff_frame(previous, frame)
        events = classify_changes(changes, frame)

        store.save_frame(frame)

        for event in events:
            store.append_event(event)
            if event.severity in {"invalidate_plan", "interrupt", "approval_required"}:
                state.increment_world_version(event)

        await asyncio.sleep(config.watch.interval_ms / 1000)
```

Monitor regions:

```yaml
regions:
  active_window_title:
    bbox_pct: [0.00, 0.00, 1.00, 0.08]
  center_modal:
    bbox_pct: [0.20, 0.15, 0.60, 0.70]
  bottom_right_toasts:
    bbox_pct: [0.65, 0.55, 0.35, 0.40]
  taskbar:
    bbox_pct: [0.00, 0.92, 1.00, 0.08]
  vscode_quick_open:
    bbox_pct: [0.20, 0.05, 0.60, 0.25]
  terminal_input_line:
    bbox_pct: [0.00, 0.75, 1.00, 0.25]
  teams_compose:
    bbox_pct: [0.20, 0.72, 0.75, 0.25]
  outlook_compose:
    bbox_pct: [0.10, 0.55, 0.85, 0.40]
```

Events:

```text
blocking_popup_detected
active_app_changed
mode_changed
caps_lock_changed
terminal_pager_detected
background_window_opened
possible_prompt_injection
windows_update_modal
credential_prompt
unknown_interruption
```

---

# Text entry and verification

Text entry is its own subsystem.

Decision tree:

```text
Need to enter text
  ↓
Is target focus known?
  no → refuse and reobserve
  yes
  ↓
Is keyboard layout known?
  no → use print/paste if safe, otherwise refuse
  yes
  ↓
Does text contain high-risk shell/control chars?
  yes → prefer print/paste; require exact verification
  no → raw HID allowed
  ↓
Does Enter submit/send/execute?
  yes → split typing and Enter into separate transactions
  no → batch allowed
```

High-risk characters:

```text
| & ; > < $ ` ~ * " ' \ ! { } [ ] ( )
```

Required result enums:

```text
verified_exact
verified_safe_normalized
verified_with_warnings
unverified_ambiguous
unverified_wrong_region
unverified_truncated
failed_symbol_mismatch
failed_case_mismatch
failed_keyboard_layout
failed_focus_lost
failed_stale_frame
blocked_by_policy
needs_human
```

Verification sketch:

```python
HIGH_RISK_CHARS = set("|&;><$`~*\"'\\!{}[]()")


def requires_strict_verification(text: str) -> bool:
    return any(ch in HIGH_RISK_CHARS for ch in text)


def verify_text(intended: str, observed: str, mode: str) -> dict:
    observed_norm = normalize_ocr_glyphs(observed)

    if mode in {"terminal.readline", "vscode.terminal"}:
        observed_norm = strip_shell_prompt(observed_norm)

    if is_wrong_region(observed_norm, intended):
        return {"status": "unverified_wrong_region", "safe_to_continue": False}

    if is_truncated(observed_norm, intended):
        return {"status": "unverified_truncated", "safe_to_continue": False}

    if requires_strict_verification(intended):
        if intended == observed_norm:
            return {"status": "verified_exact", "safe_to_continue": True}

        return {
            "status": classify_strict_mismatch(intended, observed_norm),
            "safe_to_continue": False,
            "intended": intended,
            "observed": observed_norm,
        }

    if safe_normalized_equal(intended, observed_norm):
        return {"status": "verified_safe_normalized", "safe_to_continue": True}

    return {
        "status": "failed_text_mismatch",
        "safe_to_continue": False,
        "intended": intended,
        "observed": observed_norm,
    }
```

Never return “typed and verified” unless a `verified_*` status was produced.

---

# App and mode detection

Detect modes explicitly:

```text
unknown
windows.desktop
windows.start_search
windows.update_modal
system.notification
vscode.editor
vscode.quick_open
vscode.terminal
terminal.readline
terminal.pager
browser.page
browser.address_bar
browser.form
outlook.inbox
outlook.compose
teams.chat
teams.compose
credential_prompt
captcha_or_human_verification
installer.disk_selection
bios.uefi
```

Action constraints:

```yaml
type_text:
  allowed_modes:
    - terminal.readline
    - vscode.quick_open
    - vscode.editor
    - browser.address_bar
    - browser.form
    - teams.compose
    - outlook.compose
  blocked_modes:
    - terminal.pager
    - credential_prompt
    - captcha_or_human_verification
    - unknown

press_enter:
  split_transaction_in_modes:
    - terminal.readline
    - teams.compose
    - outlook.compose
    - browser.form
    - installer.disk_selection
```

Terminal pager recovery:

```yaml
terminal.pager:
  recovery:
    - keypress: ["q"]
    - wait_for_mode: terminal.readline
```

---

# MCP server

Use the official Python MCP SDK / FastMCP-style server. Do not hand-roll MCP JSON-RPC.

```python
# pikvm_agent/mcp_server.py

from __future__ import annotations

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pikvm-agent", json_response=True)

DAEMON_URL = "http://127.0.0.1:8765"


@mcp.tool()
async def pikvm_start_task(task: str, policy: dict | None = None, operator: dict | None = None) -> dict:
    """Start a guarded PiKVM computer-use session."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DAEMON_URL}/sessions",
            json={"task": task, "policy": policy or {}, "operator": operator or {}},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pikvm_continue(session_id: str) -> dict:
    """Continue a paused/running PiKVM session until next checkpoint, approval, or completion."""
    async with httpx.AsyncClient(timeout=900) as client:
        resp = await client.post(f"{DAEMON_URL}/sessions/{session_id}/continue")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pikvm_observe(session_id: str) -> dict:
    """Return current screen summary, frame id, world version, events, and screenshot path."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{DAEMON_URL}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pikvm_approve(session_id: str, approval_id: str, decision: dict) -> dict:
    """Approve/edit/reject/respond to a pending PiKVM approval request."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DAEMON_URL}/sessions/{session_id}/approvals/{approval_id}",
            json=decision,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pikvm_abort(session_id: str, reason: str = "") -> dict:
    """Abort a PiKVM session."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DAEMON_URL}/sessions/{session_id}/abort",
            json={"reason": reason},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def pikvm_export_memory_update(session_id: str) -> dict:
    """Export a safe Atlas memory update proposal from the session trace."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{DAEMON_URL}/sessions/{session_id}/memory-update")
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
```

Raw HID tools should be disabled by default:

```text
debug_pikvm_click
debug_pikvm_keypress
debug_pikvm_type_text
debug_pikvm_scroll
```

---

# FastAPI daemon

```python
# pikvm_agent/daemon.py

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="PiKVM Agent Daemon")


class StartSessionRequest(BaseModel):
    task: str
    policy: dict = {}
    operator: dict = {}


@app.post("/sessions")
async def start_session(req: StartSessionRequest):
    session = await runtime.start_session(req.task, req.policy, req.operator)
    return session.to_public_dict()


@app.post("/sessions/{session_id}/continue")
async def continue_session(session_id: str):
    return await runtime.continue_session(session_id)


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    return await runtime.get_session_summary(session_id)


@app.post("/sessions/{session_id}/approvals/{approval_id}")
async def approval(session_id: str, approval_id: str, decision: dict):
    return await runtime.submit_approval(session_id, approval_id, decision)


@app.post("/sessions/{session_id}/abort")
async def abort(session_id: str, body: dict):
    return await runtime.abort_session(session_id, body.get("reason", ""))


@app.get("/sessions/{session_id}/memory-update")
async def memory_update(session_id: str):
    return await runtime.export_memory_update(session_id)
```

---

# Runtime composition

```python
# pikvm_agent/runtime.py

class Runtime:
    @classmethod
    def from_config(cls) -> "Runtime":
        config = load_config()

        pikvm = PiKVMBackend(config.pikvm)

        paddleocr = PaddleOCRClient(
            lang=config.ocr.lang,
            device=config.ocr.device,
        )

        omniparser = OmniParserClient(
            base_url=config.omniparser.base_url,
            timeout_s=config.omniparser.timeout_s,
        )

        screen_parser = CompositeScreenParser(
            omniparser=omniparser,
            paddleocr=paddleocr,
        )

        operator = OpenRouterOperator(config.openrouter)
        policy = SafetyPolicyEngine(config.policy)

        executor = GuardedTransactionExecutor(
            backend=pikvm,
            policy=policy,
            screen_parser=screen_parser,
        )

        graph = build_graph(
            checkpointer=build_checkpointer(config),
        )

        return cls(
            config=config,
            graph=graph,
            store=SessionStore(config.store),
            services=Services(
                pikvm=pikvm,
                screen_parser=screen_parser,
                operator=operator,
                policy=policy,
                executor=executor,
            ),
        )
```

Libraries are instantiated inside our runtime. Libraries do not call each other directly. Libraries do not call PiKVM. Our services coordinate them.

---

# Config shape

```yaml
daemon:
  listen: "127.0.0.1:8765"
  session_dir: "/home/kieran/.local/share/pikvm-agent/sessions"
  sqlite_path: "/home/kieran/.local/share/pikvm-agent/state.sqlite3"

pikvm:
  base_url: "https://pikvm.local"
  verify_tls: false
  username_env: "PIKVM_USER"
  password_env: "PIKVM_PASSWORD"

omniparser:
  mode: "managed_child_process"
  base_url: "http://127.0.0.1:8000"
  health_url: "http://127.0.0.1:8000/health"
  timeout_s: 20
  command:
    - "/home/kieran/.conda/envs/omni/bin/python"
    - "-m"
    - "omniparserserver"
  cwd: "/home/kieran/dev/OmniParser/omnitool/omniparserserver"

ocr:
  provider: "paddleocr"
  lang: "en"
  device: "gpu"     # or cpu
  disable_doc_orientation: true
  disable_doc_unwarping: true
  disable_textline_orientation: true

operator:
  provider: "openrouter"
  api_key_env: "OPENROUTER_API_KEY"
  lanes:
    cheap:
      model: "qwen/qwen3-vl-8b-instruct"
    default:
      model: "qwen/qwen3-vl-32b-instruct"
    hard:
      model: "qwen/qwen3-vl-235b-a22b-thinking"

policy:
  default_profile: "read_only_diagnostics"
  require_human_for:
    - communication_send
    - credential_entry
    - terminal_mutating
    - sudo
    - delete
    - local_file_edit
    - software_installation
    - system_setting_change
    - power_or_firmware
    - disk_or_partition
    - financial_or_purchase
    - legal_or_consent

watchers:
  interval_ms: 350
  stable_frame_count: 2
  global_change_threshold: 0.08
```

---

# Regression incidents

Turn Kieran's evidence log into tests.

```text
E1 — terminal find README symbol/case mismatch
  Must not verify README→readme or |→~.
  Must block Enter after failed symbol/case verification.

E2 — Teams/Devices stale screen + double Enter
  Must block second Enter if screen/world changed after first Enter.
  Must auto-screenshot after action.

E3 — Caps Lock Windows App / background AVD
  Must read Caps Lock/key state before typing.
  Must record background window/open events.

E4 — shell prompt false mismatch
  Must strip leading shell prompt before comparing typed command.

E5 — truncated readback
  Truncated readback is unverified, not a destructive clear/retype.

E6 — VS Code Quick Open wrong OCR region
  OCR over results dropdown is wrong-region, not typed-wrong.

E7 — long Teams text too slow / wrong voice
  Long text should use fast paste/print path and a style card/playbook.

E8 — Teams pasted bullets half-formatted
  Teams compose playbook must account for Teams autoformatting pasted text.

E9 — git pager trap
  terminal.pager mode blocks shell command typing.
  Use q recovery and no-pager guidance.

E10 — scroll no-op
  scroll(direction="up", amount=5) must not become scroll(0,0).
```

Example tests:

```python
def test_E1_find_command_symbol_case_mismatch():
    result = verify_text(
        intended="find . -name 'README*' | sort && echo \"=== root ===\"",
        observed="find . -name 'readme*' ~ sort && echo @=== root ===@",
        mode="terminal.readline",
    )

    assert result["status"] in {"failed_symbol_mismatch", "failed_case_mismatch"}
    assert result["safe_to_continue"] is False


def test_E2_refuses_double_enter_on_world_change():
    tx = load_transaction("E2_double_enter")
    world = FakeWorld(initial_version=10)
    world.increment_after_first_action(event="active_app_changed")

    result = execute_transaction(tx, world)

    assert result.status == "failed_stale_frame"
    assert world.actions_executed == ["ENTER"]


def test_E9_blocks_shell_command_in_pager():
    state = WorldState(mode="terminal.pager")
    action = TypeTextAction(text="clear; git status")

    result = safety_gate.check_action(action, state)

    assert result.blocked
    assert "pager" in result.reason
```

---

# Build order

## Phase 1 — Own the shell first

Build:

```text
FastAPI daemon
MCP facade
config loader
session store
trace log
PiKVM screenshot capture
```

Acceptance:

```text
pikvm_start_task creates a session
pikvm_observe returns frame_id/world_version/screenshot_path
no OmniParser/OpenRouter required yet
```

## Phase 2 — Add library adapters

Build:

```text
PaddleOCRClient
OmniParserClient
CompositeScreenParser
ElementMap schema
Set-of-marks debug overlay
```

Acceptance:

```bash
pikvm-agent smoke-test --screenshot sample.png
```

Expected:

```json
{
  "ocr_lines": 42,
  "omniparser_elements": 31,
  "merged_elements": 58,
  "set_of_marks_path": "output/sample.marks.png"
}
```

## Phase 3 — Add LangGraph

Build:

```text
AgentState
StateGraph
checkpointing
interrupt wrapper
fake operator
replay backend
```

Acceptance:

```text
graph can run observe → parse → fake decision → policy → finalise
graph can pause on approval and resume
state survives restart via checkpoint
```

## Phase 4 — Add guarded transactions

Build:

```text
GuardedTransaction
freshness validation
policy validation
visual locator
actionability checker
post-action screenshot
verification result enums
```

Acceptance:

```text
action without frame_id/world_version is rejected
stale world blocks action
click_element resolves through ElementMap
raw coordinate click is debug-only
```

## Phase 5 — Add OpenRouter operator

Build:

```text
OpenRouterOperator
JSON Schema response
Pydantic validation
operator prompt
model lanes
schema retry
```

Acceptance:

```text
malformed response never executes
operator can propose low-risk navigation
risky send/delete/sudo requires LangGraph interrupt
```

## Phase 6 — Add E1–E10 regression tests

Acceptance:

```text
E1: README→readme and |→~ cannot verify
E2: double Enter is blocked after world change
E3: Caps Lock state is detected or raw typing is refused
E4: shell prompt $ is stripped before comparison
E5: truncated readback is unverified, not destructive retry
E6: VS Code dropdown OCR is wrong-region, not mismatch
E7: long Teams text uses fast paste/print path
E8: Teams autoformat is handled by compose playbook
E9: pager mode blocks shell commands
E10: scroll(direction, amount) cannot become scroll(0,0)
```

## Phase 7 — Add human console

Build:

```text
live frame
set-of-marks overlay
event feed
LangGraph interrupt approvals
takeover/resume
abort
memory export
```

Acceptance:

```text
Human can pause automation instantly.
Human can take over, fix UI, resume.
Approval decisions are persisted.
```

## Phase 8 — Atlas memory loop

Build:

```text
pikvm_export_memory_update
Atlas page templates
post-session incident/playbook exporter
AGENTS.md supervisor instructions
```

Acceptance:

```text
Claude/Codex can write durable playbooks/incidents to Atlas.
atlas_health is run after writes.
No secrets/screenshots/private message bodies are exported.
```

---

# AGENTS.md content

Put this in `AGENTS.md`:

```md
# PiKVM Agent Implementation Directive

We own the daemon and MCP server.

Use existing libraries aggressively, but do not let them own the runtime.

Core rule:
- The PiKVM daemon is our process.
- The PiKVM MCP server is our server.
- Only our daemon talks to PiKVM.
- Only our daemon executes keyboard/mouse.
- Only our daemon declares actions safe or verified.

Use libraries as bounded adapters:
- OmniParser: parse screenshots into UI elements, captions, and boxes.
- PaddleOCR: OCR text and text boxes for read-back, popup detection, and verification evidence.
- LangGraph: state graph, conditional routing, checkpointing, interrupts/resume.
- MCP Python SDK: MCP protocol plumbing only.
- FastAPI: local daemon API.
- OpenRouter: structured operator decisions only.

Do not:
- Use OmniTool as the main runtime.
- Let OmniParser produce executable actions.
- Let PaddleOCR decide whether typing succeeded.
- Let LangGraph nodes contain PiKVM-specific logic directly.
- Expose raw HID tools as normal MCP tools.
- Build a generic screenshot/click/type MCP server.

Architecture:
Claude Code/Codex talks to:
1. atlas MCP server for durable knowledgebase.
2. pikvm MCP server for high-level PiKVM sessions.

Atlas is used before and after sessions, not inside the fast click/type loop.

The PiKVM daemon runs:
observe_frame → parse_screen → detect_state → operator_decide → validate_decision → policy_gate → human_interrupt if needed → execute_transaction → verify_result → continue/recover/finalise.

Every operator decision must include:
- based_on_frame_id
- based_on_world_version
- intent
- risk
- preconditions
- actions
- postconditions

No action is valid unless the world still matches the frame it was planned against.
No success is real unless our verifier proves it.
No consequential action happens without explicit approval.
```

---

# Reference links for Claude Code

## OmniParser / OmniTool

- OmniParser GitHub: https://github.com/microsoft/OmniParser
- OmniTool README: https://github.com/microsoft/OmniParser/blob/master/omnitool/readme.md
- Microsoft Research OmniParser V2 article: https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/
- Microsoft Research OmniParser article: https://www.microsoft.com/en-us/research/articles/omniparser-for-pure-vision-based-gui-agent/
- Azure model catalog entry: https://ai.azure.com/catalog/models/microsoft-omniparser-v2-0

## PaddleOCR / PP-OCRv5

- PaddleOCR GitHub: https://github.com/PaddlePaddle/PaddleOCR
- PP-OCRv5 multilingual docs: https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.en.md
- PaddleX OCR pipeline docs: https://paddlepaddle.github.io/PaddleX/3.4/en/pipeline_usage/tutorials/ocr_pipelines/OCR.html
- PaddleOCR 3.0 technical report: https://arxiv.org/html/2507.05595v1

## LangGraph / HITL

- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangChain HITL middleware: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
- LangGraph checkpoint SQLite package: https://pypi.org/project/langgraph-checkpoint-sqlite/

## MCP

- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- MCP security best practices: https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices
- FastMCP project: https://github.com/jlowin/fastmcp

## OpenRouter

- OpenRouter quickstart: https://openrouter.ai/docs/quickstart
- OpenRouter structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- OpenRouter tool calling: https://openrouter.ai/docs/guides/features/tool-calling
- OpenRouter API reference: https://openrouter.ai/docs/api/reference/overview
- OpenRouter models: https://openrouter.ai/models

## Playwright patterns

- Playwright actionability / auto-waiting: https://playwright.dev/docs/actionability
- Playwright locators: https://playwright.dev/docs/api/class-locator
- Playwright best practices: https://playwright.dev/docs/best-practices

## Benchmarks / eval patterns

- OSWorld site: https://os-world.github.io/
- OSWorld GitHub: https://github.com/xlang-ai/OSWorld
- OSWorld-Human GitHub: https://github.com/WukLab/osworld-human
- OSWorld-G site: https://osworld-grounding.github.io/
- OSWorld-G GitHub: https://github.com/xlang-ai/OSWorld-G

## Other inspiration

- UI-TARS Desktop / Agent TARS: https://github.com/bytedance/ui-tars-desktop
- Cua: https://github.com/trycua/cua
- Browser Use: https://github.com/browser-use/browser-use
- Vercel Agent Browser: https://github.com/vercel-labs/agent-browser
- Magentic-UI: https://github.com/microsoft/magentic-ui

---

# Final instruction

Build the thing as:

```text
our runtime
  using OmniParser
  using PaddleOCR
  using LangGraph
  using MCP SDK
  using FastAPI
  using OpenRouter
```

Not:

```text
OmniTool with PiKVM bolted on
```

Final invariant:

```text
Third-party libraries produce evidence.
Our daemon makes decisions.
Our daemon executes actions.
Our daemon verifies outcomes.
Atlas remembers durable lessons.
```
