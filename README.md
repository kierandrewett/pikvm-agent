# PiKVM Agent

For an isolated MCP test environment backed by a runtime-selected VNC target,
see [docs/VNC_HARNESS.md](docs/VNC_HARNESS.md).
`lab up` supervises the adapter, isolated daemon, and authenticated operator UI
and generates managed-only client configs for Claude/Gemini, Codex, and
OpenCode. The selected VNC endpoint is passed only to the adapter process.

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
| PaddleOCR (PP-OCRv6) | OCR text + boxes for read-back / verification |
| LangGraph | state graph, routing, checkpointing, interrupts/resume |
| MCP Python SDK | MCP protocol plumbing only |
| FastAPI | local daemon API |
| OpenRouter | structured multimodal operator decisions only |

## Architecture

```text
Chat UI
  → AssistantHarness
      answer normally / use one visible MCP tool
      or explicitly hand off a computer task
        → Managed AgentHarness
            reason → one bounded action → verify → recover / finish
              → private raw MCP child
                → guarded PiKVM Agent Daemon
                  → runtime-selected PiKVM or VNC adapter
```

The desktop app is a normal chat agent first. Greetings, questions, writing,
code, and research do not acquire a computer session. Every non-computer tool
call appears inline; only a locally reviewed read-only allow-list can run
without approval. When the user actually asks to view or operate the computer,
the assistant hands the exact task to the managed reason-act-verify loop.

The managed computer harness exposes only five high-level task controls to
coding clients. The **daemon** remains the authoritative machine boundary for
sessions, frame state, policy, approvals, and execution. Raw tools are private
to the harness; guarded direct MCP is an explicit compatibility mode.

## Install

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
cd ~/dev/pikvm-agent
uv venv --python 3.12
uv pip install -e '.[dev]'
```

The **core** install needs no Python ML toolchain. When the system `tesseract`
CLI is present, the runtime uses the same PSM 6, 2× raw/preprocessed ensemble
and syntax-aware selection profile as the published 1,000-case benchmark. If
it is absent, the runtime falls back to PiKVM's built-in text-only OCR
(`/api/streamer/snapshot?ocr=1`). OmniParser and PaddleOCR remain optional.
Known-intended-text read-back may retain one additional independent 1.5×
candidate, but only an exact match can verify the intended text; ordinary
screen OCR keeps the two-read latency budget.

Set `ocr.provider: hybrid` to keep Tesseract on ordinary screen parsing while
adding PaddleOCR as independent evidence only for precise known-intent
read-back. Paddle runs in one persistent, killable local worker process shared
by the runtime OCR paths, so a bounded secondary timeout cannot strand the
daemon's event-loop executor. Its evidence cannot select a nearest guess or
authorize a follow-up action. This remains opt-in until a fresh end-to-end
runtime pass clears the release gate.

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

## Run

The daemon owns sessions, watchers, policy, approvals, and execution. There is
no fallback machine-control target, and the public raw MCP server also refuses
to start without an operator visibility boundary:

```bash
# 1. the daemon (FastAPI). Set PiKVM creds, or PIKVM_AGENT_FAKE=1 for no hardware.
PIKVM_USER=admin PIKVM_PASSWORD=… uv run pikvm-agent daemon
#    → human console at http://127.0.0.1:47615/  (live frame, event feed, approvals)

# 2. run the visible harness and generate a managed MCP client configuration
uv run pikvm-agent harness serve --config config.harness.yaml
uv run pikvm-agent harness client-config \
  --config config.harness.yaml \
  --client codex
```

The generated configuration defaults to five high-level managed controls. If a
coding agent must remain the planner, generate `--control-mode direct`; its
ordinary PiKVM tools then run through an authenticated preflight/completion
boundary. Calling `pikvm-agent mcp` with only `PIKVM_AGENT_DAEMON` is refused,
and unconfigured protocol dispatch fails before the tool body.

Validate the vision pipeline against a still image without a Pi:

```bash
uv run pikvm-agent smoke-test --screenshot sample.png
```

## Visible provider-neutral harness

The canonical adapter tiers, authentication ownership, compatibility rules,
and promotion policy are documented in
[`docs/PROVIDER_SUPPORT.md`](docs/PROVIDER_SUPPORT.md). A locally ready
provider is not presented as live-compatible without dated scorecard evidence.

Direct Claude/Codex use of the burst tools bypasses the daemon operator loop and
hides the action lifecycle inside a coding CLI. The standalone harness adds a
durable reason → act → verify loop, ordered OAuth/API provider routes, stable
idempotent recovery, a high-level MCP surface with no raw HID tools, and a
responsive authenticated operator console. Guarded direct calls retain the
actual tool, exact redacted arguments, completion/refusal status, and latency;
internal frame paths and unbounded daemon result payloads stay behind the
authenticated evidence endpoints.

The out-of-band brake also requires an explicit daemon and does not depend on
the model or operator web process:

```bash
PIKVM_AGENT_DAEMON=http://127.0.0.1:<selected-daemon-port> \
  pikvm-agent panic-stop
