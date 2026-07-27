# Real-world benchmark evidence

This is the central public scorecard for the PiKVM agent and provider-neutral
harness. It records passing, failing, invalid, and infrastructure-blocked runs.
A row is not a product claim unless its environment, upstream revision, model,
sample size, and evaluator are all shown.

Last updated: 2026-07-27.

## Evidence rules

- Pin the upstream suite and dataset revisions.
- Name the provider route and model string actually sent to the provider.
- Preserve per-case results and failed trajectories.
- Include timeouts and provider errors in the denominator.
- Report median and p95 latency, model calls, wall time, and concurrency.
- Never turn a run on an arbitrary desktop into an official OSWorld or Windows
  Agent Arena score. Their task setup and state evaluators are part of the
  benchmark.
- Separate standard single-pass scores from experimental multi-pass pipelines.
- Treat samples below 100 cases as diagnostic. They show behavior, not a stable
  population estimate.

## What can honestly be claimed today

This is not release-ready yet. The strongest current evidence is that the
system can expose and guard both ordinary Claude/Codex MCP calls and its own
managed reason-act-verify loop without hiding failures. It does **not** yet
support a claim of generally reliable autonomous Windows operation.

| Product claim | Current evidence | Verdict |
| --- | --- | --- |
| Direct Claude/Codex/OpenCode calls are visible and operator-controllable | Actual MCP `ClientSession` dispatch test, exact redacted arguments plus durable outcome/latency, path/raw-payload exclusion, fail-closed missing-visibility tests, scoped observer credential, required frame/control/idempotency fields on every HID tool, real browser, 100-call audit at 6.70ms median / 7.54ms p95 | Passing local contract |
| A coding client can submit once while the dedicated harness owns progression | The referenced Claude session used 551 direct PiKVM calls; 21 API contracts prove the replacement managed loop crosses action slices, safe replans and verifier-more-work checkpoints, resumes after exact human approval, recovers internal yields after restart, reconstructs its automatic-resume ceiling from durable history, and refuses overlapping Continue bypass. Operator steering is durable, forces a fresh managed plan, and cannot discard unsettled HID or take control from direct/external drivers. Exact generated configs initialize over real stdio for all four clients and preserve their validated client identity | Passing control-loop and generated-stdio inventory contracts; authenticated task/restart pending |
| Effective client config cannot silently retain a second PiKVM tool surface | Fail-closed audit supports native Codex resolved inventory, Codex TOML/shared project JSON, Claude/Gemini JSON, and legacy/V2 OpenCode JSON/JSONC. Native read-only Codex inventory found one raw PiKVM registration and no managed registration. A session-only Codex override produced exactly one managed PiKVM surface while retaining unrelated MCPs; Claude's installed strict-MCP flags produced one explicit managed surface. OpenCode 1.14.44 resolved one managed surface under `--pure`, exact default-deny permissions, ephemeral writable state, and client-owned OAuth linked without copying it. Gemini 0.35.3 loaded the temporary system catalog natively and resolved one allowlisted managed surface from an empty dedicated profile; the exact admin-policy path/content and extension-disable argv are configured but not enforcement-tested. These paths modify no persisted registration and passed 54 launcher/audit/package contracts | Codex/Claude/OpenCode native isolation dry-runs pass; Gemini settings audit passes; no authenticated coding-client task or Gemini policy-enforcement call was run |
| Managed and direct assurance levels are unambiguous | Real browser showed persistent managed/direct control contracts; 390×844 check had no horizontal overflow | Passing UI contract |
| Computer-use actions remain inspectable while a run is changing | A 1440×980 real Chromium fixture retained an expanded 12-action group across 500ms updates; each action showed semantic summary, coordinates or exact typed-text preview, status, duration, world/control evidence and expandable exact MCP arguments. A separate dangerous Enter fixture rendered the exact two-input sequence, 47-character count, external-side-effect reason, and Allow once/Deny controls inline | Passing browser/UI contract |
| The operator can inspect the verifier's visual evidence | Authenticated API contracts return the labelled before/after bytes without exposing their local path; static UI contracts cover revision refresh, loading/error state, and blob revocation. A live disposable-Windows diagnostic exercised failed and uncertain visual verification without turning either into success | Passing local/browser contract; live task itself did not pass |
| Long prose/code arrives exactly | Windows transport trials were exact 581/581 and 142/142 characters, but the seven-trial physical loop still failed overall | Partial; not a release claim |
| Raw HID avoids encoded/script transfer hacks | A seeded 1,000-payload corpus caught 800/800 unsafe shapes with 0/200 safe false positives; the public MCP integration also refuses encoded transfer before daemon contact | Passing local syntax gate; explicit byte-verified transfer channel pending |
| Exact-byte virtual-media preparation works | 10/10 builder contracts plus 19/19 transaction/UI/adapter/surface contracts cover mode-0600 media, exact browser approval, rollback, cleanup uncertainty, identity, lease, stop, model-surface exclusion, and explicit unsupported VNC | Passing target-free contract; daemon bridge capability and live target result pending |
| OCR can safely verify arbitrary desktop text | Tesseract is 56.9% selected and 61.4% expected-aware exact; its 800-case routine tier is 71.125% exact while the preserved 200-case confusable stress tier is 0%; PaddleOCR is 78.9% normalized exact; the retrospective known-intent candidate union is 82.7% overall, 97.0% routine, and 25.5% stress on the same 1,000 cases; no confidence threshold supports a 99% lower-bound claim | Failing release gate |
| Model grounding is reliable | Current seeded ScreenSpot-Pro sample is 73/100 | Diagnostic only |
| End-to-end desktop tasks are reliable | Current OSWorld task set is 6/9; full scored-attempt denominator is 6/33 with 11 additional unscored failures | Failing release gate |
| A model can autonomously complete routine Office work | Portable Word/Excel contracts and semantic OOXML verification pass local tests. Two live disposable-Windows Word attempts are now retained: r12 failed after 32.4 minutes with 7/12 actions complete and four typing verification failures; r13 was intentionally aborted after 24.3 minutes when a wrapped-prose crop caused an OCR false negative. Neither saved an artifact; both produced zero bytes | Failing live acceptance gate |
| Windows Agent Arena is supported | 154 tasks discovered; official golden image is absent | Not run |
| Provider choice is portable | Codex and Claude OAuth CLIs live-tested; dedicated-profile Gemini CLI OAuth, native OpenAI Responses, Azure OpenAI API-key/Entra modes, OpenAI-compatible, Anthropic, Gemini AI Studio, and Vertex AI adapters protocol-tested with mocks/source contracts; one target-free command now compares identical blind pixels/schema across configured routes. Its first two-provider attempt retained both failures; the Codex local-state defect is fixed, while the approved rerun was denied before launch | Partial; no successful frozen live all-provider matrix yet |

Re-run the target-free HID payload corpus with:

```bash
uv run python bench/payload_shape_adversarial.py \
  --count 1000 \
  --seed 4204202 \
  --out /tmp/hid-payload-shape-report.json
```

The output path is created mode `0600` and never overwritten. The corpus keeps
unsafe refusals and ordinary safe controls in the same denominator.

## Latest disposable-Windows diagnostic

On 2026-07-27 the managed harness was given one bounded task: use Windows
Search to open Notepad without pressing Enter or touching a destructive,
communication, account, or purchase control. The runtime endpoint was supplied
only to the lab command and is absent from this repository. The production
PiKVM target was not contacted.

This run is retained as **paused/uncertain**, not a pass:

| Signal | Observed |
| --- | ---: |
| Wall time | 558.817 s |
| Claude CLI / Opus model calls | 12 |
| Summed model-active time | 483.697 s |
| Mean model-call latency | 40.308 s |
| Slowest model call | 85.367 s |
| HID bursts attempted / completed | 3 / 2 |
| Stale-world bursts refused before HID | 1 |
| Verifier outcomes | 0 verified / 1 failed / 2 uncertain |
| Enter presses / dangerous actions | 0 / 0 |

The run proved that same-row OCR grounding let the first click pass, a later
stale-world click was refused before HID and automatically replanned, and the
verifier would not claim success from an unexplained screen change. It also
exposed a real lab defect: another disposable adapter was connected to the same
VNC endpoint, while the mock adapter reported no other client. Two populated
Notepad windows therefore appeared without an attributable action in this
trajectory. Cross-adapter target leasing is a release blocker for the VNC lab,
and this task cannot support an autonomy claim. A post-run fix now acquires a
process-independent, canonical-target lease before RFB and passes local
integration tests; that fix has not yet been rerun against the disposable VM,
so this historical result and its verdict remain unchanged.

The full failure-inclusive record is
[`notepad-managed-diagnostic.json`](results/2026-07-27/live-vnc/notepad-managed-diagnostic.json).
It intentionally omits the VNC address, credentials, screenshots, prompts, and
raw model responses. This is not an OSWorld, Windows Agent Arena, Office, or
general desktop-reliability score.

## Live Word acceptance failures

Two Claude Opus reason-act-verify attempts ran against the authorized
disposable Windows VM on 2026-07-27. The endpoint was supplied only as a
runtime argument. The production PiKVM target was not contacted.

Neither attempt passed. r12 wrote an unsaved draft but treated four visibly
landed prose bursts as unverified, then failed before Save As. r13 resumed the
visible draft, reached 135 words, and proved that guarded fast transport
reduced ordinary action latency to a 3.44-second median. It then paused
fail-closed because the inferred OCR crop covered only the first lines of a
wrapped paragraph. Full-screen Tesseract read the complete new paragraph
exactly, with 0.92–0.96 confidence on its three lines. The run was stopped
between actions so a fresh process could load the fix.

| Signal | r12 | r13 |
| --- | ---: | ---: |
| Final state | failed | aborted deliberately |
| Wall time | 1,946.848 s | 1,459.761 s |
| Model-active time | 1,193.748 s | 668.543 s |
| Controller median | 36.422 s | 30.754 s |
| Reasoner median | 59.747 s | 66.076 s |
| Verifier median | 50.605 s | 60.310 s |
| Actions completed / checkpointed | 7 / 12 | 4 / 7 |
| Action median | 5.179 s | 3.435 s |
| Provider schema repairs | 0 | 1 |
| Saved DOCX bytes | 0 | 0 |

The failures produced four concrete fixes: long model slices continue in the
background, browser approvals acknowledge before model work, editor prose can
use the guarded fast transport without weakening command/code verification,
and wrapped editor prose receives a read-only full-screen OCR fallback that
accepts only a complete punctuation-preserving occurrence. The last fix is
covered by 188 focused typing/verification/direct-burst tests and had not yet
been rerun live when these two records were published. These are failure
records, not an Office capability claim.

Public records:
[`office-word-r12.json`](results/2026-07-27/live-vnc/office-word-r12.json) and
[`office-word-r13.json`](results/2026-07-27/live-vnc/office-word-r13.json).

## Visibility during a run

The authenticated harness console shows the live frame, current plan or direct
MCP caller, exact redacted tool arguments, selected provider/model, provider
attempts and fallback, verifier or daemon evidence, approval requests, and
pause/stop controls together. A compact live-efficiency strip shows run wall
time, summed model-active time and completed calls, progress-bearing actions,
automatic continuations/stops, recoveries, faults, and durable
provider-attempt/cost budget. Direct MCP runs
show external model accounting rather than an invented number. While work is
active, a persistent status chip
names the unmatched provider call or MCP action and updates its elapsed time
once per second. Direct-client provider/model strings are explicitly labelled
as launcher-declared; the UI does not pretend it independently identified
them. Managed actions and opt-in guarded direct calls are committed to SQLite
before HID execution. Labelled before/after images and quiet adapter/daemon logs
are retained with managed runs. The latest labelled comparison is also fetched
through an authenticated, no-store, path-free endpoint and shown inside the
Verification transaction block. The JSON files linked below are derived from
those durable records; the UI is not a separate, less-auditable execution path.

Provider rows distinguish saved CLI login, API-key environment, bearer-token
environment, and CLI bearer-token ownership. Command-backed routes name only
the executable (`az` or `gcloud`), never the credential, token environment, or
command output. They separately show the configured model alias, last model
reported by a successful call, and the latest blind provider-conformance
exact/schema/median-latency result.

The chat workspace now exposes this as a contextual Models sheet. Its
authenticated catalog comes from the same canonical ten-adapter backend
contract, while the configured-account view shows the reasoning, acting, and
checking routes, primary/fallback position, readiness, authentication owner,
coarse success/latency, and conformance state. It never renders raw
readiness/provider errors or credential source paths and contains no browser
secret-entry form. The composer now shows the effective model for all three
roles before send, and an active run exposes its durable route as locked rather
than silently adopting later settings changes. Thirty-six frontend and 161
provider/agent/API/store/fixture/static-UI contracts pass at the exact published
commit; no provider or computer was contacted. See
[`model-connections-and-routing.json`](results/2026-07-27/ui/model-connections-and-routing.json).

### Provider conformance boundary

`harness provider-conformance` renders seeded 960×540 screens and submits the
same blind transcription schema to each explicitly selected configured
provider. It has no daemon, VNC, PiKVM, HID, or computer client. The command
requires `--allow-provider-calls`, includes schema failures, timeouts, rate
limits, and unavailable routes rather than dropping them, bounds concurrency,
and writes a new mode-0600 report without overwrite.

The retained report includes synthetic expected/observed fields, normalized
token usage, returned model strings, exact and normalized accuracy, median/p95
latency, and coarse failure counts. The authenticated provider-health endpoint
reads only aggregates; raw provider output, exception bodies, prompts, image
paths, and credentials cannot enter the UI shape. Seven focused contract tests
currently pass, including failure-denominator and invalid-report privacy
checks.

The first live two-provider attempt is retained rather than discarded:
Codex failed before its model call in 145ms because the CLI's SQLite state
landed in a read-only location; Claude reached the 90.113-second runner
timeout. Codex now receives a writable ephemeral `sqlite_home` in a sibling
directory outside the empty model workspace while authentication remains
CLI-owned. Three focused regressions and the 76-case
provider/config/routing selection pass. A post-fix target-free probe made both
adapters reach the restricted 15-second outbound boundary. The requested
approved-access rerun was denied before process launch, so there is still no
current accuracy score or routing recommendation. The failure-inclusive
diagnostic is in
[`provider-conformance-attempt-2026-07-26.json`](results/2026-07-26/providers/provider-conformance-attempt-2026-07-26.json).

The target-free managed smoke lab now exercises the production coordinator and
operator app with explicitly labelled deterministic machine/model adapters.
Its ASGI contract retained the originating `codex-cli` identity, three
successful reasoner/controller/verifier calls, exact action checkpoint,
checkpoint PNG, labelled verification image, and terminal completion. That
interface test used the in-memory store; a separate CLI construction check
selected SQLite but did not execute a SQLite-backed request.

