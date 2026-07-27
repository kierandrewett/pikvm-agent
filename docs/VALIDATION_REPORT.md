# Validation report

Date: 2026-07-27

This report records the evidence gathered while building the VNC-backed
accuracy lab and provider-neutral operator harness. It intentionally omits the
runtime VNC address and credentials.

## Isolation

- The live work used an isolated adapter and daemon pair on the lab-only ports.
- The lab supervisor rejects the production daemon port.
- The generated daemon configuration pointed only at the loopback adapter.
- The generated coding-client configurations start only the high-level managed
  MCP facade. They contain no daemon URL, raw MCP entrypoint, VNC endpoint, or
  credential value.
- The interactive lab preflights three distinct scoped credentials and a usable
  model route before opening VNC, then supervises the adapter, daemon, and
  operator harness as one lifecycle.
- The production PiKVM service was not called, reconfigured, or stopped.
- The lab processes were stopped after the trials.
- A source scan found no runtime VNC host, port, credential, or temporary-tunnel
  address in the repository.

## Historical transcript replay

The supplied direct-MCP transcript was imported as evidence rather than treated
as an operator-loop run:

| Signal | Observed |
| --- | ---: |
| Transcript records | 4,768 |
| Direct HID bursts | 513 |
| Screenshot calls | 30 |
| Open calls | 8 |
| Typed characters | 70,968 |
| Type-and-submit bursts | 345 |
| Typed payloads over 120 characters | 285 |
| Base64-bearing bursts | 141 |
| Dangerous submits identified by the importer | 20 |
| Caller-stable idempotency keys | 0 |

The importer stores lengths, classifications, and hash prefixes instead of
retaining the typed content. The transcript confirms that the coding client
called direct burst tools and therefore acted as the planner; it did not use
the daemon's autonomous operator graph.

The replacement managed API now treats a submitted task as one supervised
transaction. Twenty-one focused API tests cover bounded action slices,
verifier-more-work retries, safe replanning, exact post-approval continuation,
overlapping Continue suppression, and process recovery. A restart regression
found that the automatic-resume counter was process-local and could therefore
be reset by a crash. The supervisor now reconstructs that counter from the
complete durable event transcript, preserves one slice authorized immediately
before a crash, and stops before authorizing another slice at the configured
ceiling.

## Provider adapter smoke tests

These were one-shot exploratory measurements, not latency guarantees:

| Adapter | Authentication boundary | Structured result | Elapsed |
| --- | --- | --- | ---: |
| Codex CLI | CLI-owned saved login | 3-step plan and 2 criteria | about 10.7 s |
| Claude Code CLI | CLI-owned saved login | 3-step plan and 2 criteria | about 70.5 s |

Azure OpenAI now reuses the native structured Responses adapter at the current
`/openai/v1/responses` boundary. Contract tests cover the `api-key` header and
an isolated bearer-command source suitable for Entra/Azure CLI: the command is
an exact argv vector, receives empty stdin, inherits only named environment
variables, and rejects multiline or oversized credential output. Configuration
and onboarding tests cover API-key and CLI-owned OAuth modes without persisting
a token value. These are mock-transport contracts, not a live Azure claim.

Vertex AI now reuses the Gemini structured-output adapter with the official
project/location publisher endpoint and either gcloud-owned or externally
refreshed bearer authentication. The credential command has the same
empty-stdin, allow-listed-environment, bounded-output contract, while the model
request retains inline image input and `responseJsonSchema`. This is also a
mock-transport contract, not a live Vertex claim.

Both adapters ran with a strict output schema, an isolated working directory,
no PiKVM MCP access, and provider-subprocess cancellation. API-key adapters are
covered by contract tests.

Gemini CLI 0.35.3 was separately inspected from its installed package and now
has a dedicated-profile OAuth adapter. Contract tests prove a fresh
screen-only workspace, `GEMINI_CLI_HOME` ownership without token access,
harness-supplied system settings that empty the MCP catalogue/allow-list and
disable skills/hooks/ambient context, an explicit `--extensions none`
selection, a supplemental deny-all tool policy, JSON-envelope parsing, usage
retention, and provider-error redaction. Native inspection found that this
version replaces file-based `admin.*` settings during its effective-settings
merge, so the adapter no longer relies on those ignored controls. Gemini CLI
lacks a native
response-schema flag at this version, so the harness performs the authoritative
Pydantic validation and bounded repair before HID. This restricted runner could
not complete even `gemini --version` within 60.01 seconds (about 224 MiB peak
RSS), so this is protocol/source evidence, not a live Gemini model or latency
claim. The exact flags, isolation assertions, probe result, and regression
counts are retained in
[`../bench/results/gemini-cli-0.35.3-compatibility-2026-07-26.json`](../bench/results/gemini-cli-0.35.3-compatibility-2026-07-26.json).

### Current OAuth CLI and provider-health compatibility

A fresh compatibility pass used Codex CLI 0.144.4 and Claude Code 2.1.220.
Both returned schema-valid structured output without MCP access. A local
synthetic image containing `Open settings  [0002-QEW44]` produced:

| Route | Model string returned | Read-back | Elapsed |
| --- | --- | --- | ---: |
| Codex CLI OAuth | `account-default` | exact, including repeated space | 3.839 s |
| Claude Code OAuth | `opus` | normalized exact; repeated space collapsed | 20.284 s |
| Gemini CLI OAuth | `account-default` | adapter contract only; local CLI startup timed out | >60.01 s |

This pass caught a real adapter incompatibility: Claude Code 2.1.220 does not
offer `--max-turns`, but the adapter passed it. The unsupported flag is removed
and regression-tested. All other required isolation, image, permission,
session, and schema flags were present in the installed CLIs.

Readiness is now evidence-based. Providers with missing local prerequisites
are skipped, failed routes enter a configurable cooldown, expired routes can
recover, and UI/API errors contain coarse classifications rather than raw CLI
stderr or HTTP response bodies. A provider with an executable or credential
environment variable but no successful call is shown as `Prerequisites
present · unproven`, not Ready.

The new computer-free provider-conformance command closes the next evidence
gap without pretending to be a desktop-task benchmark. It renders identical
seeded 960×540 screens for every selected route, requests one strict
three-field schema, retains failed calls and unavailable routes in the
denominator, records returned model/normalized token usage/median and p95
latency, and reduces exception bodies to coarse failure classes. It requires
the explicit `--allow-provider-calls` flag and has no daemon, VNC, PiKVM, HID,
or computer client.

Reports are new mode-0600 files and cannot overwrite prior evidence. The
running provider-health endpoint reloads the configured report path and emits
only exact/schema/latency/failure/timestamp aggregates; prompts, ground truth,
observed text, raw provider output, image paths, and exception bodies do not
enter the UI shape. Invalid reports become `invalid-report` rather than
returning partially trusted fields. Seven focused conformance/CLI/privacy
contracts pass. This runner has not made live calls through the new command,
so no cross-provider score or routing recommendation is claimed.

On 2026-07-26 the first two-provider conformance attempt retained both failures:
Codex failed before its model call in 145 ms because its SQLite state inherited
a read-only location, while Claude reached the runner's 90.113-second timeout.
Codex now receives a writable ephemeral `sqlite_home` outside its empty model
workspace; three focused regressions and the 76-case provider/config/routing
selection pass. A post-fix, target-free 15-second probe made both adapters reach
the restricted outbound boundary instead of failing locally. The requested
approved-access rerun and the separately authorized disposable-VM identity
probe were rejected before process launch by this host's execution policy.
Those are recorded as `not_executed`, not as model or VM failures, and no
accuracy score is claimed.

Provider setup now has a secret-free generator:

```bash
pikvm-agent harness init --out config.harness.yaml
```

It auto-detects supported Codex/Claude account CLIs plus Gemini CLI only when
its dedicated profile environment is configured, and can compose them with
selected OpenAI Responses, Azure OpenAI, Anthropic, Gemini AI Studio, Vertex
AI, and OpenAI-compatible API routes. It persists environment-variable names
and an optional exact credential-command argv, never credential values,
refuses an empty provider set, and refuses to replace an existing file unless
explicitly forced. Eleven focused onboarding test functions pass. The combined
onboarding and public client-launch selection passes 19 cases with four
socket-restricted loopback cases explicitly skipped.

Provider health now carries a non-secret authentication mode and optional
credential-command executable into the operator UI. Static contracts prove the
popover distinguishes saved CLI login, API-key environment, bearer-token
environment, and CLI bearer-token ownership, including `az` or `gcloud`,
without exposing a credential value or environment-variable name.