```

It reports success only after the daemon confirms that started HID work has
quiesced, and prints the safe machine alias and fingerprint it stopped.

```bash
# Auto-detect logged-in Codex/Claude CLIs and a configured dedicated Gemini
# profile, add packaged read-only web research, then write a secret-free config.
pikvm-agent harness init --out config.harness.yaml
# Use --no-web-search for a computer-only/offline installation.

# Gemini CLI OAuth requires an isolated profile root. First choose an absolute
# path, export its name below, and log in with that profile:
# export PIKVM_GEMINI_CLI_HOME=/absolute/path/to/dedicated/profile-root
# GEMINI_CLI_HOME="$PIKVM_GEMINI_CLI_HOME" gemini
# pikvm-agent harness init --oauth-clis codex,claude,gemini \
#   --out config.harness.yaml
#
# Add API routes when wanted; values remain in runtime environment variables.
# pikvm-agent harness init --out config.harness.yaml \
#   --openai-model <model> --gemini-model <model>
#
# Azure OpenAI can use --azure-auth api-key, entra-env, or azure-cli:
# pikvm-agent harness init --out config.harness.yaml --oauth-clis none \
#   --azure-model <deployment> \
#   --azure-base-url https://<resource>.openai.azure.com/openai/v1 \
#   --azure-auth azure-cli
#
# Vertex AI can use a gcloud-owned or externally refreshed token:
# pikvm-agent harness init --out config.harness.yaml --oauth-clis none \
#   --vertex-model <model> \
#   --vertex-base-url https://aiplatform.googleapis.com/v1/projects/<project>/locations/global/publishers/google \
#   --vertex-auth gcloud

export PIKVM_AGENT_DAEMON=http://127.0.0.1:<explicitly-selected-daemon-port>
export PIKVM_HARNESS_TOKEN="$(openssl rand -hex 32)"
export PIKVM_HARNESS_AGENT_TOKEN="$(openssl rand -hex 32)"
export PIKVM_HARNESS_OBSERVER_TOKEN="$(openssl rand -hex 32)"

pikvm-agent harness check --config config.harness.yaml
pikvm-agent harness serve --config config.harness.yaml
```

Open the printed `/app/` URL and paste the harness token. The console keeps the
live machine, current action transaction, provider selection, semantic event
timeline, live elapsed provider/tool activity, wall/model/progress/recovery
metrics, durable provider-attempt/cost budget, and any exact approval request
visible together. Managed verification also shows the labelled, full-resolution
before/after screen supplied to the verifier. The browser fetches those bytes
through an authenticated, no-store endpoint and never receives the local
artifact path. A model-facing client submits one goal; the harness crosses
its internal bounded action slices and safe replans automatically until
completion or a meaningful approval, uncertainty, safety, provider, cost, or
operator stop. Exact approval releases the remaining task without handing
action planning back to Claude Code or Codex. A configurable autonomous-resume
ceiling prevents model-only replan loops. The counter comes from the durable
run transcript, so restarting the harness cannot reset that safety boundary;
an already-authorized slice is recovered exactly once. Restart recovery
resumes only internal yields—not uncertainty or human pauses. Managed fallback
and schema-repair calls consume the same run budget before invocation. Direct
MCP runs say `external` because the
harness cannot truthfully meter a coding client's model usage. The run rail omits
event history, detail renders only a bounded tail with the durable total, and
the current model/tool activity remains in the durable run summary even after
its start event rolls out of that tail. Same-status activity transitions are
pushed over the authenticated event stream alongside heartbeat/reconnect
state. Durable
state uses an atomic non-event checkpoint plus append-only event rows, so a
long run does not rewrite its complete timeline on every action. Pause
cancels the in-flight model loop while retaining its
durable checkpoint; Guide records operator-only guidance, cancels an in-flight
provider wait, forces a fresh reasoner plan, and can resume under harness
ownership. It refuses to discard an unsettled HID action or take control from
a direct MCP client. Stop interrupts the loop and aborts the computer session.
The token stays in browser `sessionStorage` and is sent only in authorization
headers. Provider status shows route order, adapter/auth owner, configured
model alias, API/CLI interface, pixel and structured-output capabilities,
latency, skips, coarse failures, cooldown, and the latest blind-conformance
exact/schema/latency result. Direct-client model metadata is
labelled launcher-declared because MCP does not provide an independently
verifiable model identity. “Prerequisites present” is deliberately not
presented as proof of working authentication.

Normal assistant tools use the standard MCP Python SDK over persistent stdio or
Streamable HTTP sessions. Tools are namespaced as `<server>.<tool>`, must be on
an explicit local allow-list, and never inherit auto-execution authority from a
remote server's annotations. `harness init` enables DDGS search, news, and page
extraction as reviewed read-only tools; additional MCP servers can be declared
under `assistant_tools`. Header values and child-process credentials are
inherited by environment-variable name and are not written to configuration or
returned by the UI. Any tool not on the local `read_only_tools` list pauses for
an exact browser approval showing its arguments before execution.

Compare configured OAuth and API routes against identical seeded pixels and
one strict schema without opening any computer target:

```bash
pikvm-agent harness provider-conformance \
  --config config.harness.yaml \
  --cases 10 \
  --concurrency 2 \
  --allow-provider-calls