The companion `client-task` path audits Codex/Claude/Gemini/OpenCode isolation, reads
the task over stdin, uses no permission-bypass flags, and retains only task
digest, length, timing, and exit status. Installed OpenCode 1.14.44 passed its
native `--pure` resolved-config dry-run with exact default-deny permissions,
ephemeral writable state, client-owned OAuth linked without copying it, and no
ambient secret forwarding. Gemini requires a separate profile, clean
workspace, native merged-settings audit, system MCP allowlist, CLI MCP
allowlist, `--extensions none`, and a default-deny admin policy. Its empty-profile
settings audit passed, but OAuth, policy enforcement, and task execution remain
unproven. The isolated launcher/audit/wheel slice passes 54/54 contracts. The
recorded smoke-lab slice passes 24/24 contracts and its
focused selection passes 44/44 gates,
including public-claim and scorecard-drift checks. A repository-wide run was
attempted but could not complete this runner's temporary SQLite worker and
Starlette `TestClient` portal-thread boundaries; it reported no failure before
either boundary, but no aggregate repository pass count is claimed. A
loopback-only launch was attempted, but the execution broker rejected it
before process creation, so no outer coding client, MCP process, external
provider, or computer target ran. Evidence:
[`managed-smoke-lab-contract.json`](results/2026-07-26/harness/managed-smoke-lab-contract.json).

The exact disposable-VM authorization was also received, but this host denied
the isolated adapter before process creation. Therefore no local listener, VM
contact, observer installation, or Office task occurred in this checkpoint.
The ready observer is a 1,081,344-byte PE32+ x86-64 Windows GUI binary with SHA-256
`b6a19566f3d4530fc36930241c1c7793ff9f25de39ad96e7823ff3523f4e27f4`;
33 completed local Office/bootstrap contracts passed. This is readiness evidence
only, not a live Office result.

The latest 390×844 Chromium layout probe used an intentionally long activity
label. The viewport, document, body, and command bar were all 390 CSS pixels
wide; the activity label was constrained to 235.67 pixels and ellipsized, and
document/body scroll width stayed 390, so the new surface introduced no
horizontal overflow. This was a static local rendering check, not a live model
or HID result. See
[`activity-status-responsive-audit.json`](results/2026-07-25/ui/activity-status-responsive-audit.json).

A subsequent 200% reflow audit could not be run live because this execution
sandbox forbids creating even a loopback listener, while the in-app browser
correctly blocks local `file:`/`data:` fixtures. Static inspection still found
and fixed four concrete defects: bare-text header labels overflowed their
34-pixel compact buttons, the narrowest breakpoint removed provider status,
approval reasons were ellipsized, and dialogs had no viewport-height scroll
boundary. Two static regressions pass. This is a fix record, not a completed
200% browser claim.

The current production assistant-ui/shadcn workspace assets are **1,206,165
bytes total** including local fonts: 713 bytes HTML, 1,022,916 bytes
JavaScript, 106,116 bytes CSS, and 76,420 bytes of local fonts. Gzip output is
309,038 bytes for JavaScript and 17,693 bytes for CSS. Release regressions cap
every asset at 1.1 MB, the total at 1.25 MB, and gzip JavaScript/CSS at 320/24
KiB. This is materially larger than the retired hand-built console and remains
within the explicit current envelope; the old 128 KiB claim no longer
describes the shipped chat workspace.

The chat now consumes the authenticated run SSE directly instead of waiting on
an unconditional 750 ms full refresh. A target-free runtime trace delivered
the exact action attempt, completion, and independent verification in order.
The selected run coalesces snapshots for 75 ms and performs a 15-second
reconciliation while live; if streaming fails, the header visibly changes
from Live to Reconnecting or Updates offline while 1.5-second bounded polling
and 0.5–5-second reconnect backoff preserve progress. Browser authentication
is no longer labelled as MCP connectivity, and the managed MCP route is
visible beside the model selector at send time. Nineteen frontend and 53
harness/API/fixture contracts pass. The post-change browser visual/reflow audit
remains pending because the in-app browser blocked local navigation from its
error page; no alternate browser was used. See
[`computer-action-live-stream.json`](results/2026-07-27/ui/computer-action-live-stream.json).

Computer actions now expand into a four-phase transaction receipt: source
screen, bounded input, delivery, and independent screen check. Typed payloads
show their exact body plus character and line counts; key chords use keycaps;
pointer button and coordinates use separate tokens. A held approval says the
input has not been sent, and a linked verifier event remains the only route to
green Verified state. Twenty-two frontend tests, including eight focused
receipt tests, 58 harness/API/fixture/provider tests, the production build, and
the TypeScript similarity scan pass. This was target-free component evidence,
not a browser visual or live-machine pass. See
[`computer-action-receipt.json`](results/2026-07-27/ui/computer-action-receipt.json).

The tailored computer-use pass makes the receipt action-specific instead of
generic. Pointer input includes a normalized target map against the current
screen dimensions; typing retains the exact payload and counts; keys use
individual keycaps; and scrolling names its direction and step count. The
expanded receipt carries the intended effect, expected visual evidence,
source/input/observed transition, attempt, transport latency, and idempotency
key. Running, held, committed, verified, unverified, safely refused, failed,
and cancelled states all have text and icon treatment, not colour alone. Raw
transport errors stay in diagnostics. This exact commit passed 36/36 frontend
tests, including 12 focused action-presentation tests, and 161/161
provider/agent/API/store/fixture/static-UI tests in a detached worktree. The
1,217,335-byte bundle remains below the enforced 1.25 MB cap. See
[`computer-use-chat-controls.json`](results/2026-07-27/ui/computer-use-chat-controls.json).
The subsequent branch-wide target-free run found one stale playbook test that
omitted the now-required caller-stable idempotency key; the test was repaired
without weakening the runtime gate. The follow-up is clean: 1,052 passed, one
environment-dependent case skipped, 36/36 frontend tests passed, and the
production scorecard/build/similarity checks remain current.

The next receipt pass fixed a production/fixture contract mismatch rather than
adding presentation around synthetic data. Real verifier results arrive as
`model.completed` with role `verifier`; the chat had only linked the fixture's
older `verification.completed` shape. Each action now binds the actual
controller and verifier provider/model/latency, and each generated before/after
composite receives a durable revision, source/destination frame IDs, and an
authenticated revision-specific endpoint. A historical receipt therefore
cannot silently show a newer action's “latest” image, and host paths remain
private. The target-free UI fixture exercises the same event shape and image
fetch without connecting to a machine or model provider.

All 38 frontend tests, 113 focused Python contracts, the production build,
resource gate, and TypeScript similarity scan pass. A broader run excluding a
concurrently modified, independently CPU-bound Office acceptance file completed
1,031 passes and one environment-dependent skip in 79.79 seconds. The
repository-wide attempt had reached 700 passes, one skip, and zero failures
before only that validation process was interrupted after 692.72 seconds; this
is not presented as a full-suite pass. The current bundle is 1,222,540 raw
bytes, with 311,739-byte gzip JavaScript and 18,134-byte gzip CSS. The in-app
browser URL policy still blocks the post-change visual/reflow audit. See
[`action-bound-screen-evidence.json`](results/2026-07-27/ui/action-bound-screen-evidence.json).

Computer-use typing now carries its own bounded input receipt from watched
delivery into the assistant-ui tool disclosure. The action transcript keeps
the exact requested payload beside the screen read-back, focus outcome, edit
distance, corrections, delivery retries, and guarded fast-transport state.
Matches, mismatches, focus loss, delivery-only results, uncertain OCR, and
redacted secret input have distinct visible states without splitting one
computer action into a wall of cards. Receipts are tied to the original action
index, bounded at the public event boundary, and stripped of unknown fields.
Secret-marked input is forced to an unverified-delivery receipt and neither the
payload nor observed text is retained.

At integrated commit `f3f6635`, all 42 frontend tests and 153 focused
typing/harness Python tests pass. A detached broader run completed 1,037 passes
and one skip in 81.97 seconds; one source-location assertion was explicitly
deselected because the shipped `.mcp.json` intentionally names the real
repository rather than the temporary worktree. The production build and
resource gates pass at 1,224,925 raw bytes, with 313,393-byte gzip JavaScript
and 17,936-byte gzip CSS. The target-free fixture shows a production-shaped
typed payload and matching read-back without contacting a machine or provider.
The in-app browser URL policy still blocks the post-change visual/reflow audit;
no alternate browser was used. See
[`action-bound-typing-readback.json`](results/2026-07-27/ui/action-bound-typing-readback.json).

The previous customer wheel was built offline with Hatchling 1.31.0 from the
pre-existing cache. Its SHA-256 is
`7252debceaad7ff3ec499712e6439e1c590d27ca0effd2cec029d2524bbde96f`;
the then-current inspector verified 132 members and 131 `RECORD` entries,
including the client-isolation audit module, and all eight then-current
wheel-acceptance tests passed. The wheel package installed into a fresh venv,
and the generated console loaded `pikvm_agent.cli` from that installed wheel
when given the existing dependency layer. A dependency-complete offline
install remains blocked because several locked compiled wheels are absent from
the local cache; no network download was attempted. See
[`wheel-build-and-isolated-install.json`](results/2026-07-26/package/wheel-build-and-isolated-install.json).
That artifact predates the isolated task launcher and target-free smoke lab,
so the current inspector now rejects it as stale with both modules missing. A
fresh offline build was attempted, but the restricted runner could not create
the `uv` cache temporary file and the execution broker rejected the narrowly
scoped elevated build before process creation. No current wheel pass is
claimed.

The authenticated Guide control durably records operator guidance, cancels an
in-flight provider wait, and can resume only through a fresh harness-owned
reasoner plan. The agent credential, direct MCP runs, externally driven
benchmarks, and runs with unsettled HID cannot use it. Ten target-free
contracts pass. A real in-app Chromium fixture also exercised the Guide dialog,
exact POST payload, stale-plan removal, durable guidance, timeline event, and
ownership toast. At 390×844, document client and scroll widths were both 375
pixels and Guide remained visible. The fixture contacted no computer or
provider. See
[`operator-steering-2026-07-26.json`](results/2026-07-25/ui/operator-steering-2026-07-26.json).

### Live-frame resource envelope

The console's read-only daemon preview now uses a streamed, failure-bounded
adapter. It refuses empty or non-image bodies, invalid declared dimensions,
declared oversize, and streamed bodies that cross 4 MiB. Concurrent requests
for the same session share one upstream fetch. A true LRU retains at most eight
session frames and their locks, so cached image payload is bounded at 32 MiB;
the lock pool itself is fixed at eight stripes. The default 450 ms interval
also bounds upstream capture pressure.

Six target-free contracts pass and are tied to
[`live-frame-resource-envelope-2026-07-26.json`](results/2026-07-25/ui/live-frame-resource-envelope-2026-07-26.json).
This is a Python transport/cache envelope. It deliberately excludes browser
decode time, browser resident memory, daemon capture time, VNC/PiKVM, and
network latency.

### Artifact-backed Office acceptance

The checked [`office-acceptance-v1.yaml`](office-acceptance-v1.yaml) adds two
portable tasks:

- write a 650–900 word Shakespeare essay in Word, save/reopen it, and satisfy
  title style, paragraph, word-count, and required-phrase checks;
- create a fictional quarterly-earnings workbook in Excel, save/reopen it, and
  satisfy exact worksheet, cell, numeric, and formula checks.

The task instruction contains one `{artifact_path}` placeholder, not a VNC
endpoint or operating-system path. On Windows, the disposable helper accepts
the runtime file path through `--file`; after the managed run completes, a
guarded direct-MCP transaction publishes the exact file bytes through the
screenshot matrix. The host rejects unsafe or malformed OOXML and writes a
result only after combining run status, artifact SHA-256/checks, model lanes,
action efficiency, provider attempts, and configured cost accounting.
Captured Office files are mode `0600`. Each attempt uses a fresh guest
filename inside the lab workspace, and the observer-returned path must match
exactly before OOXML scoring; an artifact from an earlier run cannot satisfy
the current attempt.

Current evidence is deliberately limited to the acceptance contract: twenty-two
Office tests plus eleven helper/bootstrap tests pass, and the Windows observer
cross-build succeeds. `office-case --skip-provision` now restarts an installed
helper with the attempt's fresh artifact path instead of requiring a pre-running
process to know that random path. No live task result exists. Even after exact
authorization, the execution broker denied the isolated adapter before process
creation, so no listener, connection, helper action, Office mutation, or model
call occurred.
This section will link the first `result.json` only after a real artifact passes.

### Bounded history and reconnect audit

A deterministic real-Chromium fault injection started with a 1,200-event run,
delivered event 1,201, and then closed the first authenticated SSE response.
The browser opened exactly one replacement stream, resumed from its cursor,
and returned to `Events live`. The console displayed
`1201 events · latest 3`: the durable denominator stayed visible while only
the bounded tail entered the DOM.

The run-list API now contains no event array. Run detail serializes at most the
latest 500 events without first materializing the complete event history, and
the catch-up endpoint paginates at a caller-selected limit capped at 1,000.
The server emits a five-second heartbeat; the browser reports API and stream
health separately and reconnects with bounded exponential backoff. A
1,200-event contract test keeps the run-list response below 1 KiB and checks
the 200- then 1,000-event durable-history pages.

At 390×844 the reconnect probe had a 376-pixel document and 368-pixel body,
with no horizontal overflow. It visibly reported `420ms · 1` model
time/calls, `2 / 2` progress actions, and `1 recover · 1 fault`. This is a
deterministic browser/API resilience result, not evidence of model or remote
machine accuracy.

That probe exposed a deeper storage defect: SQLite still rewrote and reloaded
the complete event array inside one run JSON document. The store now keeps the
atomic non-event run checkpoint and lightweight summary separately from
append-only event rows. Existing single-blob databases migrate in place.
Run-list, detail-tail, frame, catch-up, and stream polling paths use the new
event-free state, summary, or bounded-page contracts; only explicit full
performance/report work loads complete history.

A fresh 100,000-event normalized in-memory contract benchmark now includes the
managed control path. One-event checkpoint latency was 0.086ms median /
0.138ms p95; event-free inventory was 0.014/0.024ms; the latest-500 page was
0.980/1.285ms; and the bounded control snapshot was 3.372/4.539ms while loading
exactly 1,000 events on all 100 repetitions. The old full checkpoint was
9,588,030 bytes; normalized state plus summary was 758 bytes plus a 97-byte
event append, an 11,214.070× write-size reduction.

The first implementation of the bounded control window regressed append median
latency to 19.021ms by scanning all 100,000 in-memory events. The benchmark
caught it before acceptance; deriving the suffix index from contiguous durable
sequence numbers reduced the final median to 0.086ms. The managed harness,
guarded direct coordinator, operator approval waiter, and mutation routes now
load at most the latest 1,000 events while preserving the global append cursor.
Explicit performance/report endpoints may still request the complete history.

This still isolates serialization and the storage contract: it excludes
aiosqlite, filesystem, model, OCR, HID, and network latency. The restricted
runner blocks aiosqlite's thread worker and denied the requested local
escalation, so production SQL statements and legacy migration remain exercised
through the synchronous test adapter. A real-filesystem SQLite soak is not
claimed.