The chat workspace now has a first-party Models sheet backed by an
authenticated canonical catalog rather than a hard-coded frontend list. It
shows all ten maintained adapters, the configured reasoning/acting/checking
route and fallback positions, readiness, authentication owner, coarse
success/latency, and conformance state. The component never renders raw
readiness/provider errors or credential source paths, and it offers no browser
secret-entry form. A 404-only fallback keeps the workspace usable during a
rolling update from an older server. Twenty-six frontend and 83
provider/API/config/UI tests, the production build, and the TypeScript
similarity scan pass. No provider or computer was contacted.

The responsive provider popover was exercised in a real browser at 390×844.
The document client and scroll widths were both 375 pixels; four 357-pixel
provider rows fit a 359-pixel popover with no horizontal overflow or console
errors. The browser fixture was loopback-only and contacted no computer. The
full record is
[`../bench/results/2026-07-25/ui/provider-compatibility-audit.json`](../bench/results/2026-07-25/ui/provider-compatibility-audit.json).

The returned model strings remain vendor CLI aliases because neither CLI
exposed a resolved backend model. API-key adapters were exercised with mock
transports only; this pass makes no live API-provider claim.

The current production assistant-ui/shadcn workspace totals 1,206,165 raw
bytes including local fonts (713 HTML, 1,022,916 JavaScript, 106,116 CSS, and
76,420 fonts). JavaScript compresses to 309,038 bytes and CSS to 17,693 bytes.
Tests cap each asset at 1.1 MB, total assets at 1.25 MB, and gzip JavaScript/CSS
at 320/24 KiB. This supersedes the obsolete 128 KiB hand-built-console
envelope. Current provider/tool activity is stored outside the 500-event
visible tail and streamed on start/end even when the run status does not
change. Focused store, static UI, and direct async-generator stream checks
pass.

The chat workspace now consumes that authenticated stream rather than waiting
on an unconditional 750 ms full refresh. A target-free runtime trace delivered
model completion, checkpoint, exact action attempt, action completion, and
independent verification as ordered events 391–396. The client coalesces
snapshot reads for 75 ms, reconciles every 15 seconds while live, and discloses
its Connecting, Live, Reconnecting, or Updates offline state; degraded mode
uses 1.5-second bounded polling with 0.5–5-second reconnect backoff. Nineteen
frontend and 53 harness/API/fixture tests pass. The in-app browser URL policy
blocked the post-change visual/reflow inspection, so this is runtime and
component evidence rather than a browser-layout pass.

The computer-action disclosure now presents that stream as a four-phase
transaction receipt: source screen, bounded keyboard/pointer input, HID
delivery, then independent screen check. Typed payloads retain the exact body
with visible character and line counts; pointer button and coordinates are
separate tokens; key and `keypress` actions share the same keycap treatment.
The receipt says explicitly when consequential input is held and not sent, and
only a linked verification event earns the green Verified state. Twenty-two
frontend tests, including eight focused receipt tests, 58
harness/API/fixture/provider tests, the production build, and the TypeScript
similarity scan pass. No computer or model API was contacted for this pass.

The next computer-use UI pass was validated from a detached worktree at commit
`c25c337`. The composer exposes independent reasoning, acting, and checking
primaries before send, preserves configured fallbacks, and locks the durable
route while a run is active. Computer calls now show an action-specific summary
and expand into intended effect, expected visual evidence, exact input, a
source/input/observed transition, attempt, transport latency, and idempotency
key. Coordinate input includes a normalized target map against screen
dimensions; typed text, key chords, and step-based scrolling each retain their
own presentation. Raw transport errors do not enter the receipt. All 36
frontend tests, including 12 focused computer-action cases, and all 161 focused
provider/agent/API/store/fixture/static-UI tests passed. The production bundle
was 1,217,335 raw bytes, with 311,249-byte gzip JavaScript and 18,064-byte gzip
CSS. The in-app browser URL policy still blocked the post-change visual/reflow
inspection, so this is isolated component/build evidence rather than a
browser-layout result.

A production-event regression then showed that the chat projection linked the
fixture's `verification.completed` event but not the real harness event,
`model.completed` with role `verifier`. The repaired projection accepts both
without inventing verifier evidence. It associates the last controller result
with the action checkpoint and the verifier result only within that action's
outcome window, so provider/model/latency attribution does not bleed across the
next attempt.

Before/after composites are now durable action artifacts rather than a single
mutable “latest image.” The run retains at most 64 revision records containing
the action index and before/after frame IDs; host paths remain excluded from the
visible run. The browser fetches the selected revision through an authenticated
no-store endpoint, clears the previous image before a new revision loads, and
revokes object URLs on cleanup. The synthetic UI fixture emits the same
production-shaped model events and a labelled PNG without opening a computer
target.

At commit `7b98468`, 38/38 frontend tests, 113/113 focused Python contracts,
the production build, resource gate, and TypeScript similarity scan passed. A
broader non-Office run completed 1,031 tests with one skip in 79.79 seconds.
The repository-wide run was interrupted after 700 passes, one skip, zero
failures, and 692.72 seconds because a concurrently modified Office acceptance
file was independently CPU-bound; this is explicitly not a full-suite pass.
The bundle remains inside its current envelope at 1,222,540 raw bytes,
311,739-byte gzip JavaScript, and 18,134-byte gzip CSS. Browser visual/reflow
inspection remains blocked by the in-app browser URL policy.

A subsequent complete target-free regression caught one legacy playbook test
calling the hardened HID path without its required caller-stable idempotency
key. The runtime correctly failed closed; the test was updated to use the
public call shape rather than generating a server-side key or weakening the
boundary. The repaired branch completed 1,052 tests with zero failures and one
environment-dependent skip in 86.80 seconds. All 36 frontend tests, the
production build, public scorecard check, and TypeScript similarity scan also
passed at `e4bb5ef`.

Operator steering now has a durable managed-run boundary: only the browser
operator credential can record guidance; an in-flight provider wait is
cancelled before replanning; the next reasoner sees that guidance; and pending
HID, direct MCP, and externally driven benchmark runs cannot be silently
re-owned. The focused target-free selection passed all ten tests. The in-app
Chromium fixture then exercised the real Guide dialog, exact instruction and
`auto_resume` payload, successful close, stale-plan removal, durable guidance,
timeline event, and ownership toast. At a requested 390×844 viewport, document
client and scroll widths were both 375 pixels, the transaction was 360 pixels
wide, and Guide remained visible. The deterministic API contacted no computer
or provider. See
[`operator-steering-2026-07-26.json`](../bench/results/2026-07-25/ui/operator-steering-2026-07-26.json).

Managed verification now exposes the exact labelled before/after composite
used by the verifier inside the transaction view. Two API contracts prove that
the image is bearer-authenticated, no-store, revisioned, absent until a
verification exists, and returned without serializing its local path. Static
UI contracts prove authenticated fetch, loading/error state, and blob URL
revocation. This surface was not exercised in a real browser because browser
startup is disallowed in this runner.

The managed/direct control paths previously still reloaded the complete event
history despite normalized persistence. They now load a durable state plus at
most the latest 1,000 events while carrying the global append cursor. On a
100,000-event in-memory contract, the bounded control load measured 3.372ms
median / 4.539ms p95 and every sample contained exactly 1,000 events; one-event
append measured 0.086/0.138ms. The first suffix implementation measured a
19.021ms append median because it scanned the complete history; this result was
rejected and the scan removed before acceptance.

The production aiosqlite filesystem run could not execute because the
restricted runner blocks its worker thread and denied local escalation. The
loopback-only `harness ui-fixture` now packages the 1,200-event,
changing-frame provider/action stream and has completed the missing effective
200% reflow run without a computer target. At a rendered 416×655 viewport, all
12 action rows and 10/10 screen previews loaded with zero horizontal overflow.
The held Teams fixture initially reproduced a stale intrinsic-height bug that
put Allow once and Deny outside the scroll range. Keeping
`content-visibility` virtualization on old messages only repaired the current
message from a 230/491 client/scroll-height mismatch to 521/521; 298 pixels of
scroll were then available for controls needing 53 pixels. The exact text,
final `ENTER`, and external-side-effect reason remained visible, the composer
send stayed disabled, and no approval was submitted. The fixture refuses the
production daemon port and opened no VNC, PiKVM, or model API.

The public headline scorecard is no longer hand-maintained. The checked
`bench/scorecard.yaml` manifest resolves measured fields from ten durable JSON
evidence rows, formats denominators and latency consistently, and embeds a
short SHA-256 for the manifest and every linked report. Both
`pikvm-agent harness scorecard --check` and pytest fail on report, digest, or
Markdown drift. The first generation pass caught two real ambiguities: the
then-current 54.1% Tesseract result belonged to the token-rejoin report rather than the older
comparison artifact, and OSWorld's seven official goal-state passes reduce to
six when the stricter harness-completed-plus-official-passed definition is
applied. The published headline uses the latter 6/33 end-to-end denominator.