```

Omit `--provider` to include every configured route, or repeat it to select an
explicit matrix. The consent flag is mandatory because these are real,
potentially billable provider calls. Unavailable routes and failed calls remain
in the denominator. The mode-0600 report defaults to the path shown in
`config.harness.yaml`; the running UI polls only its provider/model, exactness,
schema-validity, failure-class, latency, and timestamp aggregates. The local
report also retains normalized usage totals and synthetic expected/observed
fields for review. The UI receives no prompt, synthetic ground truth,
image/file path, raw response, or provider error body. Existing reports are
never overwritten.

Prove the complete chat-first route with one live provider before giving it a
computer target:

```bash
pikvm-agent harness assistant-conformance \
  --config config.harness.yaml \
  --provider claude-account \
  --out bench-local/assistant-claude.json \
  --allow-provider-calls
```

The fixed five-case contract covers a greeting, an ordinary question, sourced
research through the configured web MCP, an explicit computer hand-off, and a
simulated consequential send-message tool. The hand-off terminates at a
recording sink. The send-message canary has no external transport and passes
only when it is held at exact human approval with zero broker executions. This
command has no daemon, VNC, PiKVM, email, or messaging dependency and reports
`computer_target_contacted: false`. Use repeatable `--case` options to run a
named case in isolation. The mode-0600, no-overwrite report keeps only bounded
outcome, tool name, citation host, latency, and provider-call counts; it stores
neither prompts, replies, nor tool arguments.

For repeatable responsive/stream audits without any computer target:

```bash
pikvm-agent harness ui-fixture
```

This loopback-only fixture prints a one-time token, preloads 1,200 events, and
continuously alternates visible provider and exact tool activity while serving
a changing synthetic frame. It refuses the production daemon port and never
opens VNC, PiKVM, or a model API.

For support diagnostics without contacting the selected machine or a provider:

```bash
pikvm-agent harness support-bundle \
  --config config.harness.yaml \
  --out pikvm-support.json
```

The mode-0600 JSON bundle contains runtime versions, aliased provider
capabilities/readiness, route shape, credential presence/length/distinctness,
target-selection validity, storage counts, and UI asset hashes. It contains no
token values, provider/model names, configuration or artifact paths, task
text, events, screenshots, provider output, or machine endpoint. Existing
files are never overwritten; review the bundle before sharing it.

Every managed run has a durable provider-attempt cap. Optional metered cost
caps require a customer-supplied, versioned price table and a billing
classification for every routed provider; the harness never guesses prices.
It reserves a declared upper bound before each metered request and settles from
the provider's explicit usage fields. Missing/invalid usage commits the
reservation and pauses before HID. See
[`config.harness.example.yaml`](config.harness.example.yaml).

Routine Office work has a stronger acceptance boundary than “the model said
done.” The checked suite supplies target-neutral Word and Excel tasks; the
Windows lab observer reads the saved file bytes, and the host independently
parses the DOCX/XLSX package. A pass requires both a completed managed run and
artifact checks such as title/word-count/required phrases or exact worksheet
cells/formulas. The operator transaction remains in `Validating file` until
that observer-owned evidence passes or fails; neither the model token nor the
browser operator token can publish the result:

```bash
pikvm-agent harness office-case \
  --vnc "$PIKVM_LAB_VNC" \
  --config config.harness.yaml \
  --suite bench/office-acceptance-v1.yaml \
  --task-id excel-quarterly-earnings \
  --artifact-url "$PIKVM_OBSERVER_ARTIFACT_URL" \
  --output bench-local/office-excel-run-1