Machine-readable evidence:
[`stream-efficiency-reconnect-audit.json`](results/2026-07-25/ui/stream-efficiency-reconnect-audit.json),
[`normalized-storage-n100000.json`](results/2026-07-25/ui/normalized-storage-n100000.json),
[`normalized-storage-bounded-control-n100000-2026-07-26.json`](results/2026-07-25/ui/normalized-storage-bounded-control-n100000-2026-07-26.json).
No VNC, PiKVM, or production computer was contacted.

The browser operator token is never forwarded by the generated direct-client
configuration. A separate high-level agent token is limited to non-approval
run operations, and a separate observer token is limited to direct-call
preflight/completion ingest plus host artifact acceptance. These are local role
boundaries; a hosted service still needs tenant identity, short-lived
credentials, and audited token issuance.

### Live console audit

On 2026-07-25 the console was exercised in a real browser against the isolated
fake-machine daemon. It visibly exposed the `codex-ui-audit` /
`gpt-5.6-sol` route, reasoner plan, 1,280×720 live frame, world and control
versions, exact bounded pointer action, idempotency key, provider attempts,
before-HID checkpoint, verifier evidence, and pause/stop controls.

The audit found two real defects rather than hiding them:

1. Stop correctly aborted a run while Continue was in flight, but the cancelled
   Continue request appeared as HTTP 500.
2. The launcher required a 32-character access token while the API and browser
   accepted 16.

Both are fixed and regression-tested. The post-fix live race returned HTTP 200
for Continue and Stop, retained the durable `aborted` state, and emitted no
traceback. The full suite passed 450 tests with one opt-in benchmark skipped.
See the machine-readable
[`live-operator-audit.json`](results/2026-07-25/ui/live-operator-audit.json).
This verifies the tested web stop path; it does not claim an emergency stop
independent of the operator web process.

### Live benchmark visibility

`osworld-case --operator-console` now embeds the authenticated operator UI in
the live benchmark process. The UI reads the same SQLite run and the same
PiKVM-shaped daemon session used by the scored attempt; approvals made there
resolve the exact durable approval checkpoint before the existing runner
continues. It is not a replay or a second control path.

The runner prints the loopback UI URL and token environment-variable name, and
writes `operator-console.json` beside the report. That descriptor contains no
token, VNC/PiKVM location, daemon URL, VM endpoint, or provider credential.
The bearer token remains an environment-owned value pasted into one browser
tab. The benchmark wall-time budget continues while waiting for a human, so an
approval delay remains visible in efficiency results.

Embedded benchmark mode hides task creation, pause, and continue so the browser
cannot become a competing model driver. Exact approve/reject/take-over and Stop
remain available. UI decisions, runner progression, timeout aborts, and Stop
share one per-run execution lock.

The embedded approval/waiting, external-driver isolation, CLI surface,
token-free descriptor, IPv6 URL, API, live-frame, harness, public-report, and
historical-audit integration checks passed 107 tests locally. One additional
real-loopback lifecycle check was skipped because this execution sandbox
prohibits creating any socket; it remains enabled in ordinary CI. No new scored
OSWorld result is claimed from these integration tests.

### Independent emergency-stop audit

The raw MCP server and out-of-band CLI now refuse to guess a daemon. With
`PIKVM_AGENT_DAEMON` unset, raw MCP startup and `panic-stop` exit before a
server or network request exists. Invalid transports and URLs with embedded
credentials, query parameters, or fragments also fail closed.

With the operator web process absent, one live isolated trial halted one active
fake-machine session through the explicitly selected daemon. The daemon
confirmed HID quiescence and zero in-flight actions; the CLI named the safe
machine alias/fingerprint and returned in 0.34 seconds. The session then
reported sticky `failed` / `panic_stop` state at control epoch 1. A
non-quiescent response is a CLI failure and never prints confirmation.

This proves the model- and web-independent daemon brake, not operation after
the selected daemon itself becomes unreachable. The machine-readable evidence
is [`emergency-stop-audit.json`](results/2026-07-25/safety/emergency-stop-audit.json).
The post-change regression suite passed 513 tests with one opt-in benchmark
skipped in 41.61 seconds. No production PiKVM or external VNC target was
contacted.

### Live provider fallback and approval audit

An intentionally failing primary provider was placed before the real Codex CLI
OAuth route for reasoner, controller, and verifier. The primary failed three
times; `codex-fallback` / `gpt-5.6-sol` succeeded three times at 6.146s,
4.840s, and 9.652s respectively. Every role, provider, route index, sanitized
failure, selected model, and latency was written to the event stream. The
fallback path passed. The fake-machine task itself paused as uncertain because
its synthetic frame does not render a cursor, so it is not counted as a task
success.

A separate deterministic provider proposed a side-effecting communication
command. The daemon classified it as `communication_send` and both harness and
daemon reported `needs_approval`. A decision without the exact approval-intent
header was refused with HTTP 409. The exact human rejection produced no
`action.completed` event, incremented the control epoch, and left the computer
session latched `failed`.

That trial found two lifecycle defects: direct approval state was not persisted
for daemon polling, and a terminal rejected harness run left its underlying
computer session live. Both are fixed and regression-tested. The full evidence
is in
[`live-routing-and-approval-audit.json`](results/2026-07-25/ui/live-routing-and-approval-audit.json).

### Direct Claude/Codex MCP visibility audit

The ordinary `pikvm` MCP really does bypass the autonomous operator graph when
Claude Code or Codex calls `pikvm_open`, `pikvm_run_burst`, and related tools
itself. The direct-call bridge now wraps that actual MCP `call_tool` boundary.
In guarded mode it records and authorizes the exact call before the tool body
can execute. The UI labels the run `Direct MCP` and does not invent a harness
reasoner, controller, or verifier.

Ten integration tests and a real-browser audit passed:

| Contract | Result |
| --- | --- |
| Exact non-secret arguments and caller/model visible before tool body | Pass |
| Completed/refused outcome and measured tool latency remain visible afterward | Pass |
| Internal frame paths and unbounded raw result payloads never enter run JSON | Pass |
| Secret-marked typing absent from run and event APIs | Pass |
| Pause blocks future HID but leaves screenshot/OCR inspection available | Pass |
| Direct model cannot approve its own held action | Pass |
| ID-bound authenticated UI approval/rejection | Pass |
| Stop aborts the daemon session and latches the direct-call gate | Pass |
| Guarded mode fails closed if preflight is unavailable | Pass |
| Missing visibility configuration refuses startup and protocol dispatch | Pass |
| Observe-only mode preserves perception/emergency stop and blocks action tools | Pass |
| Completed tool result does not incorrectly close the direct session | Pass |
| Completion telemetry survives a coordinator restart | Pass |

A checkpoint safety review added four fail-closed contracts. Outlook-style
`Alt+S` enters the same human approval hold as `Ctrl+Enter`; stale-world
refusals discard the old action and require a fresh controller decision;
every model-facing burst/playbook/key/type/click/scroll tool requires the exact
world version, control epoch, and caller-stable idempotency key; and
target-local pointer freshness requires the explicit `isolated_benchmark`
profile, its opt-in flag, and the separate lab-app construction capability.
One non-overlapping target-free selection passed 206/206 permission, harness,
MCP schema/preflight, visibility, lab-construction, payload, history, editor,
and burst tests in 4.61 seconds. The two full `Runtime` integration cases for
missing freshness and a blank key remain uncounted because this restricted
runner stalls that fixture; the model-facing schemas, daemon request model, and
pure capability boundary are covered, but the runtime cases must run in the
clean-install suite.

The browser rendered the live frame, exact burst, `claude-code` /
`anthropic-oauth` / `opus-4.8` identity, pause/resume state, and a synthetic
Teams-send approval. Rejecting it retained the audit trail. At a requested
390×844 viewport, client and scroll width were both 375 CSS pixels; the live
screen, transaction, and timeline were each 360 pixels wide, so there was no
horizontal overflow. The page emitted zero console errors.

A 100-call local SQLite + in-process ASGI microbenchmark measured 6.696 ms
median and 7.538 ms p95 for guarded visibility, versus 0.014/0.018 ms for the
deterministic FastMCP no-op baseline: 6.682 ms median incremental overhead and
143.8 guarded calls/s. This isolates audit persistence and serialization. It
excludes socket, model, frame, OCR, HID, and remote-machine latency and is not a
service-level objective.

Machine-readable evidence:
[`direct-mcp-visibility-audit.json`](results/2026-07-25/ui/direct-mcp-visibility-audit.json).
No PiKVM or VNC target was contacted for this UI/bridge audit. At that audit
checkpoint, the regression suite passed 499 tests with one opt-in benchmark
skipped in 41.85 seconds.

After that browser audit, a protocol-level regression test drove a registered
tool through an in-memory MCP `ClientSession`, not a direct Python method call,
and confirmed the durable `run.created → action.attempted → action.completed`
order. The permission regression then proved all of the following:

| Model-facing authority contract | Result |
| --- | --- |
| Direct observer token can call only `/api/direct/*` ingest routes | Pass |
| High-level agent token can use non-approval run routes only | Pass |
| Browser operator token is distinct from both model-side tokens | Pass |
| Raw model-facing MCP lists no approval tools | Pass |
| High-level model-facing MCP lists no approval tool | Pass |
| Harness-owned private MCP child alone registers the destructive approval relay | Pass |
| Secret-marked input is absent from durable SQLite model/event shapes | Pass |
| Tool-controlled failure prose is reduced to a safe error class before persistence | Pass |
| Codex, Claude, Gemini, and OpenCode snippets contain env names but no token or daemon values | Pass |
| Generated client setup defaults to managed reason-act-verify control | Pass |
| Managed launcher requires only the scoped agent token and survives harness startup order | Pass |
| Managed run preserves a validated Codex/Claude/Gemini/OpenCode source label | Pass |

The latest managed-client checkpoint passes **65 local contracts** in 6.96
seconds. Seven of twelve real stdio cases execute: exact generated launch and
safe five-tool inventory for Codex, Claude, Gemini, and OpenCode; high-level
initialization; a redacted outage tool call followed by another successful
request in the same process; and private raw-child initialization. A
descriptor-ready POSIX transport removed the SDK stdin-worker dependency while
leaving protocol validation and session handling in the SDK. The remaining
four authenticated task/visibility/SQLite/restart cases and the first-class
`harness client-acceptance` runner case require a loopback HTTP harness, which
this runner forbids, so all five remain explicit skips. The command creates a
failure-inclusive mode-`0600` no-overwrite report, uses only deterministic
synthetic computer/provider adapters, and exits nonzero if any client fails. The
machine-readable checkpoint is
[`results/2026-07-25/safety/managed-client-launch-2026-07-26.json`](results/2026-07-25/safety/managed-client-launch-2026-07-26.json).
These local integration/security results do not
change any OSWorld, ScreenSpot-Pro, OCR, or Windows accuracy score. No PiKVM,
VNC target, or production port was contacted.

The accompanying post-change regression groups passed 59 agent/store, 29
authenticated API, 4 static UI, 97 direct-visibility/policy/stop, 43 media/
routing/conformance/config, and 74 provider/onboarding/lab/Office cases. These
group counts are reported separately because the 65-case focused selection
overlaps some of them. `test_direct_burst_runtime.py` reached the known
restricted-runner thread hang and is not counted from partial progress dots.

### One-shot permission/OCR-noise adversarial test

A deterministic seed-`8675309` permission corpus now exercises 1,000 displayed
control labels: 800 consequential controls and 200 safe navigation controls.
The consequential set covers Teams/email sends, form submission, calls and
meetings, deletion, payments, account/permission changes, software
installation, credential entry, legal consent, upload, power actions, local
file commits, settings, generic OK/Continue/Done commits, and security-disable
controls. Each case receives one randomized OCR-like mutation (deletion,
transposition, glyph confusion, insertion, or no corruption) plus randomized
UI context.

The first blind run caught **407/800** consequential controls and missed
**393/800**; all 200 safe controls were allowed. The failures showed that the
old exact regular expressions treated ordinary Save/OK/Continue controls,
permission wording such as Add member/Transfer ownership, and OCR-corrupted
labels as safe. After the policy fix, the frozen corpus catches **800/800**,
allows **200/200** safe controls, and has **0** false negatives, **0** false
positives, and **0** category errors. Security-disable controls are blocked;
other consequential controls pause for exact human approval. Grounded Replace,
Replace All, and Replace in files controls are included as local-file mutations;
Find and Replace navigation remains allowed. The focused generator/policy
selection passes **75 tests** in the final selection.

The executable corpus generator and result writer are
[`dangerous_action_adversarial.py`](dangerous_action_adversarial.py). This
restricted checkout denied creation of the JSON result directory, so no
machine-readable result artifact is linked here yet; the checked-in tests
recompute the same seeded denominator on every run. The broader async runtime
selection reached the known restricted-runner thread/SQLite hang and was
stopped without claiming a pass. No VNC, PiKVM, email, Teams, payment, or other
external surface was contacted.

The current combined permission corpus, OCR confidence/ensemble contract,
managed-lab, provider onboarding, generated-client, clean-launch, and
emergency-stop selection passes **113 tests** with four real-loopback cases
explicitly skipped, in **2.47 seconds**. Syntax compilation and
`git diff --check` pass. Adding the
older async visibility/runtime files to one aggregate invocation reached this
runner's known thread/SQLite hang at 30 seconds; the completed subset is
reported instead of treating partial progress dots as a pass.

The shipped `.mcp.json` now points at `harness managed-mcp`, not the raw PiKVM
server. A regression reads that actual file and rejects `mcp_server`,
`direct-mcp`, or a daemon target in its model-facing entry. Managed MCP no
longer exits merely because the harness UI was not already healthy during
client initialization; the five safe tools remain registered and reconnect on
each call. `--require-ready` preserves explicit fail-fast deployment behavior.

The isolated VNC launcher now has the same authority boundary. `lab up`
supervises the adapter, isolated daemon, and authenticated operator harness,
then emits managed-only configurations for Claude/Gemini, Codex, and OpenCode.
The generated client files contain the agent-token environment variable name,
but no token value, VNC endpoint, daemon URL, or raw MCP entrypoint. A missing
or shared scoped token and a provider route with no usable adapter are refused
before the VNC process is launched. Eight focused lab configuration/lifecycle
tests pass in 0.22 seconds; the combined lab, generated-client, clean-launch,
and emergency-stop selection passes **36 tests** with four real-loopback cases
explicitly skipped in this socket-restricted runner, in **0.84 seconds**. Python
syntax compilation also passes when bytecode is directed to the writable
temporary area. No VNC, PiKVM, or production port was contacted by this
onboarding audit.

Provider onboarding now has a secret-free `harness init` path. It can detect
Codex and Claude account CLIs and compose them with selected OpenAI Responses,
Azure OpenAI, Anthropic, Gemini AI Studio, Vertex AI, and OpenAI-compatible API
routes without reading a token or writing a credential value. Azure modes
cover an API-key environment, externally refreshed Entra-token environment, or
isolated exact Azure CLI argv; Vertex uses an external token or exact gcloud
argv. The public top-level help no longer advertises the raw MCP child, and the
legacy combined client examples now launch `managed-mcp`. The current
onboarding/launch selection passes **16 tests** with
four real-loopback cases explicitly skipped; the normalized-store selection
passes **11 tests**; three sandbox-safe UI release checks and the focused
same-status SSE activity transition pass separately. Across those selections:
**31 passed, 4 skipped**. The active tool/provider field survives a 600-event
tail and clears on matching completion, approval hold, target change, stale
refusal, failure, pause, or terminal run state.