The local operator now has an offline `harness support-bundle` command. It
creates a new mode-0600 JSON file and refuses overwrite. The bundle reports
runtime versions, aliased provider capabilities and readiness, route shape,
credential presence/length/distinctness, target-selection validity, bounded
storage inventory, and static UI hashes. It copies no configuration contents,
provider/model names, environment-variable names or values, endpoints, paths,
artifact names, run state, screenshots, or provider output, and performs zero
network requests. Four focused tests inject token values, a private gateway,
private model/provider labels, a target address, private paths, an artifact
name, explicit price values, and a private price-table label, then prove none
appears in the serialized bundle. The bundle exposes only each route's billing
mode, attempt limit, and whether a cost cap is enabled.

Managed provider budgets are now enforced at the provider-attempt seam. Six
focused agent tests prove that reasoner/controller/verifier calls consume the
same durable counter; fallback and schema repair cannot bypass it; metered
reservations block before invocation; missing usage commits the reservation;
and an actual cost above the reservation pauses before HID. Two configuration
tests prove that cost caps require an explicit price-table version and billing
classification for every routed provider. The UI shows attempts and configured
cost/reservation state for managed runs and says `external` for direct MCP
clients whose model calls the harness cannot observe.

Artifact-backed Office acceptance now has two target-neutral cases in
[`../bench/office-acceptance-v1.yaml`](../bench/office-acceptance-v1.yaml):
an original Shakespeare essay in Word and a deterministic quarterly-earnings
workbook in Excel. Twenty-two focused tests cover valid and failing DOCX content,
shared/inline/numeric XLSX cells and formulas, unsafe/corrupt package refusal,
portable task rendering, immutable public result semantics, formula-only
expectations, bounded continuation driving and timeout abort, and the rule that
an agent-scoped runner never approves. Each live attempt also receives a fresh
16-hex guest filename inside the lab workspace; the exact observer-returned
path must match before scoring, so a stale artifact cannot pass a later run.
Unsafe nonces fail before the VNC lifecycle starts.
Eleven Windows bootstrap tests include runtime-only artifact-path selection,
installed-observer reuse without a download or encoded payload, and
workspace traversal/quoting refusal. The observer rebuild succeeds with the
new `--file` argument when compiler cache output is redirected to the writable
temporary area.

The original `--skip-provision` contract could not coordinate with that fresh
path: it required a helper to be running with a filename that was generated
only inside the later runner call. It now means reuse the installed observer
binary. The coordinator terminates stale observer processes and restarts the
installed binary with the fresh path before starting the managed lab. The
focused Office/bootstrap suite passes 33 tests.

Artifact verification is now visible after model completion without trusting
either model or browser authority. A separate observer-only endpoint records
`pending → capturing → passed/failed`; agent and operator credentials both
receive HTTP 401. Pass requires a completed managed run, exact format, byte
count, SHA-256, and all declared semantic checks. Terminal evidence is
immutable. The run rail, transaction evidence, and verification timeline use
that state, while the public payload omits the guest path, file content, task
prose, and provider output. If the observer channel cannot publish the initial
pending state, the runner aborts the new managed run before entering its poll
or continuation loop.

This is contract evidence, not a live Office result. After the operator
explicitly authorized the disposable VNC target, temporary localhost listeners,
and guest input, the execution broker still rejected the exact isolated adapter
command before process creation. The rejection was not worked around. No local
listener, VNC socket, remote Office task, helper restart, or model call is
claimed.

Package acceptance now has a public `harness inspect-wheel` boundary. It
verifies the wheel's package identity and console entry point; requires the
managed harness, model-budget, Office runner/verifier, server, target-free
managed smoke lab,
evidence/support modules, and all three static
operator assets; checks every non-`RECORD` member's recorded SHA-256 and size;
and rejects duplicate/unsafe paths, `.env`, runtime databases, bytecode,
tests/benchmarks, excessive expansion, missing assets, and integrity
mismatches. Ten acceptance tests pass against independently assembled wheel
fixtures.

The actual Hatchling 1.31.0 backend was then loaded from the pre-existing local
cache and built a `py3-none-any` wheel without network access. The inspector
validated 132 members, 131 `RECORD` entries, 1,525,429 uncompressed bytes, the
client-isolation audit module, all three operator assets, and the
`pikvm_agent.cli:app` entry point; wheel SHA-256 is
`7252debceaad7ff3ec499712e6439e1c590d27ca0effd2cec029d2524bbde96f`.
The wheel itself installed into a fresh venv and its package/version resolved
from that venv. Its generated console script also rendered help while reusing
the already-installed dependency layer, with `pikvm_agent.cli` proven to load
from the wheel location.

That cited wheel predates the managed smoke-lab module. The current inspector
now requires that module and the isolated task launcher. A fresh offline build
was attempted but stopped at the runner's read-only `uv` cache boundary; the
narrowly scoped elevated attempt was then broker-rejected before process
creation. Rebuild and reinspection remain pending, and the older artifact is
not claimed as the current package.

This is not yet a dependency-complete clean-install pass. The fully isolated
offline resolver stopped because the local cache lacks several locked compiled
wheels, including `cffi==2.0.0`; no network download was attempted. Evidence:
[`../bench/results/2026-07-26/package/wheel-build-and-isolated-install.json`](../bench/results/2026-07-26/package/wheel-build-and-isolated-install.json).

Codex JSONL usage extraction was then added and checked against the real CLI.
A schema-valid read-only probe reported 19,177 input, zero cached-input, 84
output, and zero reasoning-output tokens in 8.333 seconds. Public benchmark
schema v4 now preserves per-stage usage and numeric totals. Earlier reports
remain immutable and do not claim token or cost data they did not capture.
A one-case live ScreenSpot path then retained 22,264 input, 17,152
cached-input, 258 output, and 225 reasoning-output tokens in 8.603 seconds.
That one case validates instrumentation only.

## Live Windows VNC trials

All actions below crossed the real MCP stdio boundary and the local
PiKVM-shaped VNC adapter.

### Prose

- Expected: 581 characters.
- Execution: three bounded MCP typing bursts.
- Burst outcome: all completed.
- OCR read-back: 580 characters and the expected prefix.
- Interpretation: useful visual/OCR evidence, but not an exact-character pass
  without the observer oracle.

### Code and keyboard layout

The first code trial exposed a VNC translation bug: the adapter converted a
physical shifted key chord to semantic US punctuation before the UK-layout
guest received it. The adapter now preserves the physical chord and delegates
the final character to the guest layout.

After the fix:

- Expected: 142 characters.
- OCR read-back: 142 characters.
- Visual inspection: the code appeared correct.
- Exact OCR equality: false because OCR confused punctuation, quotes, and some
  letters.
- Runtime outcome: `type_unverified` / `unverified_ambiguous`.
- Safety outcome: the runtime stopped before commit and did not clear or retype
  the visually plausible field.

This is the intended fail-closed result. Precise text self-corrects only when
the verifier detects a strong layout signature; ordinary OCR disagreement is
not permission to erase code.

### Provider-neutral task run

A durable Codex CLI `account-default` route was exercised as reasoner,
controller, and verifier against the isolated Windows VM. This was a diagnostic
run, not a passing benchmark:

| Signal | Observed |
| --- | ---: |
| Successful structured model calls | 28 |
| Controller median / p95 latency | 7.2 s / 11.7 s |
| Reasoner median / p95 latency | 8.8 s / 9.6 s |
| Verifier median / p95 latency | 11.0 s / 15.7 s |
| Checkpointed / completed actions | 10 / 8 |
| Recoverable typing failures | 2 |
| Repeated action stopped before HID | 1 |

The run exposed and now has regressions for strict-schema normalization,
contradictory structured-output repair, model-block recovery, verifier-failure
replanning, VNC video-lag retry, case-only Caps Lock diagnosis, preservation of
pre-existing text during correction, and exact repeated-action refusal.

The run did not reach its completion criterion before the action budget. The
single-model route repeatedly spent turns proving focus, and the pixel watcher
initially missed text that was visibly present in the resulting frame. It must
not be used as a product success claim. It does demonstrate why the product
needs separate fast grounding/control and independent verification lanes.

## Live operator-console audit

The authenticated console was exercised through a real browser against the
isolated fake-machine daemon. The visible run showed the selected provider
(`codex-ui-audit`) and model (`gpt-5.6-sol`), the reasoner plan, a live
1,280×720 frame with world/control metadata, the exact bounded pointer action
and its idempotency key, the before-HID checkpoint, provider start/completion
events, and verifier evidence. No production PiKVM endpoint was configured or
called.

The first visual trial deliberately pressed Stop while Continue was in flight.
The stop itself worked and the run became `aborted`, but the cancelled Continue
request was incorrectly surfaced as HTTP 500. The same audit also found that
the launcher required a 32-character access token while the API and browser
accepted 16 characters.