```

If `C:/PiKVM-Harness/observer.exe` is already installed on a disposable VM,
replace `--artifact-url ...` with `--skip-provision`. The runner still
restarts that helper with the attempt's fresh, randomised workspace artifact
path; it does not assume that a pre-running process could know the path.

The endpoint and artifact path are runtime inputs. Consequential approvals
remain in the operator UI; the runner has no approval method. The supplied
remote VM has not yet produced a passing Office result, so this command and its
contract are not presented as live-task proof.

Before signing or publishing a local-operator wheel:

```bash
uv build --wheel
pikvm-agent harness inspect-wheel --wheel dist/pikvm_agent-*.whl
```

The acceptance command verifies every wheel `RECORD` SHA-256/size, package
identity, the `pikvm-agent` console entry point, managed harness, model-budget,
managed-client acceptance, client-isolation audit, isolated managed-client
launcher, target-free managed smoke lab, worker-free stdio,
Office-acceptance and provider-conformance modules, and all three operator UI
assets. It rejects
unsafe/duplicate ZIP paths,
missing UI, runtime databases, `.env`, bytecode, tests, benchmark data,
oversized members, and integrity mismatches.

Set a human-readable alias and stable opaque identity in the selected daemon
configuration:

```yaml
pikvm:
  machine_alias: "Windows test VM"
  machine_id_env: "PIKVM_MACHINE_ID"
  desktop_layer: "VNC console"
```

Only a hash of `PIKVM_MACHINE_ID` is exposed. The console and approval shelf
show that fingerprint on every run. A target-fingerprint change, manual cursor
input, or concurrent machine client revokes the prior control authority before
further HID.

The generated client configuration defaults to the five high-level managed
controls. The scoped agent token can create, inspect, continue, pause, and
abort runs; it cannot steer, read provider administration, or resolve an
approval:

```bash
pikvm-agent harness client-config \
  --config config.harness.yaml \
  --client codex
```

Use `--client claude`, `--client gemini`, or `--client opencode` for their JSON
MCP formats. The generated launcher derives the harness URL at process start
and forwards only the agent-token environment-variable name. It also supplies
a validated static client label such as `codex-cli`; that label is stored with
the managed run and shown in the run rail, machine/session header, and plan.
It is source attribution, not a claim about the model selected inside that
client. The safe
high-level MCP stays registered if the harness starts later or restarts; each
tool call reconnects to the authenticated API. An outage returns a stable,
redacted error that explicitly keeps the task in managed mode; HTTP bodies,
internal endpoints, and credentials are not copied into the coding client.
On POSIX, both the high-level process and the harness-owned raw MCP child use
descriptor-ready stdio instead of the SDK's worker-backed stdin wrapper. MCP
message validation and session semantics still come from the SDK.
The next call uses a fresh connection and can recover without restarting that
client. `computer_continue` is for a
meaningful paused checkpoint; coding clients do not have to advance routine
four-action slices. Deployments that require
fail-fast startup can add `--require-ready`. The launcher does not forward the
machine target or browser approval credential.

After merging the generated entry into a client, audit every effective
project/user configuration scope before starting it:

```bash
pikvm-agent harness client-audit \
  --client codex \
  --native-inventory \
  --project . \
  --out /tmp/codex-client-audit.json
```

Codex native inventory resolves its enabled user, trusted-project, desktop, and
plugin-visible MCP registrations without launching those servers. The child
receives only `HOME`, `PATH`, and optional `CODEX_HOME`; raw inventory and
diagnostics are never printed or retained. For Claude, audit its user/local
scope plus project `.mcp.json`; Gemini and OpenCode likewise need every
effective file scope. The file audit accepts unrelated MCP servers but exits
nonzero unless it finds exactly one official managed PiKVM launcher and no raw,
guarded-direct, duplicate, malformed, or ambiguous PiKVM registration. Codex
TOML and native inventory, shared MCP JSON, Claude/Gemini JSON, and legacy plus
V2 OpenCode JSON/JSONC are supported. Retained reports use anonymous
`config-N` labels and never contain source paths, commands, arguments,
environment values, or parser input. Supply file scopes from lowest to highest
precedence; a later same-named server replaces the earlier definition, while
different PiKVM names remain simultaneously active and fail. Only Codex
currently has safe native effective-inventory enumeration; omitting another
client's ambient scope is not an isolation proof.

For Codex, Claude, Gemini, or OpenCode, avoid changing any persisted
user/project registration:

```bash
# Inspect the exact isolated launch first.
pikvm-agent harness client-launch \
  --client codex \
  --config config.harness.yaml \
  --project .