The latest completion audit also passed **64** public-suite
discovery/scoring/report/history tests with one opt-in skip, **109** managed
loop/provider/MCP tests with one opt-in skip, and **25/25** guarded direct-call
visibility tests. Isolated original-runtime checks passed **20/20** burst,
**7/7** core-model, and **36/36** policy tests. A parallel aggregate completed
its assertions but stalled in sandbox-sensitive async fixture teardown, so its
partial dots are excluded. Module compilation, JavaScript syntax, and
`git diff --check` pass.

The current broader harness, provider, report/scoring, historical-audit,
public-suite, performance, MCP, API, client-setup, and static-UI selection
passed **210 tests**, with two explicit environment/opt-in skips, in
**8.45 seconds** (the same warning). After normalized event storage, the same
selection plus storage/migration and OCR-gate coverage passed **217 tests**,
with two explicit skips, in **8.34 seconds**. The focused bounded-payload, storage,
reconnect contract, run-performance, and static-UI selection passed
**20 tests** in **0.70 seconds**. The current focused provider,
configuration, generated-client, and static-UI selection passed **48 tests** in
**0.58 seconds**. It includes the native Responses request contract, the shared
API transport boundary that redacts connection diagnostics, least-privilege
subprocess environment, and the in-flight activity UI contract.

The current public-suite discovery/evaluator, ScreenSpot-Pro scoring,
failure-inclusive scorecard, blind-corpus, real-Tesseract smoke, and Paddle
crop selection passed **55 tests** with two explicit skips in **8.08 seconds**.
The skips were the opt-in 1,000-case OCR gate and the sandbox-forbidden
loopback server lifecycle. The OCR test was then enabled explicitly and
completed 1/1 in **74.59 seconds**, but the test only asserted execution and
provider-error absence. That was not a release gate. It now requires at least
99% normalized exact, at most 0.1% mean CER, at least 95% normalized exact in
every category, exactly 1,000 balanced cases, and zero provider errors. The
current 56.9% report still fails that corrected gate with twelve explicit findings.

A separate UI/live-frame/OCR-oracle selection stalled in this restricted
runner and was terminated. Three bounded verbose reruns isolated the stalls to
the authenticated static UI ASGI test, the observer artifact-receiver ASGI
test, and the `visual_oracle` temp-file round trip with page cleanup enabled.
Other cases executed before those stalls, but partial timeout output is not
added to the denominator. This matches the known static/screenshot/temp-file
sandbox behavior; it is recorded as **not run**, not a pass or product failure.
The completed 217-test selection above is the current post-change regression
denominator.

A later minimal probe narrowed an additional aggregate-vision stall to the
runner itself: `asyncio.to_thread(lambda: 1)` emitted its before-call marker
but did not return within eight seconds. `FrameStore.capture` reproduced that
same boundary, while the real subprocess-backed 1,000-case Tesseract run
completed. Production thread offload has therefore not been replaced with
blocking event-loop work just to make this sandbox green. The exact probe and
affected evidence boundaries are in
[`asyncio-thread-limitation.json`](results/2026-07-25/runner/asyncio-thread-limitation.json).

### Control-mode assurance audit

The console now keeps the assurance boundary visible above every current
transaction. A managed run says that the harness reasoner plans, controller
acts, and an independent verifier evaluates each transition. A direct run says
that the external MCP client chooses the tool calls, the daemon enforces policy
and freshness, and **no independent model verifier is running**.

Both states were rendered in a real browser against the isolated fake-machine
daemon. The direct proof used a synthetic `claude-code` /
`anthropic-oauth` / `opus-4.8` audit record and emitted no HID. At a requested
390×844 viewport the reported inner width was 390 CSS pixels, document scroll
width was 375, and body scroll width was 360, so the new surface introduced no
horizontal overflow. Local CSS and JavaScript asset URLs are versioned so a
deployed console cannot silently retain the previous control-mode copy after
an update.

Machine-readable evidence:
[`control-assurance-audit.json`](results/2026-07-25/ui/control-assurance-audit.json).
This is an observability/assurance audit, not a remote-task accuracy result.

### Target continuity and human-concurrency audit

Historical wrong-machine, nested-desktop, focus-theft, and simultaneous-input
incidents exposed a safety gap shared by managed and direct MCP control. The
daemon now assigns every selected target a visible alias, a hashed stable
fingerprint, a declared desktop layer, and `configured_target` attestation.
The raw endpoint and optional explicit machine id never leave the daemon.
This is configuration continuity, not physical hardware or guest attestation.

The identity is present on session open, every frame/action observation, the
operator console, and every direct-burst approval. Approval/action digests bind
the session and fingerprint. A changed fingerprint latches both managed and
visible direct-MCP runs `blocked`; Continue cannot silently accept the new
target. A reported human cursor move increments the control epoch for every
live session. A second machine client also increments the epoch and returns
`control_changed` before any HID call.

The path was exercised through a real persistent MCP child, isolated daemon,
authenticated harness API, SQLite checkpoint, inline fake frame, and real
browser. Desktop and 390×844 views showed target fingerprint, machine alias,
declared VNC layer, frame/world/epoch, transaction, timeline, Pause, and Stop.
At mobile size the document client and scroll widths were both 375 CSS pixels,
the live screen/transaction/timeline were each 360 pixels wide, and there were
zero console errors. The fingerprint is placed first in the always-visible
screen header; only trailing layer text may ellipsize.

Ten sequential managed MCP-open probes measured 44.682 ms median, 55.544 ms
linear p95, and 57.404 ms maximum. This includes authenticated API, SQLite,
persistent MCP stdio, daemon HTTP, fake capture, image unpacking, and artifact
write. It excludes real video, OCR, model inference, HID, and remote-machine
latency, so it is a local integration diagnostic rather than an SLO.

Shutdown was exercised with an authenticated run-event stream held open. One
SIGINT closed the harness in 0.417 seconds; the server emitted no forced-timeout
error or traceback, the stream client exited normally, and no second signal was
needed. Both isolated loopback test ports were closed afterward.

Focused safety/UI tests passed 63/63 in 1.87 seconds. The complete regression
suite passed 507 tests with one opt-in benchmark skipped in 42.46 seconds.
Machine-readable evidence:
[`target-continuity-audit.json`](results/2026-07-25/ui/target-continuity-audit.json).
No production PiKVM, VNC endpoint, or external computer was contacted.

### Current provider compatibility and readiness audit

The managed harness was also checked against the installed OAuth CLIs rather
than relying only on mocked subprocesses. Codex CLI 0.144.4 and Claude Code
2.1.220 each returned schema-valid output in an empty, read-only temporary
workspace with MCP disabled. On the same synthetic OCR image:

| Route | Configured model string | Result | Elapsed |
| --- | --- | --- | ---: |
| Codex CLI OAuth | `account-default` | exact label, including spacing | 3.839 s |
| Claude Code OAuth | `opus` | normalized-exact label; one repeated space collapsed | 20.284 s |

The compatibility check found that Claude Code 2.1.220 no longer exposes the
`--max-turns` option the adapter supplied. That flag is removed and covered by
a regression test. Codex's required image, read-only, ephemeral, config
isolation, writable ephemeral SQLite state, and output-schema options and
Claude's safe/plan, tool restriction, MCP isolation, no-session, and
JSON-schema options were all present.

Installed Gemini CLI 0.35.3 exposes headless prompt, JSON output, plan mode,
model selection, supplemental admin policy, `GEMINI_CLI_HOME`, and `@` image
preprocessing in its shipped source. The new adapter requires a dedicated
profile, supplies a higher-precedence configuration that disables
MCP/extensions/skills/hooks/context, denies all tools at the admin-policy tier,
and retains only the copied screen in its workspace. Contract tests pass, but
this restricted runner did not complete `gemini --version` within 60.01 seconds
(about 224 MiB peak RSS). Gemini CLI is therefore listed as adapter-proven and
live-unproven—not as a speed result or operational provider. Machine-readable
evidence:
[`gemini-cli-0.35.3-compatibility-2026-07-26.json`](results/gemini-cli-0.35.3-compatibility-2026-07-26.json)
· `sha256:4beb22389eaa`.

Provider readiness now distinguishes local prerequisites from observed
success. A missing executable or credential environment variable is skipped
without making a request. A failed route exposes only a coarse class, enters a
configurable cooldown, and becomes eligible again after expiry. The operator
popover shows adapter/auth ownership, role order, model alias, skips, failures,
cooldown, latency, API interface, pixel-input shape, structured-output contract,
and one of `Operational`, `Not ready`, or `Prerequisites present · unproven`.
It does not treat an environment variable or executable as proof that
authentication works.

A real-browser 390×844 audit showed all four provider rows inside a 359-pixel
popover: document client and scroll widths were both 375 pixels, every row's
width and scroll width were 357 pixels, and the page emitted no console errors.
The probe used only a loopback synthetic API and contacted no computer target.
See
[`provider-compatibility-audit.json`](results/2026-07-25/ui/provider-compatibility-audit.json).

These are compatibility probes, not model-speed rankings. The CLI aliases are
reported as aliases because neither structured response exposed a resolved
backend model. API adapters remain protocol-tested with mock transports; no
live API-key credential was exercised in this audit. The native OpenAI adapter
uses the Responses API rather than translating through Chat Completions,
supplies the current screen as image input, requests strict JSON Schema output,
disables provider-side response storage, and safely classifies refusals without
persisting provider prose.

The Azure adapter uses that same structured Responses contract at
`/openai/v1/responses`. Its authentication seam accepts the official `api-key`
header, an externally refreshed Entra bearer token, or a no-shell bearer
command. Credential-command tests prove that the model task and schema never
enter the command, only allow-listed environment variables are inherited, and
multiline or oversized output is rejected. This remains mock-transport
evidence, not a live Azure result.

Vertex AI reuses the Gemini structured-output path with the official
project/location publisher URL and either a gcloud-owned or externally
refreshed bearer token. The credential command has the same empty-stdin,
allow-listed-environment, bounded-output contract. The request retains inline
image input plus `responseMimeType` and `responseJsonSchema`. This is also
mock-transport evidence, not a live Google Cloud result.

The Codex adapter now also consumes the supported JSONL event stream so usage
is not discarded. A real post-change schema probe reported 19,177 input, zero
cached-input, 84 output, and zero reasoning-output tokens in 8.333 seconds.
That is a transport/accounting check, not a representative task-cost sample.
Public benchmark schema v4 preserves per-stage usage and sums numeric fields;
older artifacts remain unchanged and explicitly lack usage.

One post-instrumentation ScreenSpot-Pro case then verified the complete path:
the case passed in 8.603 seconds and the report retained 22,264 input, 17,152
cached-input, 258 output, and 225 reasoning-output tokens. This is an
instrumentation check only, not an accuracy, average-token, or cost claim.

On 2026-07-27, the product `harness client-task` path completed its first
authenticated outer-client run. The failure-inclusive pair matters: the
original Codex CLI 0.144.4 attempt spent 45.246 seconds, initialized ambient
MCP servers, and then cancelled `computer_start_task` before the harness
created a run. After isolating Codex's client-owned OAuth state and explicitly
preapproving only the non-destructive managed controls, the same product path
exposed one managed MCP and no unrelated MCPs. Codex called
`computer_start_task` and `computer_status`; the harness completed in 13.700
seconds with 22 durable events, three deterministic inner model calls, one
action checkpoint, and one visual-verification revision. Destructive abort
remained prompt-gated. This was target-free: no VNC, PiKVM, production daemon,
or computer target was contacted, and it is not evidence of Windows or Office
accuracy.

## Current headline

<!-- pikvm-scorecard:start -->
_Generated from checked JSON evidence as of 2026-07-27. Manifest `sha256:85394debb8cf`; run `pikvm-agent harness scorecard --check` to detect drift._

