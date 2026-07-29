# VNC-backed managed accuracy harness

The lab runs the managed operator harness and isolated PiKVM runtime against a
local PiKVM-shaped adapter. That adapter translates the bounded PiKVM
HTTP/WebSocket contract to any RFB/VNC server. Coding clients receive only the
five high-level task/status/control tools; they do not receive raw HID tools or
the daemon address.

The VNC endpoint is supplied only to the adapter process at runtime. It is not
written to the generated daemon configuration, MCP configuration, source code,
or benchmark report. The same adapter can target Windows or Linux.

## Isolation

- The production daemon port (`47615`) is rejected by the lab supervisor.
- The generated daemon talks only to the loopback adapter.
- Lab state, SQLite data, screenshots, traces, and MCP configuration live under
  the directory passed with `--root`.
- The generated MCP server is named `pikvm-lab`.
- Missing or shared operator/agent/observer credentials are refused before the
  adapter contacts the VNC target.
- A process-independent local target lease is acquired before RFB connection.
  A second lab selecting the same canonical endpoint fails closed even when the
  VNC server does not report the other client. The lease filename contains only
  a versioned endpoint digest, never the endpoint.
- Quiet child startup failures retain a bounded, redacted diagnostic. Known
  lease contention is shown as one actionable cause in the terminal; the full
  debug trace is written only to the requested mode-0600 report.
- Stopping `lab up` terminates the adapter, daemon, and operator harness.

Install the harness extra and start an isolated lab:

```sh
uv pip install -e '.[harness]'
export PIKVM_LAB_VNC='host.example:5900'
export PIKVM_HARNESS_TOKEN="$(openssl rand -hex 32)"
export PIKVM_HARNESS_AGENT_TOKEN="$(openssl rand -hex 32)"
export PIKVM_HARNESS_OBSERVER_TOKEN="$(openssl rand -hex 32)"
python -m pikvm_agent.cli lab up --root /tmp/pikvm-mcp-lab
```

The default provider routes use existing Codex and Claude CLI logins. To use
API providers, custom model names, or a different reasoner/controller/verifier
order, pass a secret-free provider config:

```sh
python -m pikvm_agent.cli lab up \
  --root /tmp/pikvm-mcp-lab \
  --harness-config ./config.harness.example.yaml
```

The lab rewrites that config's target, bind, and state paths to isolated
loopback values. Provider credentials remain environment-owned.

VNC credentials are also runtime-only:

```sh
export PIKVM_LAB_VNC_PASSWORD='...'
export PIKVM_LAB_VNC_USERNAME='...'
```

The live benchmark reads those environment variables by name as well; it does
not accept a password value on its command line.

The lab prints the operator UI URL and emits three secret-free client files:

- `mcp.lab.json` for Claude/Gemini-compatible JSON configuration;
- `mcp.lab.codex.toml` for Codex;
- `mcp.lab.opencode.json` for OpenCode.

Each starts `harness managed-mcp` and forwards only the scoped agent-token
environment variable. Routine actions still cross real MCP stdio, the managed
reason-act-verify loop, the private raw-MCP child, the isolated daemon, and the
adapter. The coding client cannot bypass the visible harness by calling the
adapter or daemon directly.

## Exact Windows oracle

`observer/windows` builds a disposable Windows benchmark helper. It records:

- the editor's exact UTF-16 text as UTF-8;
- low-level keyboard and mouse events;
- byte-exact reads of the path shown in its file field;
- clicks on inert `DANGEROUS` controls.

The default oracle is visual. The helper paints each `pikvm-observer.v1`
snapshot as a paged black/white matrix with a conspicuous magenta border. The
benchmark reads those pages through `pikvm_screenshot`, validates page identity,
length, and CRC32, then compares the exact bytes. Ground truth therefore crosses
the same MCP/video boundary as the controller—there is no helper API or
clipboard dependency.

An authenticated HTTPS callback mode remains available for private lab
networks, but it is not the default.

Build the helper:

```sh
cmake -S observer/windows -B build/observer-windows \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_CXX_COMPILER=x86_64-w64-mingw32-g++ \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/observer-windows
```

Host only the compiled binary at a caller-owned HTTPS artifact URL, then run the
default visual benchmark. The URL and VNC target are runtime inputs and are not
written to source, generated config, or the report:

```sh
python -m pikvm_agent.harness.live_benchmark \
  --vnc "$PIKVM_LAB_VNC" \
  --observer-mode visual \
  --artifact-url "$PIKVM_OBSERVER_ARTIFACT_URL" \
  --report /tmp/pikvm-accuracy-report.json
```

Before spending several minutes on typing/OCR/editor trials, prove that the
expected helper owns the hotkeys and can independently identify the guest,
input desktop, foreground process, and focused control:

```sh
python -m pikvm_agent.harness.live_benchmark \
  --vnc "$PIKVM_LAB_VNC" \
  --observer-mode visual \
  --artifact-url "$PIKVM_OBSERVER_ARTIFACT_URL" \
  --identity-only \
  --report /tmp/pikvm-observer-identity.json
```

The probe fails closed if any identity field is missing or malformed. Public
reports retain only the source-side opaque guest fingerprint, never Windows'
raw MachineGuid or computer name.

If the helper is already running on the disposable VM, use
`--skip-provision`; no artifact URL is then needed.

The optional private callback mode is explicit:

```sh
python -m pikvm_agent.harness.live_benchmark \
  --vnc "$PIKVM_LAB_VNC" \
  --observer-mode https \
  --artifact build/observer-windows/pikvm-accuracy-observer.exe \
  --observer-public-base-url https://observer.lab.example \
  --report /tmp/pikvm-accuracy-report.json
```

The harness never creates a third-party quick tunnel. In callback mode, route
only the write-only receiver; never expose its evaluator.

## What is measured

The report separates independent signals:

- exact character accuracy from the observer;
- intended and observer-reported UTF-8 text SHA-256 values plus an exact-match
  bit;
- trailing extras, missing suffixes, and duplicated-prefix length;
- OCR exact and whitespace-normalised edit distance;
- exact file bytes;
- first differing byte and expected/actual SHA-256 hashes;
- opaque guest fingerprint, Windows session, and input desktop;
- foreground process/window and focused-control class/id;
- whether the focused control belongs to the foreground window;
- full key-down and input-event counts;
- a bounded key-down virtual-key sample for modifier/case diagnosis, with an
  explicit truncation flag in compact visual mode;
- dangerous benchmark commits;
- duplicate MCP retry behavior.

Missing file evidence is a failure, not an unavailable metric. The command
returns a non-zero exit status if any required text, OCR, click, idempotency,
file, foreground-app, or safety gate fails.

Release-evidence runs must restore a known VM snapshot before each attempt.
Closing stale editors with an unverified terminal command is not an acceptable
substitute: the command itself must remain fail-closed before Enter.

This catches the failure mode where OCR says a command is plausible while the
actual field contains one wrong character, a repeated chunk, or an extra
suffix.

### What the checksums prove

Input integrity is a chain, not one checksum:

1. The delivery hash proves the exact logical text accepted by the MCP.
2. The emitted hash proves the text handed to the keyboard transport and the
   at-most-once guard.
3. The read-back hash proves what strict OCR reconstructed from one retained
   screen frame; the frame has its own SHA-256. This is visual evidence, not a
   guest acknowledgement.
4. In the disposable lab only, the observer reports the focused editor's exact
   text. The score now publishes intended and observer-text SHA-256 values.
   Matching values are guest-side ground truth for that test.
5. For saved files, the observer returns exact bytes and the harness compares
   file SHA-256 values.

Production targets have no observer. A screen cannot calculate a cryptographic
hash of its own text, so the runtime must never label OCR-normalised text as
exact. If strict OCR cannot preserve every character and calibrated whitespace,
the action remains unverified and no submit, send, or Enter action may follow.

## Runtime safety changes

Direct MCP bursts require a bounded, non-blank, caller-stable
`idempotency_key`, and controllers must provide one for every logical burst.
Replaying the same key and payload returns the original result without sending
HID again; reusing a key for a different payload is refused.

The daemon, rather than the model, classifies direct bursts. Typed mutating
commands, save/commit shortcuts, and clicks whose local OCR indicates Send,
Submit, Delete, purchase, credential, consent, installation, or permission
changes pause with `needs_approval`. A coordinate click the daemon cannot
independently read also fails closed to approval. Approval is tied to the exact
payload, screen world, control epoch, and session. A stale approval never
force-executes.

Bare `Enter`/`Return` is also a commit boundary: depending on focus it can send
a chat message, submit a form, or execute a command. Direct bursts and named
playbooks therefore pause before that key until the human resolves the exact
pending approval. Preparing text and committing it remain separate
transactions.

Visible commit labels are matched against a fail-closed action taxonomy,
including common OCR corruptions. Save/OK/Continue, send/share/call, delete,
payment, member/owner/permission, install, credential, consent, upload, power,
and settings controls pause before HID. Controls that disable security,
firewall, antivirus, Defender, or protection are blocked outright. Routine
Search/Open/Cancel/View navigation remains allowed when independently grounded.

The VNC adapter preserves physical modifier chords from the PiKVM HID contract.
For example, `Shift` plus a physical letter or digit key remains that chord at
the RFB boundary; it is not prematurely converted to a US-layout punctuation
character. The guest keyboard layout therefore remains the authority for the
resulting symbol.

Precise text verification distinguishes a strong layout signature from ordinary
OCR noise. A confident alphanumeric-equal, symbol-different layout mismatch may
trigger the one bounded layout correction. A small mixed OCR mismatch is only
`unverified_ambiguous`: it stops before a commit and does not erase or retype
visually plausible code. Exact text/file evidence from the observer is required
to turn that ambiguity into a pass.

## Model split

The model split is configured under `operator.routing`:

1. The `hard` reasoner creates a checkpointed plan on the first step, after an
   explicit replan, after failed verification, and at a bounded refresh cadence.
2. The `cheap` controller follows that plan for routine one-step decisions.
3. The daemon independently enforces caps, freshness, idempotency, local OCR
   grounding, approvals, aborts, and key release.
4. OCR and screenshots return evidence to the reasoner.

Each trace records role, lane, and routing reason. Third-party operator
providers that do not yet accept a lane remain compatible. Neither model is
trusted with policy or retry semantics; changing models does not change the
hardware safety boundary.

## Product boundary

The reusable product seam is the daemon session, not the VNC adapter. A hosted
control plane can add tenant identity, billing, scheduling, artifact retention,
and fleet policy around it, while each customer-controlled edge worker keeps
credentials and raw HID local. Before exposing this as multi-tenant SaaS, the
remaining non-negotiable controls are per-tenant encryption keys, append-only
approval/audit records, retention controls for frames and traces, concurrency
quotas, regional workers, and an independently tested emergency-stop path.