# Then start the coding client only after the same isolation audit and a
# loopback managed-harness readiness check.
pikvm-agent harness client-launch \
  --client codex \
  --config config.harness.yaml \
  --project . \
  --execute
```

Codex receives an inline override for the `pikvm` registration, preserves its
normal OAuth state and unrelated MCP servers, and runs native effective
inventory before launch; any second raw/direct PiKVM name refuses the launch.
Claude receives one explicit secret-free config with
`--strict-mcp-config`, after the installed CLI proves both strict-isolation
flags exist. OpenCode receives an inline managed-only config under native
`--pure` mode; its resolved config must retain exact default-deny permissions,
one `pikvm_*` allow rule, and exactly one managed PiKVM registration. Its home
and writable state are ephemeral, while its own OAuth file is linked into that
state without copying the credential. Ambient secrets are not forwarded.
None of these routes writes, removes, or renames a persisted registration or
places an agent-token value in its argument vector. Gemini additionally
requires a dedicated profile and clean workspace. Its installed native
effective-settings loader must resolve one system-defined, system-allowlisted
managed MCP server. The launcher configures `--extensions none` and a
supplemental default-deny policy whose only allow rule is that server. The
current empty-profile result verifies settings and exact policy content, not
extension/policy enforcement or an authenticated task.

Run one non-interactive task without placing its text in the process argument
vector:

```bash
printf '%s\n' 'Use computer_start_task, wait for completion, and report the run ID.' |
  pikvm-agent harness client-task \
    --client codex \
    --config config.harness.yaml \
    --project .
```

`client-task` performs the same inventory audit and harness-readiness probe,
then sends the private task to the coding client over stdin. Codex uses an
ephemeral read-only local sandbox; Claude uses `dontAsk`; Gemini uses
non-interactive stream JSON with its managed MCP allow-list and default
approval mode; OpenCode uses `run --format json` under the same pure,
default-deny environment. None uses a permission-bypass flag. Its terminal
summary contains only task length/digest, exit status, timing, and isolation
metadata.

For adoption testing without VNC, PiKVM, a daemon, or an external model API,
start the target-free managed app:

```bash
pikvm-agent harness smoke-lab \
  --config config.harness.example.yaml \
  --root /tmp/pikvm-managed-smoke
```

It uses the production managed run/UI interfaces with explicitly labelled
deterministic machine/model adapters. This is a launch and visibility contract,
not a computer-use accuracy result.

Before installing a generated config into a coding client, run the same
generated launchers through the target-free acceptance loop:

```bash
pikvm-agent harness client-acceptance \
  --out /tmp/managed-client-acceptance.json
```

This starts a deterministic synthetic harness, launches the exact generated
Codex, Claude, Gemini, and OpenCode MCP commands, submits one managed task per
client, checks operator visibility and durable SQLite state, interrupts and
restarts the harness, and measures startup, task, and recovery latency. It
does not contact a computer target or external model provider. The JSON report
is created mode `0600`, is never overwritten, retains every failure in the
denominator, and the command exits nonzero if any client fails.

If Claude Code or Codex should remain the planner and call the ordinary PiKVM
tools directly, choose the explicitly weaker compatibility mode:

```bash
pikvm-agent harness client-config \
  --config config.harness.yaml \
  --client codex \
  --control-mode direct

# The generated client starts this boundary:
PIKVM_MCP_PROVIDER=openai-oauth \
PIKVM_MCP_MODEL=<actual-model-string> \
  pikvm-agent harness direct-mcp \
  --config config.harness.yaml \
  --mode guarded \
  --caller-label codex-cli