| Suite | Route | Cases | Result | Median / p95 | Wall | Status | Evidence |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Independent emergency stop | Explicit CLI → isolated daemon | 7 contracts + 1 live stop | All contracts passed; 1/1 session halted | 0.34s end-to-end | 0.34s | Passing local safety contract | [JSON](results/2026-07-25/safety/emergency-stop-audit.json) · `sha256:683bfec1c505` |
| HID payload-shape gate | Raw burst + MCP preflight | 1,000 payloads / 2 test nodes | 1,000/1,000; 800 unsafe refused; 200 ordinary allowed | Pre-HID | — | Passing local syntax gate; explicit transfer pending | [JSON](results/2026-07-25/safety/hid-payload-shape-gate-2026-07-26.json) · `sha256:debef459a6fb` |
| Read-only media builder | Target-free exact-byte ISO | 10 contracts | 10/10; 2/2 files exact; 6 unsafe names refused | — | — | Passing target-free builder; attach transaction pending | [JSON](results/2026-07-25/safety/msd-media-builder-2026-07-26.json) · `sha256:7351cb177f5c` |
| Virtual-media transaction | Target-free approval/rollback state machine | 19 contracts | 19/19; 11 durable states; daemon bridge exposed: no | — | — | Passing target-free transaction contract; physical bridge gated | [JSON](results/2026-07-25/safety/virtual-media-transaction-2026-07-26.json) · `sha256:7bd38797d675` |
| Windows VNC physical-loop diagnostic | Deterministic MCP trials; no model | 7 trials | 581 prose + 142 code characters exact; overall failed | See per-trial JSON | 336.17s | Failing diagnostic | [JSON](results/2026-07-25/windows/live-vnc-observer-iteration.json) · `sha256:5c7bab3e4abd` |
| Direct MCP visibility bridge | Guarded local SQLite/ASGI | 100 calls + 10 contracts | Contracts passed; 143.8 calls/s | 6.70ms / 7.54ms | 695ms | Local diagnostic | [JSON](results/2026-07-25/ui/direct-mcp-visibility-audit.json) · `sha256:aa923113856c` |
| Managed client launch | Codex + Claude + Gemini + OpenCode | 4 clients; 12 stdio cases | 65 local contracts passed; 7/12 stdio executed; 5 skipped | Not measured | 6.96s local selection | Generated stdio proven locally; authenticated task/restart pending | [JSON](results/2026-07-25/safety/managed-client-launch-2026-07-26.json) · `sha256:b464af4a60c8` |
| Isolated managed client launch | Codex + Claude + OpenCode; Gemini policy contract | 4 installed clients / 54 contracts | 3 native dry-runs; 1 settings-only audit; raw Codex baseline shadowed without persistence | Not measured | Dry-run | Three native isolation dry-runs plus Gemini settings audit; enforcement and tasks pending | [JSON](results/2026-07-26/safety/isolated-managed-client-launch.json) · `sha256:82c085f02a53` |
| Managed smoke lab contract | Target-free app + stdin client task | 24 contracts | 24/24; 44 focused gates | Not measured | Target-free contract | Passing contract; live task rejected-before-process-creation | [JSON](results/2026-07-26/harness/managed-smoke-lab-contract.json) · `sha256:c7bb759ff96b` |
| Live Codex managed task | Codex OAuth → isolated managed MCP → harness loop | 2 failure-inclusive attempts | 2 managed calls; 22 events / 3 inner model calls; completed | 13.70s fixed run | 13.70s | Passing target-free authenticated Codex task; live computer pending | [JSON](results/2026-07-27/harness/live-codex-managed-task.json) · `sha256:5efd19fa4ac7` |
| Operator steering | Authenticated UI → managed replan | 12 tests / 13 contracts | Operator-only durable replan; 131,022-byte UI | — | 0.78s | Passing local contract and browser interaction | [JSON](results/2026-07-25/ui/operator-steering-2026-07-26.json) · `sha256:a4325a2ad4df` |
| Computer-action chat workspace | Target-free synthetic fixture | 14 frontend + 14 harness UI/fixture contracts | 14/14 + 14/14; production build passed | 303,420-byte gzip JavaScript | Target-free contract | Component/build passing; post-change browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-chat-workspace.json) · `sha256:6578fe3fc553` |
| Live computer activity in chat | Authenticated fetch SSE → assistant-ui | 19 frontend + 53 harness/API contracts | 19/19 + 53/53; action 394 → verification 396 | 75ms snapshot coalescing; 305,613-byte gzip JavaScript | Target-free runtime | Authenticated live stream passing; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-live-stream.json) · `sha256:5d61d446891f` |
| Computer-action transaction receipt | assistant-ui tool disclosure → four-phase receipt | 22 frontend + 58 harness/API contracts | 22/22 + 58/58; 4-phase receipt / 8 focused tests | 306,345-byte gzip JavaScript | Target-free contract | Component/build passing; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-receipt.json) · `sha256:739f8fde11de` |
| Model connections and role routing | Authenticated provider catalog → chat Models sheet | 26 frontend + 83 provider/API contracts | 26/26 + 83/83; 10 adapters / 2 configured / 3 roles | 309,038-byte gzip JavaScript | Target-free contract | Catalog/routing UI passing; live providers and browser visual audit pending | [JSON](results/2026-07-27/ui/model-connections-and-routing.json) · `sha256:28d26d4baf28` |
| Computer-use chat controls | assistant-ui → per-role route + action receipt | 36 frontend + 1,052 full Python | 36/36; 1,052 passed / 1 skipped; 3 model roles / 8 action states | 311,264-byte gzip JS; 18,064-byte gzip CSS | Detached commit contract | Passing isolated contract; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-use-chat-controls.json) · `sha256:b835e8680388` |
| Action-bound screen evidence | Controller → computer input → verifier → authenticated image | 38 frontend + 113 focused Python | 38/38 + 113/113; 1,031 broader / 1 skipped; 2 model roles | 311,739-byte gzip JS; 18,134-byte gzip CSS | Target-free contract | Passing target-free contract; browser visual and concurrent Office file pending | [JSON](results/2026-07-27/ui/action-bound-screen-evidence.json) · `sha256:b5c40e8ccb52` |
| Action-bound typing read-back | Watched typer → daemon receipt → assistant-ui action transcript | 42 frontend + 153 focused Python | 42/42 + 153/153; 1,037 broader / 1 skipped; 6 visible read-back states | 313,393-byte gzip JS; 17,936-byte gzip CSS | Detached target-free contract | Passing exact-input/read-back contract; browser visual audit pending | [JSON](results/2026-07-27/ui/action-bound-typing-readback.json) · `sha256:3ee72269bfa9` |
| Live-frame resource envelope | Target-free streamed preview adapter | 6 contracts | 6/6; 4,194,304-byte frame; 8 sessions / 33,554,432 bytes cached | 450ms minimum upstream interval | — | Passing transport resource contract; browser decode pending | [JSON](results/2026-07-25/ui/live-frame-resource-envelope-2026-07-26.json) · `sha256:345d2a92bda7` |
| Normalized storage + bounded control | In-memory production contract | 100,000 events + 100 appends | 11,214.070× write-size reduction; 1,000 control events loaded | 0.086ms / 0.138ms append; 3.372ms / 4.539ms control | 214.978ms import | Serialization diagnostic; real SQLite pending | [JSON](results/2026-07-25/ui/normalized-storage-bounded-control-n100000-2026-07-26.json) · `sha256:9af680551989` |
| Gemini CLI provider adapter | Gemini CLI 0.35.3 / `account-default` | 79 provider/config/UI cases | Adapter contracts passed; startup probe timeout; 228,904 KiB peak RSS | 60.01s startup probe | 60.01s | Adapter contract; live provider unproven | [JSON](results/gemini-cli-0.35.3-compatibility-2026-07-26.json) · `sha256:4beb22389eaa` |
| Provider conformance attempt | Codex CLI + Claude Code | 2 providers / 2 calls | 0/2 exact; 2 failures; Codex adapter fixed afterward | 0.145s / 90.113s | 90.258s | Failing diagnostic; approved rerun blocked before launch | [JSON](results/2026-07-26/providers/provider-conformance-attempt-2026-07-26.json) · `sha256:abff0e7407a7` |
| ScreenSpot-Pro, single pass | Codex CLI / `gpt-5.6-sol` | 100 | 73/100, 73.0% | 7.63s / 13.29s | 218.53s | Current seeded sample | [JSON](results/2026-07-25/screenspot-pro/codex-gpt-5.6-sol-seed104729-n100.json) · `sha256:dc21a201b455` |
| Blind OCR | Local Tesseract structured ensemble | 1,000 | 56.9% selected; 61.4% expected-aware exact; 2.08% CER | 156ms / 215ms | 40.20s | Failing release gate | [JSON](results/2026-07-25/ocr/tesseract-structured-candidates-seed104729-n1000.json) · `sha256:68da9a6bdb5e` |
| Blind OCR | PaddleOCR v6 medium CPU | 1,000 | 78.9% normalized exact; 1.06% CER | 874ms / 2.54s | 1,078.82s | Failing gate; crop adapter fixed afterward | [JSON](results/2026-07-25/ocr/ocr-seed104729-n1000-comparison.json) · `sha256:dbbce9299995` |
| Blind OCR known-intent candidate union | Tesseract precise + PaddleOCR evidence | 1,000 paired cases | 827/1,000, 82.7%; routine 776/800, 97.0%; stress 51/200, 25.5% | Not measured | Retrospective paired analysis | Failing gate; runtime hybrid pass not run | [JSON](results/2026-07-26/ocr/hybrid-known-intent-candidate-union-n1000.json) · `sha256:5b37f898a147` |
| Hybrid OCR worker lifecycle | Tesseract precise + killable Paddle worker | 5 lifecycle cases + 19 contracts | 19/19; hard timeout before yes, after no | 5,025ms / 5,062ms | 5.07s | Process lifecycle fixed; diagnostic only, n=5 | [JSON](results/2026-07-26/ocr/hybrid-worker-shutdown-smoke-2026-07-27.json) · `sha256:766f4b73b6db` |
| OSWorld-Verified tracer | Codex, Claude, and mixed role routes | 9 current; 33 scored + 11 unscored attempts | 6/9 current; 6/33 all scored attempts | 128.56s / 883.97s | 2,583.98s current set | Diagnostic; three current failures | [JSON](results/2026-07-25/osworld/summary.json) · `sha256:061062fbbdbe` |
| Windows Agent Arena | — | 154 tasks discovered | Not run | — | — | Blocked by missing official image | [JSON](results/2026-07-24/inventories/windows-agent-arena.json) · `sha256:c52ba54f6b29` |
| Historical PiKVM incident audit | Claude Code + Codex + OpenCode histories | 24 conversations; 4,453 PiKVM calls | 70 incidents: 20 critical, 27 high | — | — | Available local histories audited | [JSON](historical_pikvm_incidents.json) · `sha256:77e3703476cd` |
| Historical critical/high regression coverage | Checked local control ledger | 47 critical/high incidents | 7 locally covered; 40 partial; 0 open | — | — | Coverage ledger; most incidents remain partial | [JSON](historical_pikvm_coverage.json) · `sha256:d6164522d369` |
<!-- pikvm-scorecard:end -->

The current 100-case Codex sample is 73%, with a Wilson 95% interval of
63.6–80.7%. Its first 20 cases scored 16/20 while the remaining 80 scored
57/80, demonstrating how strongly the earlier 20-case result depended on the
small denominator. Text targets scored 52/68 (76.5%) and icons 21/32 (65.6%).
Windows and macOS slices were nearly identical at 73.8% and 73.7%; the one
Linux case failed and is not a meaningful platform estimate.

The run made 100 calls with zero provider errors. Summed model-active time was
860.24 seconds versus 218.53 seconds wall time, or 3.94 effective concurrent
calls with four workers. Throughput was 0.458 cases/s. One successful call took
50.19 seconds despite a 13.29-second p95, so tail control remains a product
concern. Twenty-six failures were target misses and one was a valid
target-absent response. Token usage was not captured in this schema-v3 run;
instrumentation was added immediately afterward and is required in schema v4.

The older two repeated Codex single-pass runs aggregate to 34/40, or 85%, with
a Wilson 95% interval of 70.9–92.9%. They remain visible as nondeterminism
evidence, not the headline score. The two-stage run made 40 model calls,
consumed 345.3 seconds of summed model-active time, and improved that run from
75% to 80%. It is not yet efficient enough to enable universally.

The Claude result uses the CLI model alias `opus`; Claude Code did not expose a
more specific resolved model identifier in its structured response. Do not
compare its five-case result directly with the 20-case Codex trials.

### Models actually used

The original seven-task OSWorld tracer used Codex CLI `gpt-5.6-sol` for
reasoner, controller, and verifier. The subtitle task then exercised Claude
Code's `opus` alias in all three roles and a mixed route with Claude as
reasoner plus Codex as controller and verifier. None of those subtitle runs
passed, so the comparison is latency and failure evidence—not a model-accuracy
win.

On the selected same-task attempts, all-Claude controller median latency was
25.27 seconds. The mixed controller median was 8.31 seconds, 67.1% lower, and
summed model-active time fell from 838.69 to 657.42 seconds. The mixed run
still timed out and scored `0.0`. Its 16/18 raw action completion also
overstated useful work: only 11 completed actions contained a click, key, text
entry, or scroll; five were observation-only pointer movements. The controller
now rejects multi-move pointer wiggles, and run metrics report
progress-bearing versus observation-only actions separately.

The next mixed run reduced observation-only completions from five to two and
raised the progress-action ratio from 68.8% to 85.7%, but still timed out at
official `0.0`. It exposed a trailing newline hidden in `type_text` and a
separate text-plus-Enter burst. Both are now rejected by the structured action
contract. The fixed-code live run proved those invalid outputs were stopped
before HID, but Codex exhausted its bounded repairs and the harness blocked
after 262.83 seconds with official `0.0`. Safer is necessary; it is not the
same as task success.

Three further clean-VM replays remain failures but moved the boundary forward:

| Run | Route | Progress | Provider behavior | Wall | Terminal state |
| --- | --- | ---: | --- | ---: | --- |
| R14 | Claude reasoner; Codex controller/verifier | 2/4 actions | 1 failure; 0 fallbacks | 185.03s | blocked after stale-frame retries and a timeout |
| R15 | Claude reasoner; Codex controller/verifier | 9/13 actions | 4 failures; 2 repairs; 1 safe-draft downgrade | 696.62s | blocked after provider outages were miscounted as stagnation |
| R16 | Claude reasoner; Codex primary with Claude fallback | 13/19 actions; 12 progress-bearing | 3 failures; 3 fallbacks; 3 repairs; 2 safe-draft downgrades | 860.53s | exact ffprobe proof, then `needs_approval` |
| R17 | Claude reasoner; Codex primary with Claude fallback | 0/3 actions | 0 failures; 2 repairs; 3 stale refusals | 103.51s | blocked on continuously changing video pixels |
| R18 | Claude reasoner; Codex primary with Claude fallback | 0/3 actions | 0 failures; 0 repairs; 3 stale refusals | 80.70s | blocked; exposed `META` alias and two-key navigation shape |
| R19 | Claude reasoner; Codex primary with Claude fallback | 10/15 actions; all 10 progress-bearing | 4 failures; 4 fallbacks; 3 repairs; 1 safe-draft downgrade | 745.72s | ffprobe proof, then `needs_approval` |
| R20 | Claude reasoner; Codex primary with Claude fallback | 13/19 attempts; all 13 completions progress-bearing | 3 failures; 2 fallbacks; 5 repairs; 3 stale retries; 5 repeat stops | 1,071.89s | blocked before approval after OCR/focus loop |

R16 is the first live same-task run to demonstrate the intended multi-model
fallback rather than merely configuring it. Codex controller median latency was
8.17 seconds; the three Claude controller fallbacks had a 21.28-second median.
The run found `/home/user/video.mp4`, visibly proved H.264 video, AAC audio, and
a `mov_text` subtitle stream, then paused before the mutating ffmpeg extraction
command because no human approval was supplied. It still scored official
`0.0`; a safety stop is not task completion.

R16 also retained a real four-character delivery loss (`.mp4`) and one
observation-only pointer move. Independent verification prevented the partial
command from being submitted. R17 and R18 then showed that a playing video can
invalidate every full-frame checkpoint even when the proposed desktop shortcut
does not depend on those pixels. The guarded rebase now recognizes
unchanged-control-epoch `Escape`, `META`/`SUPER`, `Ctrl+Alt+T`, and
non-committing sequences of those keys. It still excludes Enter, clicks,
editing shortcuts, secret text, and any control-epoch change.

R19 escaped the video state, live-proved pointer-only no-op rejection and the
corrected safe-draft evidence contract, found and probed the exact video, and
reached the same approval boundary 114.80 seconds faster than R16. It also
showed that the final exact-text glyphs can appear one capture after the
existing delayed reread. Exact typing now permits a second read-only settled
capture before returning `type_unverified`; it never retypes or submits during
that extra check. These changes are regression-proven, but R19 remains official
`0.0` and no subtitle file was written without approval.

R20 was the first run with an operator-resumable approval path, but it never
reached an approval. It live-proved three bounded stale retries across
`Ctrl+Alt+T` and an uncommitted text draft, including same-idempotency retry
events. It then retained one wrong-window double-click, repeated ambiguous
terminal OCR, and a `move → click → move → click` burst at identical
coordinates. The stagnation guard stopped the run at official `0.0`.
Post-run validation now rejects duplicate pointer activations inside one burst
before checkpoint or HID. A follow-up live run is blocked by the current local
Docker/sandbox approval state, so this last gate is focused-test-proven only.

The durable same-task comparison is
[`model-comparison.json`](results/2026-07-25/osworld/model-comparison.json).
ScreenSpot-Pro records separate Codex `gpt-5.6-sol` and Claude `opus` runs.
The blind OCR baseline is local Tesseract, not a language model. There is no
undisclosed operator model behind these numbers.