Both defects now have regression tests. The API, launcher, and browser use one
32-character minimum, and a cancelled continuation returns the latest durable
run snapshot instead of escaping through Starlette middleware. A post-fix live
HTTP race returned 200 for both Continue and Stop, retained the aborted run, and
produced no server traceback. The complete suite then passed 450 tests with one
opt-in benchmark skipped. The durable audit is
[`../bench/results/2026-07-25/ui/live-operator-audit.json`](../bench/results/2026-07-25/ui/live-operator-audit.json).

This proves the web stop path works under the tested race. The separate
out-of-band emergency-stop contract is covered below.

## Independent emergency-stop audit

The raw MCP server and emergency CLI no longer guess the conventional
production daemon port. With `PIKVM_AGENT_DAEMON` absent, each exits before
starting a server or issuing a network request. Invalid transports and daemon
URLs containing embedded credentials, query parameters, or fragments are also
refused. A selected daemon is no longer sufficient for public raw MCP:
startup also requires the operator visibility boundary, and protocol dispatch
independently fails closed before the tool body if that boundary is absent.

With no operator web process running, an isolated live trial explicitly
selected the fake-machine daemon and halted its one active session. The CLI
received `quiesced=true`, zero in-flight actions, named the safe target alias
and fingerprint, and completed in 0.34 seconds. A read-only follow-up found the
session latched `failed` with `error=panic_stop` and control epoch 1. The daemon
then shut down cleanly. A deterministic non-quiesced response exits non-zero
and never prints confirmation.

This closes the operator-web-independent brake contract at the daemon control
plane. It does not replace a hardware relay or PiKVM-native kill channel if the
selected daemon itself is unreachable. Machine-readable evidence:
[`../bench/results/2026-07-25/safety/emergency-stop-audit.json`](../bench/results/2026-07-25/safety/emergency-stop-audit.json).

### Provider fallback and dangerous-action approval

A separate seeded 1,000-case one-shot permission benchmark now covers 800
dangerous and 200 safe controls, including Microsoft Teams message sends,
Outlook email/Reply all, meeting responses and cancellations, channel posts,
uploads, account/permission changes, purchases, deletes, local file commits,
and security-disable actions. Each generated label receives up to one
OCR-shaped mutation. The current classifier caught 800/800 dangerous cases,
allowed 200/200 safe controls, and produced zero category errors in the
combined 111-test safety selection (1.78 seconds). This is target-free
classifier evidence, not a live message-send trial. Machine-readable evidence:
[`../bench/results/2026-07-26/safety/dangerous-action-gate.json`](../bench/results/2026-07-26/safety/dangerous-action-gate.json).

The later checkpoint review found one shortcut outside that label corpus:
Outlook's `Alt+S` send path. It now enters the `communication_send` hold before
HID. The same review removed automatic stale-action rebasing, made a fresh
controller decision mandatory after a changed world, required world/control
versions and a caller-stable idempotency key on every model-facing HID tool,
and limited target-local pointer freshness to the explicit
`isolated_benchmark` profile plus a separate lab-app construction capability.
One non-overlapping target-free selection passed 206/206 permission, harness,
MCP schema/preflight, visibility, lab-construction, payload, history, editor,
and burst tests in 4.61 seconds. Two full `Runtime` integration cases for
missing freshness and a blank key remain uncounted because this restricted
runner stalls that fixture; they must run in the clean-install suite.

An intentionally failing subprocess provider was placed before the real Codex
CLI OAuth route for all three roles. The primary produced three sanitized
failures; `codex-fallback` / `gpt-5.6-sol` then succeeded for reasoner,
controller, and verifier in 6.146s, 4.840s, and 9.652s. Each transition carried
the role, route index, provider, error class, selected model, and latency. The
provider fallback passed. The fake-machine task paused as uncertain because its
synthetic frame did not render a cursor, so the task itself is not a success
claim.

A deterministic policy probe then proposed side-effecting communication text.
The daemon classified it as `communication_send`; both layers reported
`needs_approval`; and a decision missing the exact intent-bound approval ID was
refused with HTTP 409. The exact human rejection produced no completed action,
bumped the computer control epoch, and latched the underlying session failed.

The first trial exposed that daemon polling did not persist the direct-approval
state and that a rejected harness run left its computer session live. Both
lifecycle defects were reproduced in red tests and fixed. The machine-readable
record is
[`../bench/results/2026-07-25/ui/live-routing-and-approval-audit.json`](../bench/results/2026-07-25/ui/live-routing-and-approval-audit.json).

## Direct MCP visibility and authority audit

The ordinary PiKVM MCP was instrumented at its real `call_tool` boundary, not
inside the autonomous operator graph. An authenticated preflight records the
external caller, actual tool, exact non-secret arguments, and current
operator-gate state before the tool body can execute. Completion attaches
daemon freshness, policy evidence, measured latency, and completion/refusal
status to the same durable run. The visible serializer removes internal frame
paths and the daemon's unbounded raw result payload; operators retain the
structured outcome and fetch frame bytes only through the authenticated
no-store endpoint.

The integration contracts cover before-HID visibility,
secret-text redaction, pause/read-only behavior, model self-approval refusal,
ID-bound UI approval, latched stop, guarded fail-closed behavior, and
observe-only perception/emergency-stop availability while action tools remain
fail-closed. They also prove that a completed tool call does not
close its parent session and that result telemetry survives a coordinator
restart. Missing visibility now refuses both public startup and actual tool
dispatch; only the harness-owned child explicitly uses the private
already-audited path. A real browser then exercised a direct
`claude-code` / `anthropic-oauth` / `opus-4.8` run, pause/resume, and a
synthetic Teams-send approval. The send was rejected and its audit trail
remained visible. No PiKVM or VNC target was contacted.

At a requested 390×844 viewport, the browser reported identical 375-pixel
client and scroll widths, so there was no horizontal overflow. Live screen,
transaction, and timeline regions were each 360 pixels wide. The page emitted
zero console errors.

The 100-call local SQLite + in-process ASGI microbenchmark measured 6.696 ms
median and 7.538 ms p95 guarded-call latency, or 6.682 ms median incremental
overhead over a deterministic FastMCP no-op. It excludes socket, model, frame,
OCR, HID, and remote-machine latency. Full evidence is in
[`../bench/results/2026-07-25/ui/direct-mcp-visibility-audit.json`](../bench/results/2026-07-25/ui/direct-mcp-visibility-audit.json).

Generated managed-client configuration now covers Codex, Claude, Gemini, and
OpenCode. The current focused selection passes 65 local configuration, scope,
stdio, API-propagation, durable-caller, outage/recovery, error-redaction,
package, and UI-budget contracts in 6.96 seconds.
The client-isolation audit adds 21 target-free contracts. It accepts native
Codex resolved inventory, Codex TOML and project shared MCP JSON,
Claude/Gemini JSON, and legacy/V2 OpenCode JSON/JSONC; recognizes only the
official Python-module and installed-console launcher shapes; ignores
explicitly disabled registrations; merges explicit documents from low to high
precedence by server name; and fails for active raw, guarded-direct,
distinct-duplicate, ambiguous, missing, or invalid PiKVM registrations. The
Codex inventory child uses exact arguments, empty stdin, a ten-second timeout,
a 1 MiB output ceiling, and a fixed cross-platform runtime-path environment
allowlist that excludes API keys, tokens, and credentials. Its raw stdout/stderr
never enters the optional owner-only no-overwrite report, which also contains
no source path, command, argument, environment value, parse input, or secret.
Native read-only Codex inventory failed because it contains one raw PiKVM
registration and no managed registration. The read-only current Claude
user→project file audit passed with one managed registration: the project
definition overrides the same-named user definition under documented scope
precedence. The known Gemini user/project file scopes failed with no managed
PiKVM registration; the known OpenCode global/project file scopes failed with
one raw registration and no managed registration. No live config was changed.
Codex now has native effective enumeration; safe native enumeration for the
other clients and install integration remain open.

The current installed-client dry-run then reproduced the adoption failure:
Codex CLI 0.144.4 resolved one raw PiKVM registration and no managed one.
The new isolated launcher supplied a session-only managed override and reran
Codex's native effective inventory; it resolved exactly one managed PiKVM
surface, retained unrelated MCP servers, and changed no persisted
configuration. Claude Code 2.1.220 exposed both `--mcp-config` and
`--strict-mcp-config`; its isolated plan resolved one explicit managed surface
and ignored ambient MCP registrations. OpenCode 1.14.44 then executed its
native resolved-config probe under `--pure` with an ephemeral writable home,
client-owned OAuth linked without copying it, exact default-deny permissions,
and no ambient secret forwarding. It resolved one managed PiKVM surface and no
competitor. All three native dry-runs passed. Gemini CLI 0.35.3 then ran its
installed native
effective-settings loader from an empty dedicated profile and clean workspace.
It resolved one system-defined, system-allowlisted managed surface; the
launcher also verified the exact supplemental default-deny policy path/content
and `--extensions none` argument. This was a settings-only audit: no
authenticated profile, model, extension, tool, or policy decision executed.
The expanded focused launcher, audit, and wheel-package slice passed 54/54
contracts. No coding-client task, MCP server, model, harness socket, or
computer target was opened.
Evidence:
[`../bench/results/2026-07-26/safety/isolated-managed-client-launch.json`](../bench/results/2026-07-26/safety/isolated-managed-client-launch.json).