```

The direct configuration also forwards only environment-variable names. It
never writes the daemon target or any token value. The direct MCP process
receives the observer token, not the browser operator token.

`guarded` mode fails closed before a tool body if the harness cannot record the
preflight. `observe` is an explicit degraded migration mode that keeps only
screenshots, OCR, abort, and panic-stop available if the UI is offline; every
keyboard, pointer, typing, playbook, session-start, and autonomous action still
fails closed. It is not the generated default. Without either boundary, public
raw MCP startup and dispatch are refused. In both configured modes, an operator
pause blocks future HID while leaving screenshots/OCR available, the direct
model cannot approve its own held action, and completion-telemetry failure
never turns already-completed HID into an ambiguous tool error. Stop aborts the
active daemon session. Set `secret=true`
on credential `pikvm_type_text` calls so the timeline records the input without
exposing its value. Raw text preflight refuses command-shaped dense Base64
transfers, encoded PowerShell, heredoc openers, and long nested shell payloads
before HID, including transfers split across contiguous type actions. The raw
model-facing MCP does not expose approval tools;
only the harness-owned private MCP child can relay a decision already made in
the authenticated browser UI.

See [`docs/HARNESS_ARCHITECTURE.md`](docs/HARNESS_ARCHITECTURE.md) for the
provider/auth model, visibility events, UI information architecture, trust
boundaries, and SaaS edge/control-plane split. The target-free exact-byte media
builder and approval-gated virtual-media transaction coordinator are
specified in [`docs/CONTENT_TRANSFER.md`](docs/CONTENT_TRANSFER.md). The
builder, exact browser approval, durable transition trace, rollback,
lease/stop release, and cleanup uncertainty latch are implemented locally.
The daemon mutation bridge remains unexposed until it has a one-time capability
bound to the approved checkpoint; no physical attach result exists. The
selected VNC/PiKVM location
is never stored in harness code or configuration. The evidence and remaining
live-oracle gate are recorded in
[`docs/VALIDATION_REPORT.md`](docs/VALIDATION_REPORT.md).

Run the offline 1,000-case blind OCR baseline and inspect per-model/HID speed
for a saved task:

```bash
pikvm-agent harness ocr-benchmark \
  --cases 1000 --seed 104729 --evaluation-seed 65537 \
  --jobs 4 --out /tmp/pikvm-ocr-blind

# A separate release gate targets OCR's tendency to collapse repeated spaces.
# It can run as one job or as resumable deterministic shards.
pikvm-agent harness ocr-spacing-benchmark \
  --cases 1000 --seed 104729 --evaluation-seed 65537 \
  --jobs 4 --out /tmp/pikvm-ocr-spacing

pikvm-agent harness run-metrics \
  --state .pikvm-harness/state.sqlite3 \
  --run-id <run-id>

# Fails if the public headline or any linked evidence digest has drifted.
pikvm-agent harness scorecard --check
```

See [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) for OSWorld, Windows Agent
Arena, ScreenSpot-Pro, and the proposed seeded PiKVM-100 comparison protocol.
The centralized, failure-inclusive real-world scorecard is
[`bench/README.md`](bench/README.md); it links the durable per-case public-suite
reports and never presents an infrastructure-blocked run as a score. Its
headline table is generated from [`bench/scorecard.yaml`](bench/scorecard.yaml);
the test suite and `harness scorecard --check` both fail if a report, digest,
denominator, or rendered value drifts.
The first authenticated Codex `client-task` now appears there as a
failure-inclusive two-attempt result: the pre-fix approval cancellation and
the fixed 13.7-second target-free managed completion are both retained. It
does not claim a live Windows or Office pass.
The full discussed scope—including incomplete and externally blocked items—is
tracked in [`docs/REQUIREMENT_STATUS.md`](docs/REQUIREMENT_STATUS.md).
Live OSWorld attempts can add `--operator-console` to serve the authenticated
screen/action/evidence timeline from the same run store while the case is in
progress. Approval decisions made there resume the exact pending checkpoint;
the generated `operator-console.json` contains discovery metadata but no token
or machine endpoint.

## Status

The original eight core-runtime build phases are complete (see `docs/PLAN.md`):
daemon + MCP facade, PiKVM client, world-versioned frames, local vision,
hard-coded safety policy, watched typing, guarded execution and recovery,
checkpointed autonomous operation, regression bench, console, and Atlas export.

The provider-neutral harness, high-level MCP facade, VNC adapter, operator UI,
transcript importer, and Windows observer are implemented and covered by the
automated suite. The local operator edition is still pre-release: the exact
observer benchmark must pass live on Windows and Linux before the product gate
in [`docs/SAAS_PRODUCT.md`](docs/SAAS_PRODUCT.md) is satisfied. Run the suite
with `uv run pytest`.