## ScreenSpot-Pro

- Evaluator source: `likaixin2000/ScreenSpot-Pro-GUI-Grounding`
- Evaluator revision: `dbe00114bc53a32c61c1a267786da85967710da8`
- Dataset: `likaixin/ScreenSpot-Pro`
- Dataset revision: `210e78d3844251110bff86c95835ebd37a6930fa`
- Discovered corpus: 1,581 cases
- Seed: `104729`
- Scoring: the predicted pixel must be inside the official `x1,y1,x2,y2`
  bounding box. No click tolerance is added.
- Codex concurrency: four calls
- Claude concurrency: two calls

Durable per-case reports:

- [`codex-gpt-5.6-sol-seed104729-n100.json`](results/2026-07-25/screenspot-pro/codex-gpt-5.6-sol-seed104729-n100.json)
- [`codex-gpt-5.6-sol-seed104729-n20-r1.json`](results/2026-07-24/screenspot-pro/codex-gpt-5.6-sol-seed104729-n20-r1.json)
- [`codex-gpt-5.6-sol-seed104729-n20-r2.json`](results/2026-07-24/screenspot-pro/codex-gpt-5.6-sol-seed104729-n20-r2.json)
- [`codex-gpt-5.6-sol-seed104729-n20-verified.json`](results/2026-07-24/screenspot-pro/codex-gpt-5.6-sol-seed104729-n20-verified.json)
- [`claude-opus-seed104729-n5.json`](results/2026-07-24/screenspot-pro/claude-opus-seed104729-n5.json)

Report schema v3 preserves first-pass and post-verification coordinates
separately. Older R1/R2 artifacts retain the schema emitted at run time rather
than being silently rewritten after the fact.

The compact 100-case artifact preserves every case ID, official target box,
predicted point, hit/miss, latency, error class, and platform/application/UI
slice. Case IDs resolve the original instruction and image in the pinned
dataset. Public benchmark schema v4 additionally records first-pass and
verifier usage, but this v3 run predates that instrumentation.

### Issues found by the live runs

1. Codex rejected Pydantic's tuple-oriented `prefixItems` output schema before
   the first model call could be scored. The grounding decision now uses a
   fixed-length homogeneous array accepted by strict Codex output. The
   identical tracer passed after the fix.
2. A Vivado case contains two visible controls that semantically perform
   "Generate Bitstream." The model repeatedly selected the labeled navigation
   action while the official box identifies the toolbar icon. It remains an
   official miss; the report does not silently override the label.
3. An Excel dropdown miss landed 2 pixels outside the official box after the
   verification correction. It remains a miss even though it may be actionable
   in the real application.
4. Repeating the same 20 cases changed the score from 90% to 80%. One-shot
   samples are therefore prohibited as release evidence.
5. Crosshair verification improved one trial by one case but roughly doubled
   calls and wall time. It should be routed selectively by uncertainty/risk,
   not applied to every click.

## OSWorld-Verified

The official repository is pinned locally at
`b7db4d8c85d9e95e0b1db44de5bec954cf37f0cf`. Its current `test_all.json`
contains 369 tasks. A valid score requires the official resettable VM, task
setup actions, file cache, in-guest server, and evaluator. The tracer ran the
official Ubuntu QCOW through Docker with `/dev/kvm`, applied setup outside the
model boundary, drove the guest through harness → MCP → isolated daemon, and
ran the official evaluator afterward. The production PiKVM target was not
used.

Nine supported tracer tasks now have scored runs. Six reached both
`harness_status=completed` and official score `1.0`; inactive-screen dimming,
conda repair, and subtitle extraction scored `0.0`. A tenth compatible task,
desktop-file organization, was exercised but did not reach its evaluator and
remains unscored. The current diagnostic is 6/9, with a Wilson 95% interval of
35.4–87.9%; it is not a representative 369-task OSWorld score.

| Task | Cycles | Actions | Model completions | Model-active | Wall | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Enable Do Not Disturb | 1 | 2/2 | 5 | 40.57s | 47.15s | official `1.0`, completed |
| Set volume to maximum | 1 | 2/2 | 5 | 33.73s | 40.16s | official `1.0`, completed |
| Enable automatic screen lock | 11 | 9/10 | 31 | 295.30s | 369.63s | official `1.0`, completed |
| Rename `todo_list_Jan_1` to `todo_list_Jan_2` | 2 | 5/5 | 12 | 98.29s | 128.56s | official `1.0`, completed |
| Disable inactive-screen dimming | 4 | 5/7 | 18 | 147.87s | 181.05s | official `0.0`, blocked |
| Set timezone to UTC+0 | 11 | 18/23 | 50 | 491.19s | 602.09s | official `1.0`, completed |
| Restore deleted poster from Trash | 1 | 3/3 | 7 | 66.68s | 82.01s | official `1.0`, completed |
| Repair conda environment | 1 | 2/3 | 6 | 54.59s | 61.44s | official `0.0`, approval required |
| Extract and remove video subtitles | 12 | 13/19 | 46 | 833.08s | 1,071.89s | official `0.0`, blocked |

Across the current nine-task set: 180 model completions from 191 provider
attempts, eight structured-output repairs, one deterministic safe-draft
downgrade, three provider failures, one approval, 59/74 completed actions,
2,061.32 seconds of summed model-active time, and 2,583.98 seconds wall time.
End-to-end median/p95 were 128.56/883.97 seconds. Auto-lock and UTC were
accurate but far too slow; the latest subtitle run regressed into ambiguous
terminal OCR and repeated focus actions before reaching an approval boundary.

The iteration history is intentionally less flattering. Thirty-three attempts
reached the official evaluator: seven achieved the goal state, while only six
also had a truthful harness `completed` state. Eleven more attempts ended before
evaluation because of infrastructure, provider, process-lifecycle, or
controller/setup failures. The current 6/9 set is therefore shown beside—not
instead of—the all-attempt history:

| Denominator | Result |
| --- | ---: |
| Current latest-run task set | 6/9 official + harness success |
| All officially scored attempts | 7/33 official goal-state success |
| All scored attempts requiring official + harness success | 6/33 |
| Unscored attempts retained as failures | 11 |

Durable evidence:

- [`summary.json`](results/2026-07-25/osworld/summary.json) is the
  machine-readable denominator, route, aggregate, scored-attempt, and
  unscored-attempt index.
- [`model-comparison.json`](results/2026-07-25/osworld/model-comparison.json)
  compares selected all-Codex, all-Claude, and mixed-role attempts of the same
  subtitle task, including role latency and useful-action efficiency.
- [`f9be0997-dnd-r3/report.json`](results/2026-07-24/osworld/f9be0997-dnd-r3/report.json)
  is the clean Do Not Disturb rerun.
- [`28cc3b7e-max-volume-r1/report.json`](results/2026-07-24/osworld/28cc3b7e-max-volume-r1/report.json)
  is the clean maximum-volume run.
- [`a4d98375-auto-lock-r9/report.json`](results/2026-07-24/osworld/a4d98375-auto-lock-r9/report.json)
  is the clean auto-lock run after eight prior iterations.
- [`e0df059f-rename-folder-r2/report.json`](results/2026-07-24/osworld/e0df059f-rename-folder-r2/report.json)
  is the clean text-entry and file-rename run.
- [`bedcedc4-dim-screen-r3/report.json`](results/2026-07-25/osworld/bedcedc4-dim-screen-r3/report.json)
  is the retained inactive-screen-dimming failure: the requested control was
  absent, the harness blocked rather than changing a substitute, and the
  official evaluator scored `0.0`.
- [`b6781586-utc-r2/report.json`](results/2026-07-25/osworld/b6781586-utc-r2/report.json)
  is the passing UTC+0 run, including its 602.09-second wall time and five
  recoverable typing failures.
- [`5ea617a3-restore-trash-r2/report.json`](results/2026-07-25/osworld/5ea617a3-restore-trash-r2/report.json)
  is the passing restore-from-Trash run: 3/3 actions, seven model calls, no
  repairs or recoverable failures, and 82.01 seconds wall time.
- [`48d05431-conda-r1/report.json`](results/2026-07-25/osworld/48d05431-conda-r1/report.json)
  is the retained conda-repair failure. The guarded terminal mutation required
  approval, no approval was supplied, and the official evaluator scored
  `0.0`.
- [`9f3bb592 subtitle mixed run`](results/2026-07-25/osworld/9f3bb592-subtitles-r11-mixed-stale-plan-preserved/report.json)
  is the 900-second Claude-reasoner/Codex-controller-verifier attempt: 16/18
  raw completed actions, 11 progress-bearing completed actions, two provider
  failures, and official score `0.0`.
- [`R12 pointer-guarded run`](results/2026-07-25/osworld/9f3bb592-subtitles-r12-mixed-pointer-wiggle-guarded/report.json)
  is the follow-up timeout: 12 progress-bearing and two observation-only
  completions, but two recoverable typing failures and official score `0.0`.
- [`R13 commit-separated run`](results/2026-07-25/osworld/9f3bb592-subtitles-r13-mixed-text-commit-separated/report.json)
  live-proves invalid text/commit combinations were rejected before HID. The
  controller then exhausted repairs; the harness blocked and scored `0.0`.
- [`R14 stale-frame run`](results/2026-07-25/osworld/9f3bb592-subtitles-r14-mixed-safe-draft-downgrade/report.json)
  retained two refused text drafts, zero erroneous HID from those refusals, one
  provider failure, and official score `0.0`.
- [`R15 outage/stagnation run`](results/2026-07-25/osworld/9f3bb592-subtitles-r15-mixed-stale-draft-retry/report.json)
  completed nine progress-bearing actions but blocked after four provider
  failures and one repeated focus action were combined by the old stagnation
  counter; official score `0.0`.
- [`R16 multi-model fallback run`](results/2026-07-25/osworld/9f3bb592-subtitles-r16-mixed-fallback-outage-aware/report.json)
  exercised three Claude controller fallbacks, found and probed the video,
  caught a four-character typing truncation before Enter, and then paused on
  the exact terminal-mutation approval request; official score `0.0`.
- [`R17 continuous-pixel run`](results/2026-07-25/osworld/9f3bb592-subtitles-r17-mixed-post-r16-fixes/report.json)
  refused three actions before HID because the playing video advanced the
  full-frame world version; it blocked with 0/3 actions and official score
  `0.0`.
- [`R18 navigation-alias run`](results/2026-07-25/osworld/9f3bb592-subtitles-r18-mixed-global-shortcut-rebase/report.json)
  retained the same fail-closed result and exposed `META` plus an
  `Escape`-then-`META` navigation sequence missing from the first narrow
  rebase; 0/3 actions completed and official score was `0.0`.
- [`R19 approval-gated run`](results/2026-07-25/osworld/9f3bb592-subtitles-r19-mixed-navigation-rebase/report.json)
  closed the player, rejected a pointer no-op, found and probed the exact video,
  completed 10/15 progress-bearing actions, exercised four Claude fallbacks,
  and paused before the exact mutating ffmpeg draft; official score `0.0`.
- [`R20 interactive-approval run`](results/2026-07-25/osworld/9f3bb592-subtitles-r20-interactive-approval/report.json)
  live-proved three bounded stale retries and retained a wrong-window click,
  repeated terminal-focus loops, five pre-HID repeat refusals, and one duplicate
  pointer-activation burst; it blocked before approval with official score
  `0.0`.
- [`5ea617a3 unsupported setup`](results/2026-07-25/osworld/5ea617a3-restore-trash-r1/unsupported-setup.json)
  retains the first unscored attempt. It booted the VM before discovering the
  official `download` setup was unsupported, then stopped with zero model calls
  and zero HID actions.
- [`bedcedc4 interrupted run`](results/2026-07-25/osworld/bedcedc4-dim-screen-r2/interrupted-provider-lifecycle.json)
  and [`UTC interrupted run`](results/2026-07-25/osworld/b6781586-utc-r1/interrupted-provider-lifecycle.json)
  retain the two new unscored lifecycle failures.
- [`f9be0997-dnd-r2/report.json`](results/2026-07-24/osworld/f9be0997-dnd-r2/report.json)
  preserves the first model-driven run: the official state scored 1.0, but the
  harness ended `needs_approval` after 79.70s and only 2/4 actions completed.
- [`attempt-1-infrastructure-failure.json`](results/2026-07-24/osworld/f9be0997-dnd/attempt-1-infrastructure-failure.json)
  records the earlier invalid infrastructure attempt. It made zero model calls
  and zero HID actions, so it is not counted as a task result.

Each scored report retains the SQLite event stream and labelled before/after
images beside it. Unscored attempts have explicit evidence records rather than
invented `0.0` evaluator scores.

### Issues found and fixed by the tracer loop

1. The upstream container's iptables fallback forwarded SSH/RDP but not the
   in-guest API on port 5000. The runner now declares `USER_PORTS=5000`; a
   regression test asserts the fallback launch shape.
2. The initial model-driven run reached the correct OS state, but its verifier
   received only the after-frame. Ubuntu moved the toggle knob immediately
   while its orange accent colour settled later. The verifier treated grey as
   off and proposed a second click that could have undone the successful
   change.
3. Every action verification now receives a persistent, labelled,
   full-resolution BEFORE/AFTER composite. The verifier prompt prioritizes
   geometry such as knob side, checkmarks and selection state over colour. On
   the fixed rerun it explicitly observed the knob moving left → right and
   completed the task.
4. A separate runtime gate now blocks a nearby repeated click on a
   state-changing toggle after failed or uncertain verification. This protects
   the computer even if a future verifier still misreads the transition.
5. The first auto-lock harness declared completion after merely opening the
   settings menu; the official evaluator scored `0.0`. Completion now requires
   indexed visible evidence for every planned success criterion and rejects
   contradictory “not yet” summaries.
6. A provider call stalled for 389.35 seconds. Per-role provider attempts,
   schema repairs, failures, fallbacks, and latency are now persisted live; the
   benchmark timeout is 60 seconds and quiet child-process logs are retained.
7. A truncated guest PNG broke startup. Frame acquisition now fully decodes
   images and retries corrupt frames with bounded backoff.
8. A broad OCR crop around “Screen” also contained “File History & Trash” and
   falsely classified a safe navigation click as deletion. Risk targeting now
   selects the nearest OCR row with a vertical-distance bound and fails closed
   when no reliable row exists.
9. An externally terminated run had reached the desired visual state but never
   ran the evaluator. SIGTERM now cancels through the runner's `finally` block;
   the partial run remains unscored and its isolated container was explicitly
   verified stopped.
10. The planner invented a five-minute lock delay absent from the task.
    Planning now preserves defaults and forbids made-up values or preferences.
11. The first rename run correctly opened the editor, then stalled on bursts of
    three identical pointer moves “to preserve focus.” Duplicate consecutive
    moves are now rejected during structured-output validation before HID, so
    the provider gets one repair attempt.
12. In the passing rename run, the narrow input field clipped the final `2`.
    The verifier refused to commit an unproven value; a bounded caret move
    revealed the exact text, and only then did the controller activate Rename.