A target-free managed smoke lab now closes the previous manual acceptance
setup gap. Its ASGI run traversed `AgentHarness`, the production operator app,
all three deterministic model lanes, one bounded action, checkpoint frame,
labelled before/after verification, provider health, and originating
`codex-cli` identity. The interface test used the in-memory store because
SQLite worker threads do not complete in this runner; the CLI construction
test separately proved the launchable app selects `SqliteRunStore`, but did not
execute a SQLite-backed request.

`client-task` adds non-interactive Codex/Claude/Gemini/OpenCode task commands that
retain the same isolation audit, read private task text from stdin, avoid
dangerous bypass flags, and expose only task length/digest plus execution
metadata. Gemini additionally requires the dedicated profile and uses
stream-JSON, its managed MCP allow-list, `--extensions none`, default approval
mode, and the supplemental policy; that task route is contract-tested but has
not executed against a logged-in profile. The isolated
launcher/audit/wheel slice passes 54/54 contracts. The previously recorded
smoke-lab slice remains 24/24 and its focused selection 44/44, including a
public-claim integrity check
that requires contract evidence to remain distinct from live execution and a
scorecard-drift check that now interpolates status fields from the cited JSON.
An exact
loopback-only server launch was attempted, but the host execution broker
rejected it before process creation. No listener, outer coding client, MCP
process, OAuth/API provider, computer target, or production registration was
contacted. The deterministic inner provider did execute three successful
reasoner/controller/verifier calls in the ASGI contract. Evidence:
[`../bench/results/2026-07-26/harness/managed-smoke-lab-contract.json`](../bench/results/2026-07-26/harness/managed-smoke-lab-contract.json).

A repository-wide test run was also attempted, but this restricted runner
cannot complete thread-backed boundaries: the temporary SQLite `runtime`
fixture stalled before its test body, and a second partition reached
Starlette's synchronous `TestClient` portal thread and stalled there. No test
failure was observed before either boundary, but interrupted progress is not
reported as a pass count. The 44-test focused selection uses an in-memory ASGI
transport where required and completed normally.

The previously cited wheel now fails the current inspector because it predates
`pikvm_agent/harness/managed_client_launcher.py` and
`pikvm_agent/harness/smoke_lab.py`. A fresh offline build was attempted: the
sandboxed invocation stopped before the build backend because the existing
`uv` cache is read-only, and the narrowly scoped escalated invocation was
rejected by the execution broker before process creation. No network access
was used and no current wheel pass is claimed.

Twelve real MCP SDK stdio cases are defined. Seven execute here: all four exact
generated-command inventories, high-level initialization, a redacted outage
tool call that leaves the same process responsive, and private raw-child
initialization. The product now waits directly on POSIX descriptors while
retaining SDK message validation and sessions, avoiding the blocked
worker-backed stdin wrapper. The remaining four authenticated task/create/
status/outage/restart cases and the product-runner case are skipped because
this restricted runner forbids their loopback HTTP harness. They are not
presented as passes. The generated launchers carry only the scoped agent secret
name plus a validated static client label; they carry no daemon target,
operator token, or observer token. The `harness client-acceptance` command runs
that target-free synthetic matrix, emits a failure-inclusive mode-`0600`
no-overwrite report, and exits nonzero when a client fails; its complete task
and restart path remains unexecuted in this runner.
Evidence is in
[`../bench/results/2026-07-25/safety/managed-client-launch-2026-07-26.json`](../bench/results/2026-07-25/safety/managed-client-launch-2026-07-26.json).
The current failure-inclusive explicit-scope results are
[Codex](../bench/results/2026-07-26/safety/codex-client-config-isolation.json),
[Claude](../bench/results/2026-07-26/safety/claude-client-config-isolation.json),
[Gemini](../bench/results/2026-07-26/safety/gemini-client-config-isolation.json),
and
[OpenCode](../bench/results/2026-07-26/safety/opencode-client-config-isolation.json).
Post-change regression groups also passed 59 agent/store, 29 authenticated API,
4 static UI, 97 direct-visibility/policy/stop, 43 media/routing/conformance/
config, and 74 provider/onboarding/lab/Office cases. The direct-burst runtime
aggregate reached the known restricted-runner thread hang and is not counted.

## ScreenSpot-Pro 100-case grounding run

The pinned ScreenSpot-Pro corpus was shuffled with seed 104729 and the first
100 cases were evaluated with Codex CLI `gpt-5.6-sol`, four concurrent calls,
no verifier pass, and the official box-hit rule:

| Metric | Result |
| --- | ---: |
| Correct | 73/100, 73.0% |
| Wilson 95% interval | 63.6–80.7% |
| Text / icon | 52/68 (76.5%) / 21/32 (65.6%) |
| Median / p95 model latency | 7.634 s / 13.293 s |
| Maximum model latency | 50.186 s |
| Model-active / wall time | 860.237 s / 218.527 s |
| Effective concurrency / throughput | 3.94 / 0.458 cases/s |
| Provider errors | 0 |

The first 20 cases scored 16/20; the next 80 scored 57/80. This is materially
below the earlier 34/40 aggregate and demonstrates why the scorecard forbids a
small one-shot sample as a product claim. Twenty-six failures were target
misses and one was a `negative` target-absent response. Token usage was not
captured by this schema-v3 run; schema v4 instrumentation was added afterward.

The compact artifact preserves all 100 case IDs, official boxes, predictions,
hit/miss results, latency, and failure classifications:
[`../bench/results/2026-07-25/screenspot-pro/codex-gpt-5.6-sol-seed104729-n100.json`](../bench/results/2026-07-25/screenspot-pro/codex-gpt-5.6-sol-seed104729-n100.json).
No computer-control target was contacted.

## OSWorld-Verified live task runs

The official OSWorld repository was pinned at
`b7db4d8c85d9e95e0b1db44de5bec954cf37f0cf`. Nine tasks were scored in resettable
official Ubuntu VMs through harness → MCP → isolated daemon, with setup and
official evaluation kept outside the model boundary. Six reached both
`harness_status=completed` and official score `1.0`; inactive-screen dimming,
conda repair, and subtitle extraction scored `0.0`. A tenth compatible task
was exercised but remained unscored.

| Task | Actions completed / attempted | Model-active | Wall | Result |
| --- | ---: | ---: | ---: | --- |
| Enable Do Not Disturb | 2/2 | 40.57 s | 47.15 s | official `1.0`, completed |
| Set volume to maximum | 2/2 | 33.73 s | 40.16 s | official `1.0`, completed |
| Enable automatic screen lock | 9/10 | 295.30 s | 369.63 s | official `1.0`, completed |
| Rename `todo_list_Jan_1` to `todo_list_Jan_2` | 5/5 | 98.29 s | 128.56 s | official `1.0`, completed |
| Disable inactive-screen dimming | 5/7 | 147.87 s | 181.05 s | official `0.0`, blocked |
| Set timezone to UTC+0 | 18/23 | 491.19 s | 602.09 s | official `1.0`, completed |
| Restore deleted poster from Trash | 3/3 | 66.68 s | 82.01 s | official `1.0`, completed |
| Repair conda environment | 2/3 | 54.59 s | 61.44 s | official `0.0`, approval required |
| Extract and remove video subtitles | 13/19 | 833.08 s | 1,071.89 s | official `0.0`, blocked |

The current nine-task diagnostic is 6/9, with a Wilson 95% interval of
35.4–87.9%. It consumed 2,061.32 seconds of summed model-active time and
2,583.98 seconds wall time; end-to-end median/p95 were 128.56/883.97 seconds.
This is a tracer, not a representative score over the discovered 369 tasks.

All attempts remain in the denominator history. Thirty-three reached the official
evaluator: 7/33 achieved the official goal state, while 6/33 also had a
truthful harness-completed state. Eleven additional attempts ended before
evaluation and remain explicit unscored engineering failures.

