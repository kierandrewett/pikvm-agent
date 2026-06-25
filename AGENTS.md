# PiKVM Agent Implementation Directive

We own the daemon and MCP server.

Use existing libraries aggressively, but **do not let them own the runtime**.

## Core rule

- The PiKVM daemon is our process.
- The PiKVM MCP server is our server.
- Only our daemon talks to PiKVM.
- Only our daemon executes keyboard/mouse.
- Only our daemon declares actions safe or verified.

## Use libraries as bounded adapters

- **OmniParser** — parse screenshots into UI elements, captions, and boxes.
- **PaddleOCR** — OCR text and text boxes for read-back, popup detection, and
  verification evidence.
- **LangGraph** — state graph, conditional routing, checkpointing,
  interrupts/resume.
- **MCP Python SDK** — MCP protocol plumbing only.
- **FastAPI** — local daemon API.
- **OpenRouter** — structured operator decisions only.

## Do not

- Use OmniTool as the main runtime.
- Let OmniParser produce executable actions.
- Let PaddleOCR decide whether typing succeeded.
- Let LangGraph nodes contain PiKVM-specific logic directly.
- Expose raw HID tools as normal MCP tools.
- Build a generic screenshot/click/type MCP server.

## Architecture

Claude Code / Codex talks to:

1. **atlas** MCP server — durable knowledgebase (before and after sessions, never
   inside the fast click/type loop).
2. **pikvm** MCP server — high-level guarded PiKVM sessions.

The daemon runs:

```text
observe_frame → parse_screen → detect_state → operator_decide
  → validate_decision → policy_gate → [human_interrupt]
  → execute_transaction → verify_result → continue / recover / finalise
```

Every operator decision must include: `based_on_frame_id`,
`based_on_world_version`, `intent`, `risk`, `preconditions`, `actions`,
`postconditions`.

## The invariant

```text
No action is valid unless the world still matches the frame it was planned against.
No success is real unless our verifier proves it.
No consequential action happens without explicit approval.

Third-party libraries produce evidence.
Our daemon makes decisions.
Our daemon executes actions.
Our daemon verifies outcomes.
Atlas remembers durable lessons.
```

---

## Build order (track progress here)

Reference: `docs/PLAN.md` → *Build order*. Tick items as they land; commit in
small, single-purpose chunks.

- [x] **Phase 1 — Own the shell**: FastAPI daemon, MCP facade, config loader,
  session store, trace log, PiKVM screenshot capture. *Accept:* `pikvm_start_task`
  creates a session; `pikvm_observe` returns frame_id/world_version/screenshot
  path; no OmniParser/OpenRouter required. ✅ `pytest tests/test_phase1_shell.py`
  + live `pikvm-agent daemon` boot verified.
- [x] **Phase 2 — Library adapters**: TesseractOcrProvider (zero-dep default) +
  PaddleOCRProvider (optional) + PiKVMOcrProvider (live), OmniParser client +
  provider (+ Null default), CompositeScreenParser, set-of-marks overlay,
  provider factory. *Accept:* `pikvm-agent smoke-test --screenshot sample.png`
  reports ocr/omni/merged counts + overlay. ✅ verified (real OCR via tesseract).
- [x] **Phase 3 — LangGraph**: AgentState, GraphDeps injection, StateGraph wiring
  (observe→parse→detect→decide→validate→policy→[interrupt]→execute→verify→
  continue/recover/finalise), async SQLite checkpointing, approval interrupt,
  FakeOperator + OpenRouterOperator. Runtime drives it (continue/approve).
  *Accept:* ✅ happy path runs to finalise; pauses on approval + resumes; state
  survives a simulated restart (`tests/test_phase3_graph.py` +
  `tests/test_phase3_runtime.py`).
- [x] **Phase 4 — Guarded transactions**: GuardedTransactionExecutor (locator +
  actionability clicks, scroll, verified typing, wait_for_mode polling),
  WatchedTyper (self-correcting typing), Recovery (pager/modal/refocus), wired
  into the graph (real execution + recover node). Freshness = re-observe +
  world-version check before execute. *Accept:* ✅ click resolves via ElementMap
  + actionability blocks obscured/ambiguous; raw coordinate click is not an
  action type (debug-only by construction); typed text verified by the verifier;
  scroll never (0,0). `tests/test_transactions.py`, `test_typing.py`,
  `test_recovery.py`, `test_sprintc_hardening.py`.
- [x] **Phase 5 — OpenRouter operator**: OpenRouterOperator — json_schema
  structured output, Pydantic validation, prompt builder, model lanes, schema
  retry, vision content block. *Accept:* ✅ malformed never executes (retry →
  OperatorError); offline-tested via httpx.MockTransport (`tests/test_openrouter.py`);
  risky categories route through the LangGraph approval interrupt.
- [x] **Phase 6 — E1–E10 regression suite**: `bench/test_incidents.py` — all ten
  incidents as named tests against the owned components (verifier, policy,
  executor, typer, recovery, models). ✅ 13/13.
- [x] **Phase 7 — Human console**: daemon-served dependency-free console
  (`pikvm_agent/webui/`) — live frame, event feed, approval queue
  (approve/reject), continue/abort, memory export. *Accept:* ✅ pause/approve/
  abort + persisted approvals via the daemon (`tests/test_phase7_console.py`).
- [x] **Phase 8 — Atlas memory loop**: `build_memory_update` → redacted
  playbook/incident proposal (no screenshots/secrets/typed bodies), wired into
  `pikvm_export_memory_update`. ✅ `tests/test_atlas_export.py`.

## Deviations from the plan (and why)

These are deliberate, minimal adaptations to the host environment. Everything
else follows `docs/PLAN.md` as written.

- **Default OCR is PiKVM's built-in tesseract** (`/api/streamer/snapshot?ocr=1`),
  a zero-dependency `OCRProvider`. PaddleOCR and OmniParser are optional (`[vision]`
  extra) and the pipeline degrades gracefully when their servers are absent — so
  the daemon, graph, policy, verifier, and E1–E10 suite all run without a native
  ML toolchain.
- **Verification logic is ported from the battle-tested TypeScript implementation**
  in `~/dev/pikvm-desktop-agentic` (watched typing, fingerprint thresholds, the
  E1–E10 incidents). See `docs/PIKVM_API.md` for the inherited PiKVM API + tuning
  constants.
- The existing TypeScript MCP server in `~/dev/pikvm-desktop-agentic` is left in
  place (the Electron app depends on it); this Python runtime is the additive,
  transactional successor.