13. A fixed four-second burst deadline truncated the exact query
    `dim screen when inactive`. Omitted deadlines are now derived from typed
    character count and declared waits, capped at 110 seconds, and the chosen
    budget/source are returned in the tool result.
14. Search-field prose previously used fuzzy verification and could report a
    partial prefix as completed. Text marked with `context=field` or
    `context=terminal` is now verified exactly and stops before follow-up
    actions on ambiguous read-back.
15. Killing only the top-level Codex process could leave descendants holding
    output pipes, so a 60-second timeout hung indefinitely. Provider CLIs now
    run in their own process group; timeout and cancellation kill the group
    before bounded pipe cleanup.
16. The automated tracer could repeatedly resume a paused model-only state.
    It now stops after two cycles without durable action progress and records a
    blocked run instead of consuming the full cycle budget.
17. The dimming task remains a real failure. The model could not find the
    requested control and safely refused to alter Screen Blank or Automatic
    Suspend. Hidden evaluator rules were not disclosed to make the run pass.
    The UTC task subsequently passed its upstream evaluator, but ten-minute
    latency and 18/23 action completion remain release-blocking efficiency debt.
18. The first restore-from-Trash attempt discovered an unsupported official
    `download` setup only after booting the VM. The tracer now preflights setup
    and evaluator shapes before startup, reproduces official HTTP(S) downloads
    and guest uploads outside the model boundary, and still fails closed on
    unknown types. The corrected task passed 3/3 actions on its first model
    trajectory.
19. Claude initially emitted valid structured decisions that the adapter
    rejected because its schema normalization was incomplete. The provider
    adapter now translates the harness schema and retains repair/failure
    telemetry; the earlier failures remain in the denominator.
20. The policy treated read-only `command -v ffmpeg ffprobe` as terminal
    mutation. A narrow parser exemption now accepts only the shell builtin's
    query form; wrappers, redirects, substitutions, and mutating payloads
    remain guarded.
21. Exact terminal text could be present while full-frame Tesseract misread
    `ls -l` as `1s -ul`. Ambiguous precise typing may now continue only through
    passive evidence waits, then returns `unverified` to the independent visual
    verifier. It can never carry Enter, clicks, keys, or more text behind the
    uncertainty.
22. Stale-world refusals previously discarded the durable high-level plan and
    paid for another slow reasoner call. A stale frame now refreshes the world
    and retries controller selection against the preserved plan; a changed
    control epoch still invalidates it.
23. The mixed-model run exposed a different stall: alternating pointer moves
    were transport-successful but made no task progress. Multi-move
    pointer-only bursts are now rejected before HID. Performance reports
    separate progress-bearing actions from observation-only actions so raw
    completion efficiency cannot hide this failure.
24. R12 embedded a newline inside `type_text` and later bundled a long terminal
    command with Enter. The first caused exact read-back failure; the second
    happened to stop before Enter only because its text was unverified.
    `type_text` now rejects HID control characters, and any following action in
    the same controller decision must be a passive evidence wait.
25. R13 live-proved the new contract prevented the unsafe action from reaching
    HID, but the controller repeatedly returned invalid repaired output after a
    stale refresh. The two-cycle stagnation guard stopped the run at 262.83
    seconds. This remains open model-control reliability debt, not a safety
    exception to relax.
26. R14 showed that a playing video can advance the full-frame world version
    during every model call. An initial optimization rebound a stale
    non-secret draft once, but the checkpoint safety review rejected that
    exception: the current harness discards every stale action after refresh
    and requires a fresh controller decision. Only pointer actions in the
    separately constructed disposable-lab runtime can use target-local
    freshness.
27. R15 showed that transient provider outages and semantic controller loops
    were sharing one stagnation counter. Model/transport outages now preserve,
    rather than increment, the semantic stall count; the wall-time and cycle
    budgets still bound the run.
28. R16 live-proved the configured controller fallback: Codex failed or
    exhausted structured repair three times and Claude returned valid
    controller decisions. Every route index, latency, repair, downgrade, and
    fallback is retained in the event stream.
29. A safe-draft downgrade originally retained expected evidence for the
    removed Enter action, forcing the verifier to request an unnecessary
    replan. Downgraded actions now ask the verifier only to prove that the exact
    draft is visible and unsubmitted.
30. R16 exposed a dropped final `.mp4` chunk and a pointer-only no-op. The
    missing text was not submitted. Post-run typing code retries only that exact
    chunk, once, when exact/code text has a grounded field, settled pixels show
    no meaningful change, and OCR exactly matches the pre-chunk prefix.
    Pointer-only moves are now rejected before HID.
31. R17 and R18 showed that full-frame freshness can livelock on a playing
    video before a desktop shortcut reaches HID. A daemon-confirmed
    pre-execution stale refusal may now be rebound once only when the control
    epoch is unchanged and every active key is a non-committing navigation key:
    `Escape`, `META`/`SUPER`, or `Ctrl+Alt+T`. Enter, clicks, editing shortcuts,
    secret text, and arbitrary key combinations remain excluded.
32. R19 live-proved the pointer no-op guard and normalized safe-draft evidence,
    but an exact command still returned `type_unverified` one capture before
    its final `.mp4` glyphs became visible. Exact prefix-only results now receive
    one additional settled OCR capture. This check is read-only and bounded;
    it emits no key, text, click, or submission action.
33. R19 used four Claude controller fallbacks and reached the exact
    terminal-mutation approval request in 745.72 seconds, 114.80 seconds faster
    than R16. It still scored `0.0`; reaching a safety boundary remains a
    diagnostic result, not evidence that the end-to-end task works.
34. The OSWorld runner previously tore down the resettable VM immediately when
    the daemon requested approval. `--interactive-approvals` now retains the
    same run, checkpoint, idempotency key, MCP session, and VM; it prints a
    redacted exact action and resumes only after an explicit local yes/no.
    Unattended runs retain the original fail-closed behavior.
35. R20 live-proved three allowed stale retries. A first `Ctrl+Alt+T` retry
    encountered a second stale frame and stopped rather than looping; a fresh
    controller checkpoint later completed. A text draft also rebound once and
    remained unsubmitted when visual verification was uncertain.
36. R20 emitted `move → click → move → click` at identical coordinates inside
    one controller burst. That first burst reached HID and is retained as a
    failure. The controller schema now rejects duplicate pointer activations
    within a burst and directs the model to use one click or the explicit
    `double_click` action.

The tracer uses an `isolated_benchmark` policy only inside the resettable,
public VM. It permits reversible guest settings and local edits needed by these
tasks while continuing to require approval for communications, credentials,
sensitive transmission, permissions, installation, power/firmware, disk,
payments, legal actions, deletion, uploads, terminal mutation, and privilege
escalation. Production policy defaults were not relaxed.

The normalized 369-task inventory is preserved at
[`osworld-verified.json`](results/2026-07-24/inventories/osworld-verified.json).
The `harness osworld-case` tracer launches a caller-pinned official QCOW and
container image, applies the selected public setup records outside the model
boundary, drives the computer through harness → MCP → isolated daemon, and
runs the public evaluator outside that boundary. Unsupported setup or evaluator
types fail closed.

The compatibility preflight was then compared directly with the pinned
upstream setup controller. It had unnecessarily capped the runnable set at the
seven tasks already exercised. The outer coordinator now reproduces official
`launch`, `open`, `activate_window`, and `command` setup through the guest
endpoints, and exact-match lists without an explicit conjunction use
upstream's default `and` semantics. This increases preflight-compatible tasks
from 7 to 10 and adds a conda repair, desktop-file organization, and subtitle
extraction case.

Preparing the live reruns exposed a second evaluator-integrity defect: official
`postconfig` records were neither validated nor applied. That would have made
the subtitle task score against missing evaluator dependencies even after a
correct desktop action. A failing contract reproduced it first; the outer
coordinator now validates and applies those records before scoring, still
outside the model/MCP boundary. All 30 public-desktop contract tests pass.

All three compatibility-expansion tasks were exercised. Conda repair and
subtitle extraction reached the official evaluator and scored `0.0`; desktop
organization was cancelled after 1,232.58 seconds and remains unscored.
Compatibility evidence, task IDs, setup mappings, live attempts, and remaining
evaluator limits are in
[`compatibility-expansion.json`](results/2026-07-25/osworld/compatibility-expansion.json).
Every retry must boot a clean official snapshot and retain failures in the
denominator.

## Windows Agent Arena

The official repository is pinned locally at
`6d39ed88c545a0d40a7a02e39b928e278df7332b`. Its current
`test_all.json` contains 154 tasks in 12 domains. A valid run requires the
Windows Agent Arena golden image, its in-guest setup/evaluator server, and a
reset between tasks. The documented local image path requires a Windows 11
evaluation ISO and produces an approximately 30 GB golden image.

Status: task discovery and evaluator shape are verified. The required golden
image is not present, so no score is reported. Upstream requires a human to
visit Microsoft's Evaluation Center, accept its Terms of Service, download the
English (United States) Windows 11 Enterprise Evaluation ISO, rename it to
`setup.iso`, and then build the approximately 30 GB golden image. This project
does not automate acceptance of those terms. Hardware acceleration is
available; the missing, terms-gated ISO/golden image is the remaining
environment blocker.

The normalized 154-task inventory is preserved at
[`windows-agent-arena.json`](results/2026-07-24/inventories/windows-agent-arena.json).
Validation found six upstream split/config ID disagreements. Three are casing
differences; the others alter a suffix or, in one VLC case, identify an entirely
different UUID. They are retained in `integrity_warnings` and are not repaired
or hidden by this project.

## Historical production-use failure audit

The local Claude Code, Codex, and OpenCode histories contained 24 conversations
with 4,453 PiKVM tool calls. A deep, sequence-aware reconstruction identified
70 redacted incident chains: 20 critical, 27 high, 20 medium, and 3 low. These
are not model failure rates—the corpus intentionally records visible
failure/correction chains—but they are direct regression inputs.

A 2026-07-27 source correction also established the route actually used. The
reference Claude conversation made 551/551 PiKVM calls directly; the seven
audited Codex histories made 1,482/1,482 low-level calls, and the two OpenCode
histories made 95/95 direct calls. None used the current managed `computer_*`
facade. Four older Claude conversations made 37 calls through a different
legacy autonomous route; its internal model was not recorded and its failures
must not be attributed to the visible outer Claude model.

- [`HISTORICAL_FAILURE_AUDIT.md`](../docs/HISTORICAL_FAILURE_AUDIT.md) explains
  the failures, model attribution, user corrections, and one-shot risk.
- [`historical_pikvm_incidents.json`](historical_pikvm_incidents.json) contains
  the redacted machine-readable corpus.
- [`historical_pikvm_coverage.json`](historical_pikvm_coverage.json) maps every
  critical/high incident exactly once to a current control family, named
  regression nodes, and an explicit remaining gap.
- `tests/test_historical_pikvm_incidents.py` validates its provenance,
  redaction, summaries, and the two largest reconstructed runaway payloads.

The checked coverage ledger is intentionally severe: of 47 critical/high
incidents, 7 are locally covered, 40 are partial, and 0 are open. The five
editor-transaction incidents moved from open to partial after exact
baseline/diff/diagnostic/rollback contracts landed; those local evaluators are
not presented as real-application proof. Three ledger-contract tests prevent
an incident from being dropped, counted twice, linked to a nonexistent test,
or labelled complete without a remaining limitation.

The highest-risk incidents now have executable gates: 4,265- and
10,259-character input is refused before HID; raw and watched fast-print paths
check the stop gate between submissions of at most 16 characters; panic-stop
cannot return `ok=true` until already-started HID has quiesced; and an in-flight
request cannot overwrite the sticky stopped state afterward. Infrastructure
auto-approval flags, Terraform/OpenTofu apply or destroy, and forced OCI
deletion/termination are now dangerous from command text alone on both direct
and managed paths, even without caller-supplied terminal context. Watched
normal and fast-print typing also compares a fresh pixel grid before every
later chunk. A clustered change outside the established field stops before
further input and reports the exact committed prefix. The fast path never
clears and replays a whole prose draft after an OCR mismatch. Any unverified
read-back blocks Enter, keys, clicks, and further text; only passive evidence
waits may follow. A direct caller's `method=print` is now only a transport hint
when the production watched typer is present, and `no_verify` is refused before
HID.

These focus-theft and submit-boundary protections pass 31 watched-typing tests
and 38 burst/verification tests locally. They are synthetic regressions derived
from the historical failures, not a live Office or notification-shape score.

## Exact Windows guest, foreground, and focus identity

The disposable Windows observer now reports an opaque guest fingerprint,
Windows session, input desktop, foreground executable/process, focused-control
class/id, and whether that focus belongs to the foreground window. The guest
fingerprint is computed inside Windows from a domain-separated SHA-256 hash;
raw MachineGuid and computer name never leave the VM. The evaluator rejects
unknown fields, malformed fingerprints, or missing identity evidence.

Three live attempts failed because the older `observer-v4.exe` still owned the
global snapshot hotkeys. Path-filtered and wildcard PowerShell cleanup proved
ineffective on this VM. Provisioning now uses explicit `taskkill` commands for
the known LAB-only observer image names. The next probe returned exact v5
identity evidence through persistent MCP stdio, an isolated daemon, VNC, and
four screenshot-matrix pages.

The same contract was then compacted at the visual wire boundary. V6 used two
pages and 13.625 seconds, down from four pages and 20.698 seconds: 50% fewer
pages and 34.2% lower end-to-end latency. Both results passed the same strict
snapshot schema. This is a test oracle, not a production-machine dependency,
and it proves only the guest/desktop where the helper runs—not an uninstrumented
nested remote desktop inside that guest.

Machine-readable evidence:
[`observer-environment-identity-probe.json`](results/2026-07-25/windows/observer-environment-identity-probe.json).
The report contains no VNC endpoint or raw Windows machine identity, and the
production PiKVM daemon was not contacted. The latest post-change regression
suite passed 569 tests with one opt-in benchmark skipped in 58.01 seconds
while deselecting the one known-failing Paddle field-crop regression described
below. That historical suite statement is retained; the later focused
remediation is recorded in the OCR section.

## Live Windows MCP/VNC accuracy lab

The post-fix run used deterministic trials rather than an operator model so it
could isolate the MCP, policy, VNC transport, OCR, and observer layers. Every
action crossed MCP stdio and an isolated daemon into the VNC-backed PiKVM API
emulator. The target address remained a runtime argument and is absent from
source, config, and reports. Port `47615` was rejected and the production
PiKVM service was not touched.