The live loop exposed and fixed typing-budget truncation, fuzzy verification
of exact fields, provider child-process leakage after timeout, and repeated
paused-state resumption without action progress. The inactive-screen-dimming
task remains an honest failure: the visible settings page did not expose the
requested control, and the controller blocked rather than changing Screen
Blank or Automatic Suspend. The UTC run passed, but its 602-second latency and
five recoverable typing failures are release-blocking efficiency debt.
The first restore-from-Trash attempt also exposed late compatibility checking:
the tracer booted a VM before rejecting the official `download` setup. Setup
and evaluator shapes now preflight before startup, official HTTP(S) task files
are uploaded outside the model boundary, and the corrected task passed 3/3
actions in 82.01 seconds.

The machine-readable denominator and links to every current report are in
[`../bench/results/2026-07-25/osworld/summary.json`](../bench/results/2026-07-25/osworld/summary.json).
The selected all-Codex, all-Claude, and mixed-role subtitle attempts are in
[`../bench/results/2026-07-25/osworld/model-comparison.json`](../bench/results/2026-07-25/osworld/model-comparison.json);
none passed. One mixed run cut controller median latency to 8.31 seconds but
timed out, and only 11 of its 16 raw completed actions were progress-bearing.
The next reduced observation-only work but exposed newline-in-text and
text-plus-Enter output. The fixed-code run stopped those shapes before HID,
then blocked when the controller exhausted its bounded repairs. R14 and R15
retained moving-frame and provider-outage failures. R16 exercised three real
Claude controller fallbacks after Codex failures, found and probed the target
video, caught a four-character truncation before Enter, and stopped at an exact
terminal-mutation approval request. R17 and R18 retained fail-closed moving-video
world-version failures and exposed the controller's `META` alias plus a
two-key `Escape`/`META` sequence. The guarded stale rebase now regression-covers
those non-committing shortcuts without allowing Enter, clicks, editing
shortcuts, or secret text. R19 closed the video, live-proved pointer no-op
rejection and normalized safe-draft evidence, found and probed the target, then
reached the same approval boundary in 745.72 seconds. Its one late-glyph false
negative led to a second bounded read-only exact-text reread. None of R16–R19
passed the official evaluator. R20 added an operator-resumable approval path
and live-proved three same-idempotency stale retries, but never reached an
approval: ambiguous terminal OCR, a wrong-window double-click, repeated focus
actions, and a duplicate pointer-activation burst consumed 1,071.89 seconds
before stagnation stopped the run. Duplicate pointer activations within one
burst are now rejected before checkpoint or HID; the follow-up live run is
blocked by the current local Docker/sandbox approval state.
The production PiKVM target was not configured or contacted.

Preflight coverage was subsequently widened against the pinned upstream setup
controller. Official `launch`, `open`, `activate_window`, and `command` setup
records now stay in the outer coordinator, and omitted exact-match-list
conjunctions follow upstream's default `and` semantics. This makes 10 of 369
tasks tracer-compatible instead of 7. All three newly compatible tasks were
then exercised: conda repair and subtitle extraction reached the evaluator and
scored `0.0`; desktop organization was cancelled after 1,232.58 seconds and
remains unscored. Live-run preparation also caught that official evaluator
`postconfig` actions were never validated or applied. A failing contract
reproduced the defect; the coordinator now applies them before scoring and
outside the model/MCP boundary. All 30 public-desktop contracts pass. See
[`../bench/results/2026-07-25/osworld/compatibility-expansion.json`](../bench/results/2026-07-25/osworld/compatibility-expansion.json).

## Blind OCR benchmark

The committed runner generates exactly 1,000 unique seeded cases, 100 in each
of ten categories. Evaluation order uses a separate seed, image filenames are
opaque, and the private ground truth is not written until all OCR calls have
finished.

The current Latin-font Tesseract ensemble with pixel-proven title-bar
filtering and pixel/token fragment rejoining produced:

| Metric | Result |
| --- | ---: |
| Cases | 1,000 |
| Exact match | 44.9% |
| Whitespace/Unicode-normalized exact match | 56.9% |
| Expected-aware exact candidate (normal two-read profile) | 61.4% |
| Mean character error rate | 2.08% |
| Median OCR latency | 156 ms |
| p95 OCR latency | 215 ms |
| Evaluation wall time | 40.20 s |
| Throughput (four workers) | 24.9 images/s |
| Prose normalized exact | 83% |
| Identifier normalized exact | 76% |
| UI-label normalized exact | 90% |
| Numeric / punctuation normalized exact | 0% / 0% |

The prior 20.2%-exact / 16.1%-CER result is superseded: 450 generated cases
included expected suffixes clipped outside the image. A regression now requires
all scored text to fit. The corrected single-candidate baseline was 31.2%
normalized exact and 4.67% CER. Selecting between independent raw and enhanced
Tesseract reads without ground-truth access raised that to 37.3% and 4.04%,
with about 40ms additional median latency. A paired diagnosis then found
coloured window controls being recognized as short leading text. Removing a
line only when the source pixels prove three similarly sized coloured
title-bar blobs added 67 normalized-exact cases with no paired regressions,
raising the score to 44.0% and reducing CER to 2.83%.

A subsequent paired failure analysis found Tesseract inventing spaces inside
URLs, paths, hashes, and terminal tokens. Rejoining only where pixel gaps and
existing machine-token syntax agree raised the untouched 1,000-case result to
54.1% and 2.27% CER. It added 101 normalized-exact cases with no paired
regressions. Paths reached 64%, URLs 50%, terminals 75%, and identifiers 68%.

The current selector rejects impossible structured syntax before trusting
confidence. Against a paired confidence-only baseline it added 16 exact cases
with zero regressions, reaching 56.9%. The provider also retains both blind
reads. When precise intended text of at least eight characters is already
known, accepting only an exactly matching candidate raises verification
coverage to 61.4%; nearest guesses remain forbidden.

A fresh full-corpus run then tested a third independent 1.5× prepared read.
Keeping the existing selector unchanged produced the same 569/1,000 selected
transcripts and raised exact known-intent candidate coverage from 614 to 645
cases. That precision profile measured 225ms median, 321.05ms p95, 58.445
seconds wall, and 17.11 images/s, versus 169ms, 240.05ms, 43.735 seconds, and
22.86 images/s for the normal two-read profile on the same host. The extra
read is therefore dispatched only for precise known-intent read-back; general
screen OCR retains the normal latency budget. Numeric and punctuation remain
0/100 and the release gate still fails. Evidence:
[`../bench/results/2026-07-26/ocr/tesseract-precise-multiscale-n1000.json`](../bench/results/2026-07-26/ocr/tesseract-precise-multiscale-n1000.json).

A deterministic failure diagnosis then reproduced the numeric/punctuation
zeroes twice and varied only page segmentation, candidate scale, render
degradation, and font. PSM 7 and raw-line PSM 13 were worse, no raw/2×/1.5×
candidate was exact on any of the 200 cases, and none of the five completely
clean PNG cases was exact. The cause is visible in the corpus contract: every
numeric case contains `0O1Il|`, and every punctuation case contains dense
ASCII quote, backtick, and operator distinctions. Report schema v4 therefore
labels those cases as an adversarial `stress` tier while retaining them in the
overall score and every existing category gate.

The subsequent blind 1,000-case rerun produced 71.125% normalized exact and
0.885% CER on 800 routine cases, versus 0% exact and 6.849% CER on the 200
stress cases. Overall accuracy remained 56.9%, all 12 release failures
remained active, and the tier adds a 10% stress-CER ceiling. It cannot weaken
the gate or authorize OCR-only verification. The rerun took 48.454 seconds at
188ms median / 258ms p95 in the restricted runner. Evidence:
[`../bench/results/2026-07-26/ocr/tesseract-tiered-diagnosis-n1000.json`](../bench/results/2026-07-26/ocr/tesseract-tiered-diagnosis-n1000.json).

The runtime factory had still forced legacy PSM 3 despite the benchmark using
PSM 6, so the published score did not describe the default product path. A
failing factory regression reproduced that drift. The default and shipped
example now instantiate the measured PSM 6, 2× raw/preprocessed ensemble with
syntax-aware selection, while retaining explicit configuration controls. The
focused configuration/provider suite passes 11 tests.

Confidence is not an acceptance oracle: 23 of the 236 cases with mean
Tesseract confidence at or above 0.90 were still wrong. This remains a failing
release gate and does not authorize exact code, numeric, URL, punctuation, or
irreversible action verification from OCR alone.

PaddleOCR PP-OCRv6 medium CPU was run on the exact same 1,000 cases. It reached
78.9% normalized exact and 1.06% CER, at 874ms median, 2.54s p95, and
1,078.82 seconds wall time. Numeric confusables still scored 0/100 and
punctuation only 51/100. Paired with the current Tesseract output, both engines
were correct on 539 cases, only Tesseract on 30, only Paddle on 250, and neither
on 181. Their 551 identical reads include 12 identical errors, giving a 96.23%
Wilson lower bound—still unsafe as commit authority.