The observer binary was rebuilt, downloaded to the disposable Windows VM, and
verified there against SHA-256
`b6a19566f3d4530fc36930241c1c7793ff9f25de39ad96e7823ff3523f4e27f4`.
Its screenshot protocol uses three copies of every payload byte, majority
decode, and end-to-end CRC32. Compact reports retain the full key-down count,
cap the diagnostic key sample at 128 entries, and explicitly say when the
sample is truncated. The LAB-ONLY prerelease is
[publicly inspectable](https://github.com/kierandrewett/pikvm-agent/releases/tag/observer-lab-20260725-0218).

| Trial | Exact source of truth | Result | Time |
| --- | --- | --- | ---: |
| 581-character prose | Windows observer | 581/581, 0 character errors; OCR had 1 normalized error | 61.36s |
| 142-character code | Windows observer | 142/142, 0 character errors; OCR had 6 errors and stopped continuation | 79.56s |
| Duplicate MCP retry | Observer + idempotency record | Exact once; replay did not type twice | 24.56s |
| OCR-grounded benign click | Observer dangerous-event log | Completed after one bounded visual republish | 21.75s |
| Dangerous Send click | Policy + observer dangerous-event log | `needs_approval`; 0 commits | 15.92s |
| Notepad, clean targeted state | File bytes + foreground process | 65/65 bytes, matching SHA-256, `Notepad.exe`; save approval required | 67.76s |
| VS Code, overlapping-window fix | Foreground process + fail-closed runtime | Corrected from `Notepad.exe` to `Code.exe`; OCR remained ambiguous; no save | 83.43s |

Live Office acceptance assigns every attempt a new 16-hex guest filename
inside the dedicated lab workspace. The model receives that exact path and the
observer must return it byte-for-byte before OOXML scoring. This prevents a
stale document from an earlier attempt satisfying the current run. Unsafe
nonces and paths fail before VNC is opened; eight focused regressions cover the
fresh-path contract.

The same run now carries a monotonic `pending → capturing → passed/failed`
artifact state. Only the distinct observer credential can publish it: both the
model-facing agent token and browser operator token receive HTTP 401. A passed
state requires a completed managed run, the exact file format, byte count,
SHA-256, and every declared host-side semantic check; passed and failed states
are immutable. The run rail, current transaction, and verification timeline
show this state instead of presenting model completion as saved-file proof.
If the observer evidence channel cannot enter `pending` immediately after run
creation, the runner aborts the managed run before polling or continuing it.

The coherent seven-trial run is still a failure. Its measured trial wall time
was 336.17 seconds, median 39.85 seconds and p95 89.09 seconds. The visual
oracle alone required 35 screenshot pages across the seven trials. That
redundancy recovered a persistent single-cell corruption and one missing-border
capture, but it is too slow for an interactive product path.

The loop found and fixed VNC lock-key crashes, post-HID HTTP 500 evidence loss,
single-cell oracle corruption, shifted-letter case loss, UK pipe/backslash
mapping, discarded partial benchmark records, missing partial-typing progress,
and overlapping-window activation. It did not paper over the remaining
failures:

- tiny code OCR substitutes load-bearing brackets and quote characters;
- exact input can therefore be correct while the runtime safely refuses Save
  or Enter;
- repeated editor attempts pollute a desktop that is not reset between runs;
- an attempted `taskkill` cleanup was approval-gated, then stopped before
  Enter because its command text could not be verified. No process was killed,
  and that cleanup shortcut was removed.

A release-evidence run now requires a VM snapshot reset between attempts.
The complete redacted metrics and defect ledger are in
[`live-vnc-observer-iteration.json`](results/2026-07-25/windows/live-vnc-observer-iteration.json).
The result supports a claim that the tested transport and safety gate worked;
it does not support a claim that OCR or the full editor workflow is
production-ready. After these fixes, the complete regression suite passed 467
tests with one opt-in benchmark skipped.

## OCR and physical-computer diagnostics

The seeded blind OCR corpus contains 1,000 cases across prose, UI labels, code,
terminals, paths, URLs, identifiers, numeric confusables, punctuation, and
mixed case. The recognizer sees opaque image names, and the ground truth is not
written until every OCR call has finished.

Report schema v4 additionally labels the 800 ordinary desktop-text cases
`routine` and the 200 numeric-confusable/dense-punctuation cases `stress`.
This is a diagnostic split, not a scoring exemption: stress cases remain in
the overall denominator and the unchanged 95%-per-category exactness gate.
The stress tier adds a 10% mean-CER ceiling.

| Metric | Result |
| --- | ---: |
| Exact | 44.9% |
| Normalized exact | 56.9% |
| Expected-aware exact candidate | 61.4% |
| Mean character error rate | 2.08% |
| Median / p95 | 156ms / 215ms |
| Throughput | 24.9 images/s with four workers |

A fresh schema-v4 blind rerun kept the same 569/1,000 selected result and
614/1,000 exact-candidate result. Routine cases scored 569/800 (71.125%) with
0.885% CER; stress cases scored 0/200 with 6.849% CER. The 12 existing release
failures all remained active. The deterministic diagnosis rejected PSM 7,
raw-line PSM 13, candidate selection, degradation, and font family as primary
causes of the stress zeroes: the fixed `0O1Il|` token and dense ASCII
quote/backtick/operator string are intrinsically adversarial. Exact evidence
is in
[`tesseract-tiered-diagnosis-n1000.json`](results/2026-07-26/ocr/tesseract-tiered-diagnosis-n1000.json).

The current full rerun completed all 1,000 blind calls with zero provider
errors in 40.203 seconds of evaluation time. Tesseract word boxes sometimes
split one URL, path, hash, or terminal token into several words. The provider
now removes those invented spaces only when both the pixel gap and an existing
machine-token marker agree. A balanced 200-case development slice improved
from 45.5% to 56.5%. The untouched full corpus improved from 44.0% to 54.1%:
101 cases became normalized-exact and no previously exact case regressed.
Paths moved from 23% to 64%, URLs from 14% to 50%, terminals from 58% to 75%,
and identifiers from 61% to 68%.

The current selector additionally rejects impossible whitespace in URLs/UNC
paths and malformed hashes or run identifiers before trusting confidence. On
the full paired replay it changed 29 selections, added 16 normalized-exact
results, and regressed none: identifiers reached 76%, paths 68%, and URLs 56%.
The selected result is now 569/1,000. Raw and prepared reads are both retained
without receiving expected text. When the typing verifier already knows a
precise intended string of at least eight characters, an exact independent
candidate may verify it; nearest guesses never can. This raised exact candidate
coverage to 614/1,000, 45 above the selected read, while leaving the general
OCR release gate unchanged.

The application factory and shipped example configuration now use this exact
measured profile: PSM 6, 2× preprocessing, independent raw/prepared reads, and
syntax-aware selection. Each setting remains explicit and configurable. Two
factory regressions and the example-config contract prevent a benchmark-only
profile from silently differing from production runtime behavior.

The current retained comparison is
[`tesseract-structured-candidates-seed104729-n1000.json`](results/2026-07-25/ocr/tesseract-structured-candidates-seed104729-n1000.json).
The preceding token-fragment checkpoint is
[`tesseract-token-rejoin-seed104729-n1000.json`](results/2026-07-25/ocr/tesseract-token-rejoin-seed104729-n1000.json).
The prior exact rerun and execution-only test timing remain preserved in
[`tesseract-seed104729-n1000-current-rerun.json`](results/2026-07-25/ocr/tesseract-seed104729-n1000-current-rerun.json).

The original 20.2%-exact / 16.1%-CER result is superseded. Diagnosis found
that 450 cases scored complete strings even though the renderer had clipped
part of the string outside the image. The generator now enforces that every
scored character is visible. On that corrected corpus, the old single
preprocessed candidate reached 31.2% normalized exact and 4.67% CER.

The provider independently reads the raw image and an
autocontrasted/upscaled version, then selects by OCR confidence and
disagreement shape without access to ground truth. A paired failure analysis
then found that coloured window controls were repeatedly recognized as a short
leading word such as `eee`. The corrected provider removes that OCR line only
when source pixels prove three similarly sized coloured blobs in the title-bar
region. On the same 1,000 cases it added 67 normalized-exact results with zero
paired regressions, raised normalized exact from 37.3% to 44.0%, and reduced
CER from 4.04% to 2.83%. The later token-fragment correction described above
superseded 44.0% as the next checkpoint; the structured selector described
above now supersedes it.

Confidence alone is unsafe. At a reported mean confidence of at least 0.90,
Tesseract covered 236 cases but was still wrong on 23. The widest zero-error
slice begins at 0.948, but covers only 36/1,000 cases and its Wilson 95% lower
accuracy bound is 90.36%. At 0.95, all 28 covered cases were correct but the
lower bound is only 87.94%. No threshold has a 99% Wilson lower accuracy bound.
Confidence discriminates somewhat (AUC 0.711), but its 10-bin expected
calibration error is 0.250 and its Brier score is 0.291. It may prioritize
rechecks; it cannot authorize exact or irreversible work. Numeric confusables
and punctuation remain at 0% normalized exact. The current calibration is
retained in the structured-candidate report linked above.

PaddleOCR PP-OCRv6 medium was then run on the exact same 1,000 rendered cases,
not a separately generated small sample. It reached 78.9% normalized exact and
1.06% CER, with 874ms median, 2.54s p95, and 1,078.82 seconds wall time on an
8-core Ryzen 7 5800X CPU. It scored code 94%, identifiers 99%, paths 91%,
prose 94%, terminals 93%, UI labels 95%, and URLs 91%. Punctuation was only
51% and numeric confusables remained 0%, so this still fails the release gate.
Wilson 95% intervals are 53.8–59.9% for the current Tesseract rate and
76.3–81.3% for Paddle.

The 56.9%-Tesseract output was paired by case ID with the same Paddle run.
Tesseract and Paddle were both correct on 539 cases, only Tesseract was correct
on 30, only Paddle on 250, and neither on 181. Even an impossible
ground-truth oracle choosing the better answer reaches only **819/1,000**.
The two engines produced the same normalized text on 551 cases; 539 agreements
were correct and **12 were identically wrong**. Agreement therefore has 55.1%
coverage, 97.82% observed accuracy, and a Wilson 95% interval of
96.23–98.75%—useful recheck evidence, but not safe commit authority. UI-label
agreement happened to be 85/85, yet its Wilson lower bound is only 95.68%.

For the narrower case where the exact intended text is already known, retaining
all Tesseract precise-read candidates plus the independent Paddle result found
an exact candidate on **827/1,000** paired cases: 776/800 routine cases (97.0%)
and 51/200 stress cases (25.5%). Expected text is consulted only after both OCR
calls and only for exact equality; no nearest candidate is selected. The
runtime now has an opt-in hybrid provider that keeps ordinary screen parsing
on Tesseract, bounds the secondary wait, and exposes Paddle only as
precise-read evidence. These figures remain a reconstruction from two
completed frozen-corpus reports, not a new 1,000-case runtime pass.

The first runtime implementation put Paddle in `asyncio.to_thread`. A
five-case probe scored every case in 25.109 seconds but could not shut down and
was hard-killed at 180 seconds. Paddle now runs in one persistent, killable
child process shared by the runtime OCR paths. A timed-out read leaves at most
one shielded worker request; follow-ups cannot queue overlapping inference.
The same five-case lifecycle probe then ran with four parallel primary workers,
scored in 5.068 seconds, and exited normally under a 90-second boundary, with
19/19 focused contracts. Participation is explicit: five precise calls made
four secondary attempts, one completed, three timed out, and one was skipped
while the worker was busy. Five cases are diagnostic only, so hybrid accuracy
and the release gate remain unproven. Neither candidate evidence nor engine
agreement grants commit authority. Evidence:
[`hybrid-known-intent-candidate-union-n1000.json`](results/2026-07-26/ocr/hybrid-known-intent-candidate-union-n1000.json)
and
[`hybrid-worker-shutdown-smoke-2026-07-27.json`](results/2026-07-26/ocr/hybrid-worker-shutdown-smoke-2026-07-27.json).

The executable analysis is
[`ocr_ensemble_analysis.py`](ocr_ensemble_analysis.py), and its
synthetic regression explicitly proves that two engines can agree on the same
wrong text.

The Paddle field-verification path also exposed a separate defect: it accepted
a requested crop region but ran inference on the full screenshot. The adapter
now creates an ephemeral region-only image inside its worker, preserves the
source frame, and removes the crop after both success and failure. Three
focused tests reproduce the original 120×80-versus-30×20 mismatch and pass
after the fix. This is an adapter-boundary result; a live model-backed field
rerun remains outstanding and the published 1,000-case full-image accuracy is
unchanged. See
[`paddle-region-remediation.json`](results/2026-07-25/ocr/paddle-region-remediation.json).

The complete paired metrics, hardware/software versions, confidence coverage,
defect ledger, and limitations are in
[`ocr-seed104729-n1000-comparison.json`](results/2026-07-25/ocr/ocr-seed104729-n1000-comparison.json).
The earlier Tesseract and 100-case Paddle artifacts remain preserved as
historical evidence rather than being rewritten.

The isolated Windows VNC diagnostic used the Codex CLI `account-default` route
for planner, controller, and verifier. It completed 8 of 10 checkpointed
actions but did not complete the task. Controller, reasoner, and verifier
median latencies were 7.2s, 8.8s, and 11.0s. This remains a failed diagnostic,
not an end-to-end pass.

## Reproduction

Download the public ScreenSpot-Pro data at its pinned revision:

```bash
hf download likaixin/ScreenSpot-Pro \
  --repo-type dataset \
  --revision 210e78d3844251110bff86c95835ebd37a6930fa \
  --local-dir /tmp/screenspot-pro-official
```

Run a configured provider:

```bash
pikvm-agent harness screenspot-pro \
  --config /path/to/harness.yaml \
  --provider codex-gpt-5-6-sol \
  --dataset /tmp/screenspot-pro-official \
  --suite-revision dbe00114bc53a32c61c1a267786da85967710da8 \
  --dataset-revision 210e78d3844251110bff86c95835ebd37a6930fa \
  --limit 20 --seed 104729 --jobs 4 \
  --out /tmp/screenspot-results
```

Add `--verifier-provider <provider>` to exercise the experimental independent
crosshair verifier. Omit `--limit` for the full 1,581-case corpus.

Validate public stateful-suite inventories and run one supported official
OSWorld tracer:

```bash
pikvm-agent harness suite-inventory \
  --suite osworld-verified \
  --repo /path/to/pinned/OSWorld \
  --revision <git-sha> \
  --out /path/to/osworld-inventory.json

pikvm-agent harness osworld-case \
  --repo /path/to/pinned/OSWorld \
  --revision <git-sha> \
  --qcow /path/to/Ubuntu.qcow2 \
  --docker-image <tag-or-digest> \
  --task-id <official-task-id> \
  --config /path/to/harness.yaml \
  --operator-console \
  --out /path/to/case-results
```

`--operator-console` requires the access-token environment variable named by
the harness config (by default `PIKVM_HARNESS_TOKEN`) to contain at least 32
characters. The runner prints the loopback URL; paste that token into the
browser gate. For a terminal-only gate use `--interactive-approvals` instead.
The two approval modes are deliberately mutually exclusive.

Run the blind OCR and inspect a saved physical-computer task:

```bash
pikvm-agent harness ocr-benchmark \
  --cases 1000 --seed 104729 --evaluation-seed 65537 \
  --jobs 4 --out /tmp/pikvm-ocr-blind

pikvm-agent harness run-metrics \
  --state .pikvm-harness/state.sqlite3 \
  --run-id <run-id>
```