The paired outputs also support a narrower known-intent analysis. Retaining the
Tesseract precise candidates and independent Paddle result gives an exact
candidate on 827/1,000 cases: 776/800 routine (97.0%) and 51/200 stress
(25.5%). This consults the hidden expected string only after both OCR calls and
requires exact equality; it never selects the nearest guess. An opt-in hybrid
provider now implements that evidence shape while keeping ordinary screen OCR
on Tesseract and bounding the Paddle wait. The 827 result is reconstructed from
two completed paired reports, not a fresh hybrid runtime run.

The first runtime hybrid probe exposed a lifecycle defect: Paddle ran through
`asyncio.to_thread`, all five cases scored in 25.109 seconds, and the process
then remained stuck until its 180-second hard timeout. A non-cancelled
single-inference control also failed to return within 120 seconds in this
runner. Paddle now lives in one persistent, killable subprocess shared by both
runtime OCR consumers. At most one shielded native request exists; caller
timeouts cannot create an overlapping executor backlog, and runtime/benchmark
shutdown closes the worker. The same five-case lifecycle probe then used four
parallel primary workers, scored in 5.068 seconds, and exited normally under a
90-second boundary. Nineteen focused contracts cover request framing, exact
path/region forwarding, redacted worker failures, one-inflight behavior, busy
skip, real subprocess termination, provider reuse, diagnostics, and runtime
cleanup. Its provider denominator records five precise calls, four secondary
attempts, one completion, three timeouts, and one busy skip. This is a lifecycle
diagnostic, not an accuracy sample; the fresh 1,000-case hybrid run remains
outstanding. It still fails the release gate and cannot authorize a commit.
Evidence:
[`../bench/results/2026-07-26/ocr/hybrid-known-intent-candidate-union-n1000.json`](../bench/results/2026-07-26/ocr/hybrid-known-intent-candidate-union-n1000.json)
and
[`../bench/results/2026-07-26/ocr/hybrid-worker-shutdown-smoke-2026-07-27.json`](../bench/results/2026-07-26/ocr/hybrid-worker-shutdown-smoke-2026-07-27.json).

The Paddle field-verification path also reproduced a contract defect: it
accepted a crop region but sent the full screenshot to inference. The adapter
now creates a temporary region-only image and cleans it after success or
failure; three regression tests pass. A live field-crop run remains pending.
The paired report, environment versions, confidence coverage, and defect
ledger are recorded in
[`../bench/results/2026-07-25/ocr/ocr-seed104729-n1000-comparison.json`](../bench/results/2026-07-25/ocr/ocr-seed104729-n1000-comparison.json).
The current Tesseract selector/candidate evidence is
[`../bench/results/2026-07-25/ocr/tesseract-structured-candidates-seed104729-n1000.json`](../bench/results/2026-07-25/ocr/tesseract-structured-candidates-seed104729-n1000.json).

## Exact Windows observer

The observer was rebuilt as a stripped 64-bit Windows GUI executable, uploaded
to the LAB-ONLY prerelease, downloaded to the disposable VM, and verified on
Windows against SHA-256
`b6a19566f3d4530fc36930241c1c7793ff9f25de39ad96e7823ff3523f4e27f4`.
No executable bytes or Base64 were typed through HID.

The third visual protocol revision fixed a repeatable physical-screen error:
one matrix cell always lost the same bit and invalidated every CRC. Payload
bytes are now triplicated and majority-decoded before the end-to-end CRC check.
The compact screenshot record retains full event/key counts, caps the diagnostic
key sample at 128 entries, and labels truncation. The live run decoded 35 pages
across seven trials; one missing-border capture recovered through a bounded
republish.

The post-fix deterministic MCP run produced:

| Trial | Independent result | Elapsed |
| --- | --- | ---: |
| 581-character prose | 581/581 exact; OCR 1 normalized error | 61.360 s |
| 142-character code | 142/142 exact; OCR 6 errors; commit blocked | 79.562 s |
| Duplicate retry | exact once; idempotent replay | 24.555 s |
| OCR-grounded click | completed after one bounded republish | 21.745 s |
| Dangerous Send | approval required; zero dangerous commits | 15.923 s |
| Notepad clean-state rerun | 65/65 file bytes; matching hash; `Notepad.exe` | 67.756 s |
| VS Code overlap rerun | `Code.exe`; OCR ambiguous; save not attempted | 83.429 s |

The coherent seven-trial attempt consumed 336.165 seconds of measured trial
time (39.853-second median, 89.089-second p95) and failed its release gate.
Input transport, idempotency, and the tested communication guard passed. OCR
and the full editor matrix did not.

Repeated editor attempts also proved that an unreset desktop is not a valid
release denominator: stale Notepad/VS Code buffers produced a real newer-file
conflict. An attempted `taskkill` cleanup required approval and then stopped
before Enter when its exact command text could not be verified. No process was
killed, and the shortcut was removed. Release-evidence attempts now require a
VM snapshot reset instead.

The machine-readable result is
[`../bench/results/2026-07-25/windows/live-vnc-observer-iteration.json`](../bench/results/2026-07-25/windows/live-vnc-observer-iteration.json).
It deliberately omits the VNC endpoint.

### Exact environment-identity iteration

The observer now also captures the Windows guest, session, input desktop,
foreground process, focused-control class/id, and whether that focus belongs
to the foreground window. It hashes the Windows machine identity inside the VM;
the receiver accepts only `guest:<16 lowercase hex>` and forbids unknown
snapshot fields, so a raw computer name cannot silently enter the report.

Three live attempts failed because the old `observer-v4.exe` retained the
global snapshot hotkeys despite two PowerShell cleanup strategies. Exact
LAB-only image-name cleanup fixed ownership. The v5 identity probe then passed
through MCP/VNC in four pages and 20.698 seconds. Compact visual-only field keys
reduced v6 to two pages and 13.625 seconds while expanding into the same strict
schema: 50% fewer pages and 34.2% less end-to-end time.

This independently proves the Windows guest and input destination in the
instrumented test VM. It does not claim that production machines have the
helper, nor that a nested remote desktop inside the observed guest is the same
identity. Evidence:
[`../bench/results/2026-07-25/windows/observer-environment-identity-probe.json`](../bench/results/2026-07-25/windows/observer-environment-identity-probe.json).

## Automated verification

- Live-frame resource checkpoint: six target-free contracts pass. Preview
  bodies are streamed and aborted above 4 MiB; media type and declared
  dimensions are validated; concurrent requests for one session share one
  upstream fetch; and both payload cache and lock registry stay within an
  eight-session LRU (32 MiB worst-case cached payload). This is a transport
  envelope, not a browser-decode or live-machine result.
- Current combined typing, burst, verification, direct-permission, seeded
  dangerous-control, historical coverage/provenance, scorecard, public
  evidence, harness-scoring, live-frame, and MCP payload-preflight selection:
  204 passed in 10.37
  seconds. This
  selection has no VNC, PiKVM, provider, Office, email, Teams, or other external
  client.
- Current historical typing-safety checkpoint: all 31 watched-typing tests and
  all 44 burst/verification tests pass. The new cases first reproduced normal
  and fast-printer input continuing after an out-of-field screen change, then
  prove zero later chunks, exact partial progress, held-input release, no
  whole-prose replay after OCR disagreement, and refusal to run Enter after
  ambiguous prose. They also prove `method=print` cannot bypass a present
  watched typer and `no_verify` is rejected before HID. The test fixture
  executes deterministic image-grid work inline because this restricted
  runner's worker-thread path does not return.
- Raw-HID payload-shape checkpoint: a seeded 1,000-case corpus passes with
  800/800 unsafe cases refused before HID and 200/200 ordinary controls
  allowed. It balances command-shaped dense Base64 transfer, the same transfer
  split across contiguous type actions, PowerShell `EncodedCommand`, heredoc
  openers, long nested shell commands, prose, short shell commands, plain-text
  append, displayed digests, and ordinary code. A separate public-MCP
  integration test proves refusal before daemon contact. This is a local
  syntax gate, not an explicit byte-transfer channel or remote file equality
  result.
- Read-only media-builder checkpoint: 10 functional contracts pass, plus one
  checked-report contract. Two source files and the canonical SHA-256 manifest
  are extracted byte-exactly from the generated ISO; output is mode `0600` and
  installed without overwrite. Six unsafe or Windows-ambiguous guest names,
  three budget failure classes, an existing image, and subprocess
  cancellation are fail-closed. This is target-free builder evidence only:
  there is no PiKVM upload, attach, detach, or guest result.
- Virtual-media transaction checkpoint: 19/19 target-free transaction,
  operator-API, daemon-adapter, and UI contracts pass. They require the exact
  browser approval ID and intent, reject the agent credential, checkpoint every
  intended transition, distinguish clean pre-upload refusal from ambiguous
  mutation, roll back only proven-owned state after a definite failure, latch
  cleanup without retry after uncertainty, and release on stop or lease expiry.
  Arbitrary VNC is refused before staging. The daemon mutation bridge is
  deliberately unexposed until a one-time capability is bound to the exact
  approval checkpoint, so this is not a physical attach claim.
- Critical/high history coverage checkpoint: three ledger tests pass. All 46
  critical/high incident IDs are mapped exactly once; each non-open control
  family names existing pytest nodes, and every status retains a concrete
  limitation. The public denominator is 6 locally covered, 40 partial, and 0
  open rather than treating the existence of the audit as remediation. The
  three replacement incidents moved from open to partial after grounded
  Replace/Replace All/Replace in files controls, including an OCR-corrupted
  label, became approval-gated while Find and Replace navigation remained
  allowed. The compounding Undo/Redo incident also moved to partial after both
  shortcuts became approval-gated. The five editor incidents moved from open
  to partial after exact helper-backed baselines, unique logical-line bounds,
  diff previews, diagnostic invariants, a one-cycle repair budget, and
  whole-unit rollback payloads gained local regression coverage. Managed
  orchestration, rollback execution, and real Word/VS Code/rich-text evidence
  remain incomplete.
- Post-conformance harness selection: 308 tests passed, one official-suite
  opt-in case skipped, and one local-socket UI case was deselected in 3.89
  seconds. This covered API/client setup/config/MCP, onboarding, packaging,
  performance, all provider adapters, storage/reporting/scoring, UI, 51 managed
  harness cases, seven provider-conformance cases, model routing, 21 Office
  cases, public benchmarks/suites, Windows bootstrap, and history audit. The
  receiver file was excluded after its first `TestClient` hit this runner's
  known blocked worker-thread path; no partial receiver progress is counted.
- Current submit-once checkpoint: 348 tests passed across bounded API,
  configuration, performance, static UI, UI-fixture, live-frame/report,
  support-bundle, model-budget, provider/client, guarded direct visibility and
  policy, MCP facade, Office, Windows bootstrap, package, and onboarding
  selections. Four optional real-loopback cases were skipped. The authenticated
  static-ASGI case, receiver module, and original direct-runtime module stall
  on this runner's blocked worker-thread path; they were terminated and are not
  included in the passing denominator. Python compilation, JavaScript syntax,
  scorecard evidence, and `git diff --check` passed.
- Current focused completion audit: 113 safety/onboarding tests passed with
  four real-loopback skips; 64 public evidence/scoring/history tests passed
  with one opt-in skip; 109 managed-loop/provider/MCP tests passed with one
  opt-in skip; and 25 guarded direct-call visibility tests passed. Isolated
  original-runtime checks passed 20 burst, 7 core-model, and 36 policy tests.
  Parallel sandbox-sensitive async fixture teardown stalled and was terminated;
  its partial progress is not included in any denominator.
- Bounded-history and synthetic UI-fixture slice: 12 storage tests, 39 managed
  loop tests, 17 direct-call tests, 12 API tests, 6 MCP driver tests, and 5
  fixture/safety tests passed. The 100,000-event benchmark, module compilation,
  CLI help, and diff checks also passed.
- Action-bound typing read-back checkpoint: at integrated commit `f3f6635`,
  42/42 frontend tests and 153/153 focused typing, harness, API, fixture, and
  security-boundary tests passed. A detached broader run completed 1,037
  passes, one environment-dependent skip, and one explicit deselection in
  81.97 seconds. The deselected assertion requires the shipped `.mcp.json` to
  name the current checkout path, while the isolated verification checkout was
  intentionally under `/tmp`. The production build passed at 1,224,925 raw
  bytes, with 313,393-byte gzip JavaScript and 17,936-byte gzip CSS. The
  assistant-ui action transcript now binds the exact requested text to watched
  screen read-back, focus evidence, edit distance, corrections, retries, and
  guarded transport state; secret-marked text retains neither payload nor
  observed read-back. This is target-free evidence. The in-app browser URL
  policy blocked post-change visual/reflow inspection, and no alternate browser
  was used.
- Cross-browser chat-workspace checkpoint: at clean source commit `b27070a`,
  Playwright 1.61 ran the authenticated 1,200-event fixture in Chromium 149,
  Firefox 151, and WebKit 26.5. All three engines expanded 12/12 computer
  actions, loaded 20 action-bound previews, retained exact MCP/model identity,
  exposed provider-owned OAuth and environment-owned API routes, kept the held
  approval controls reachable, and showed truthful guarded-direct ownership.
  Document, conversation, and action overflow were zero at 1440×900 and
  390×844. Page errors, console errors, external requests, approval submissions,
  and computer inputs were all zero. Chromium, Firefox, and WebKit completed in
  3.631s, 7.833s, and 5.336s. WebKit used Playwright's official Noble image
  because the Fedora host lacks its Ubuntu-linked ABI. The audit found and
  fixed a real visibility defect: direct mode's badge had lived in the composer
  toolbar even though direct runs hide the composer. This is target-free browser
  evidence, not live computer, browser-decode, resident-memory, or multi-hour
  evidence. The machine-readable report is
  [`../bench/results/2026-07-27/ui/cross-browser-chat-workspace-audit.json`](../bench/results/2026-07-27/ui/cross-browser-chat-workspace-audit.json).
- At-most-once input-integrity checkpoint: a deterministic backend accepted
  only the leading space of a word-boundary chunk while stale OCR still
  returned the pre-chunk prefix. The previous recovery branch replayed the
  whole chunk and produced two spaces, reproducing the reported failure at the
  real watched-typing seam. The branch was removed because keyboard input has
  no target-side idempotency acknowledgement. Ambiguous delivery now stops
  unverified. A seeded 1,000-case stale-readback regression emitted every
  canonical payload exactly once with zero introduced doubled spaces and zero
  retries. The receipt now distinguishes the requested and canonical delivery
  hashes, the actual ordered payload stream handed to the transport, the
  complete OCR text, and the exact captured-frame hash. The UI labels this
  strongest screen-bound state `Exact visual read-back`; it does not call it a
  guest acknowledgement. No computer, VNC, PiKVM, provider, or approval was
  contacted for this checkpoint.
- Python regression suite: 569 passed, 1 opt-in benchmark skipped, and the one
  known-failing Paddle field-crop regression explicitly deselected in 58.01
  seconds.
- JavaScript syntax check: passed.
- Harness YAML and design JSON parsing: passed.
- Python module compilation: passed with bytecode directed outside the source
  tree.
- `git diff --check`: passed.

That 569-test statement is a retained historical checkpoint. The Paddle crop
regression was fixed afterward: the requested region is now the only image
sent to inference, its temporary file is removed on success and failure, and
three focused tests pass. This does not revise the published full-image OCR
accuracy; see
[`../bench/results/2026-07-25/ocr/paddle-region-remediation.json`](../bench/results/2026-07-25/ocr/paddle-region-remediation.json).

The suite covers structured model output, provider fallback and readiness,
MCP/API authentication, event and frame visibility, durable pause/stop,
idempotency, stale-world refusal, approvals, direct-burst policy, VNC keyboard
translation, OCR verification, observer protocol, transcript import, scoring,
reporting, responsive UI behavior, target-identity continuity, manual-input
epoch revocation, and concurrent machine-client exclusion.

The target-continuity slice was also exercised through the actual persistent
MCP child and authenticated browser UI on isolated non-production ports. Ten
fake-machine session opens measured 44.682 ms median and 55.544 ms linear p95.
The 390×844 audit had equal 375-pixel document client/scroll widths and zero
console errors. With an authenticated SSE client connected, one SIGINT closed
the harness cleanly in 0.417 seconds without a forced timeout, traceback, or
second signal. Full structured evidence is in
[`../bench/results/2026-07-25/ui/target-continuity-audit.json`](../bench/results/2026-07-25/ui/target-continuity-audit.json).

The benchmark taxonomy and proposed seeded PiKVM-100 model-routing experiment
are documented in [`BENCHMARKS.md`](BENCHMARKS.md).

## Remaining release gates

The local implementation is ready for continued evaluation, but the local
operator edition should not be called release-ready until:

1. the exact Windows observer benchmark passes after a VM snapshot reset,
   including precise-code OCR and the full editor matrix;
2. the same benchmark passes against at least one Linux VM;
3. repeated trials establish accuracy and latency distributions rather than
   one-shot results;
4. nested guest transitions on helper-free production machines gain a bounded
   visual identity contract; the instrumented Windows lab now independently
   proves guest, input desktop, foreground process, and focused control;
5. packaging, signed updates, and threat modelling are complete;
6. a hardware-independent brake or documented availability boundary exists for
   loss of the selected daemon itself.
