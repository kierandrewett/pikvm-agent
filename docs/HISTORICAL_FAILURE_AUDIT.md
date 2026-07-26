# Historical PiKVM failure audit

Audit cutoff: 2026-07-24 22:30 UTC

## Outcome

The local histories contain 24 conversations with 4,453 PiKVM tool calls:

| Client | Sessions | PiKVM calls |
|---|---:|---:|
| Claude Code | 15 | 2,876 |
| Codex | 7 | 1,482 |
| OpenCode | 2 | 95 |
| **Total** | **24** | **4,453** |

The reconstruction produced 69 redacted incident records. Nineteen are critical and 27 are high severity. These are incident sequences, not raw error-line counts: one record can contain repeated manifestations of the same failure, such as the eleven confirmed repeated-character typos in one policy-editing session.

The machine-readable corpus is `bench/historical_pikvm_incidents.json`. It omits raw typed bodies, screenshots, credentials, machine addresses, email addresses, and private file contents. It retains conversation ID, model attribution, source record indices, safe typo fragments, correction, outcome, risk, and concrete regression requirements.

`bench/historical_pikvm_coverage.json` separately maps all 46 critical/high
incidents exactly once to a current control family, real regression test nodes,
and an explicit limitation. Its checked status is 6 locally covered, 40
partial, and 0 open. The low covered count is intentional: the five editor
incidents moved only to partial after exact baseline/diff/diagnostic/rollback
contracts landed; local evaluators are not upgraded into real-application
claims.

The audit was read-only. It did not connect to or operate any PiKVM target.

## What failed most seriously

### 1. A successful tool call did not mean successful input

The most important transport failure was silent loss. In one Claude Sonnet 5 sequence, three adjacent 200-character encoded chunks never appeared on the remote machine even though the bursts returned success. The agent only discovered the missing 600 characters through an end-to-end hash mismatch.

Other sessions showed:

- input stopping mid-command;
- the final one or two characters arriving late or not at all;
- Enter racing ahead of the filename tail;
- suffix repair adding extra characters;
- repeated letters and punctuation;
- an action reporting cancellation while buffered HID input continued.

The MCP success contract is therefore wrong if it means only “events were queued.” A type action needs an observed-delivery state: attempted length, delivered or visually reconciled length, outstanding queue depth, interruption reason, and verification result.

### 2. Long input became an uncontrolled hazardous process

Two Codex GPT-5.5 sessions contain the clearest examples:

- a 4,265-character heredoc partially entered a terminal, opened a scratch editor, and triggered a Save As recovery loop;
- a 10,259-character command continued producing effects after the burst failed, and the first panic-stop report did not match what the user still saw being typed.

The user corrected this directly: keep commands short and inspectable, stage complex work in a temporary file, diff it, and apply a small surgical change. That correction should be enforced by the server, not left as prompt advice.

Required behavior:

- reject oversized type actions before emitting any HID events;
- reject heredocs, multiline scripts, and dense encoded blobs in normal raw-HID tools;
- separate type from submit;
- expose queue depth;
- make panic stop drain and invalidate every queued event;
- require a hardware-observed “stopped” state before reporting success.

### 3. Focus and target identity were not trustworthy

The histories repeatedly show text going to a different place than intended:

- terminal text entered a live source file;
- browser text entered a source file;
- commands landed in the Problems filter or editor;
- Company Portal, meetings, reminders, and Teams notifications stole focus;
- a terminal edit landed in an unsent Teams compose box;
- the agent believed it was in host Teams while the AVD title bar showed it was still inside the remote desktop;
- the agent inspected local or secondary VS Code clones while believing it was using the authoritative AVD;
- one live target exposed unrelated personal and work applications, causing the agent to stop.

Foreground validation must happen immediately before every text chunk and every Enter, not once at the start of a burst. “Same screenshot generation” is insufficient if a window can appear while input is running.

The harness should always show:

- physical target identity;
- nested desktop layer, such as host versus AVD;
- foreground process/window title;
- focused control class;
- control epoch owner;
- whether human input has occurred since the last observation.

Typing into an email, chat compose box, cloud console, password field, or destructive terminal prompt should require a stricter destination policy.

### 4. Visual grounding and OCR caused incorrect edits and navigation

The audit includes:

- OCR claiming a correct word was misspelled;
- a branch declared absent because it was outside the current viewport;
- clicks opening Domain groups, Sticky Notes, sound settings, or an unrelated search app;
- clicks repeatedly opening the wrong cloud page;
- wrapped visual lines being treated as complete logical lines;
- Replace All running with a truncated search string;
- wrong similarly named tabs and files being treated as authoritative.

OCR should produce token confidence and provenance, not just text. Before editing an alleged typo, the agent should corroborate it using a second frame, exact editor search, terminal output, or the Windows observer in lab mode. Absence claims need search-completeness evidence.

### 5. Recovery often compounded the original error

Several of the worst file incidents came from attempted repairs:

- partial suffix repair produced an extra malformed suffix;
- repeated undo and redo left a buffer structurally damaged;
- multiple fragment repairs progressively corrupted a whole section;
- stale screenshots triggered duplicate repair input;
- rich-text autocorrect and bullets changed Teams draft values during re-entry.

Recovery must be transactional. After one failed micro-repair, the safe default is to restore a known-good checkpoint and replace the smallest complete logical unit. The verifier must compare the surrounding region, not only the intended token.

### 6. Permission boundaries were too weak

No audited conversation proved that an email or Teams message was accidentally sent, but the system got dangerously close:

- terminal text landed in a Teams compose box;
- agent and user nearly edited the same compose box concurrently;
- rich-text transformations changed a draft;
- destructive cloud actions were proposed repeatedly and stopped by user rejection;
- an incorrect destructive text-processing anchor was caught only before submission;
- wrong-page and wrong-target navigation occurred near cloud mutation work.

External send, delete, publish, commit/push, cloud mutation, and broad file replacement must be two-stage:

1. prepare and verify the exact payload and target without committing it;
2. obtain a fresh action-specific approval immediately before the irreversible control.

Approval cannot be inferred from a general earlier request. The approval UI must show the actual destination, exact action class, target identity, and a concise effect summary. Human input or a target change invalidates the approval.

## Model attribution

### PiKVM call volume

| Model | PiKVM calls |
|---|---:|
| Claude Opus 4.8 | 1,147 |
| Claude Sonnet 4.6 | 583 |
| Claude Sonnet 5 | 1,146 |
| GPT-5.4 mini | 37 |
| GPT-5.5 | 1,444 |
| GPT-5.6-sol | 1 |
| Gemini 3.5 Flash through OpenRouter | 15 |
| Gemini 3.6 Flash through OpenRouter | 68 |
| Qwen 3.5 Flash through OpenRouter | 8 |
| GPT-5.4 mini-fast | 4 |

### Reconstructed incident records by attributed model

| Attributed model | Incident records |
|---|---:|
| Claude Opus 4.8 | 25 |
| Claude Sonnet 4.6 | 9 |
| Claude Sonnet 5 | 17 |
| GPT-5.4 mini | 2 |
| GPT-5.5 | 14 |
| Gemini 3.6 Flash | 1 |
| Gemini 3.5/3.6 transition sequence | 1 |

These figures are not failure rates and should not be used as a leaderboard. Tasks, risk, session length, PiKVM server version, operator mode, and user supervision differed substantially. One corpus record may combine many repeated errors, while another captures one event. The safe use is regression design: replay the failure class against multiple models under identical randomized conditions.

There were no reconstructed PiKVM input failures for the four calls made by GPT-5.4 mini-fast, eight by Qwen 3.5 Flash, or the single GPT-5.6-sol call. Those samples are far too small to imply reliability.

The first OpenCode session advertises Kimi K2.7 Code in session metadata, but Kimi made no PiKVM calls. The active model changed inside the session; call-level attribution resolves the actual PiKVM calls to Gemini 3.6 Flash, Qwen 3.5 Flash, and GPT-5.4 mini-fast.

### Attribution limits

- Claude Code records normally carry the model on each assistant tool-call message. Synthetic compaction messages were excluded from model attribution.
- Codex records carry the model in turn context. PiKVM calls inherit the active turn model.
- OpenCode stores a session-level model that can become stale after model switching. Attribution was reconstructed from the latest model transition preceding each call.
- Some incidents are plainly tool or transport failures, such as silent chunk loss. Others are model planning failures, such as choosing the wrong target. Many are mixed.
- Screenshots do not identify the internal operator model used behind a server-side autonomous call. The outer conversation model is known; the inner operator may not be.
- Histories capture what the assistant and user noticed. An undetected typo or unintended click with no later correction cannot be reconstructed from dialogue alone.

## What the user did to correct the system

The user was the most important safety layer in the histories. Recurring corrections were:

- identify host versus AVD from the title bar;
- insist the AVD, not a local clone, was authoritative;
- reject destructive tool calls;
- tell the agent to click the literal visible control instead of inventing a workaround;
- stop long HID buffers and mandate short commands;
- request panic stop when typing continued;
- point out filename tails arriving on the wrong side of Enter;
- take over a Teams draft when simultaneous input became unsafe;
- require exact verification before accepting code or policy changes.

These corrections should become product invariants and executable tests, not stay as user-specific memory.

## Coverage

| Client | Conversation | Calls | Reconstructed incidents | Call-level models |
|---|---|---:|---:|---|
| Claude Code | `17dade7d-4eaf-48fd-8694-92ea86f9fcbc` | 412 | 5 | Opus 4.8, Sonnet 5 |
| Claude Code | `2301754a-2916-4193-bd58-ceaa33f01356` | 11 | 1 | Sonnet 4.6 |
| Claude Code | `57a2ac47-c8c9-4d2b-8a39-2196a2374941` | 14 | 1 | Opus 4.8 |
| Claude Code | `64db6d25-1914-4d0e-9684-56818d66bd03` | 279 | 5 | Opus 4.8 |
| Claude Code | `6520c24b-6370-4c93-b157-17025a06074f` | 5 | 1 | Sonnet 4.6 |
| Claude Code | `84e5c39e-f05b-4b1e-b5ff-488990b6d050` | 160 | 3 | Opus 4.8 |
| Claude Code | `8d396eb9-df6f-4fb5-873a-e8083ab740b4` | 572 | 7 | Opus 4.8, Sonnet 5 |
| Claude Code | `afd29976-3642-4a13-886c-dd67a055b825` | 20 | 1 | Sonnet 4.6 |
| Claude Code | `b74cf224-d1e1-4fb0-adc3-fe73d6feae54` | 1 | 1 | Opus 4.8 |
| Claude Code | `agent-awhat-were-the-6f0ec4e4480f7474` | 15 | 2 | Opus 4.8 |
| Claude Code | `d818758c-c4e7-48c8-8cc4-3198caa7f226` | 244 | 6 | Opus 4.8 |
| Claude Code | `e0a35f1f-06d0-4012-9446-18db63f072ee` | 521 | 5 | Sonnet 4.6 |
| Claude Code | `f2ef736d-dc0d-4900-b75d-c4ffcb562c85` | 26 | 1 | Sonnet 4.6 |
| Claude Code | `737f59a5-cba2-44aa-ad94-669b120c6403` | 551 | 9 | Opus 4.8, Sonnet 5 |
| Claude Code | `969d00aa-cefa-4c21-a281-655717b432ac` | 45 | 3 | Opus 4.8 |
| Codex | `019f044e-f13e-71e1-8475-d7b60dd269ec` | 37 | 2 | GPT-5.4 mini |
| Codex | `019f1891-e094-7232-bf39-fdcfd08d6daf` | 77 | 2 | GPT-5.5 |
| Codex | `019f18de-77a1-7bf2-88a1-f532aa594023` | 117 | 3 | GPT-5.5 |
| Codex | `019f377d-0b09-77b1-b1a1-ac191ad61a0d` | 974 | 7 | GPT-5.5 |
| Codex | `019f383d-e781-7c93-8054-aa0286042ba6` | 21 | 0 | GPT-5.5 |
| Codex | `019f46eb-4a65-7782-9bb5-07d6918f76fa` | 255 | 2 | GPT-5.5 |
| Codex | `019f942a-8387-7f53-ab13-28bacd39b51a` | 1 | 0 | GPT-5.6-sol |
| OpenCode | `ses_0759ab1adffeahPfYnG9TVWd6T` | 25 | 0 | Gemini 3.6, Qwen 3.5, GPT-5.4 mini-fast |
| OpenCode | `ses_0758ae687ffeh5acBnBDtlBaqx` | 70 | 2 | Gemini 3.5, Gemini 3.6 |

Zero reconstructed incidents means no failure-and-correction chain was visible in that conversation, not that the model or tool was proven error-free.

## Turning the history into tests

The corpus is intended to seed deterministic and randomized regression tests:

1. **Type/submit race:** vary payload length, punctuation, filename suffix length, and inter-event delay; fail if Enter is observed before the exact text.
2. **Silent loss:** inject dropped HID events while returning transport success; require the verifier to detect the mismatch.
3. **Duplicate keys:** inject repeated letters, quotes, and punctuation; require exact readback before commit.
4. **Focus theft:** raise a notification or switch foreground windows midway through a burst; require immediate pause with zero subsequent characters.
5. **Nested desktop identity:** present visually similar host and AVD desktops; fail any action against the wrong layer.
6. **Human concurrency:** inject human input during a draft; require epoch revocation and no clearing or sending.
7. **Wrapped lines and rich text:** test logical-line selection, auto-pairing, Teams bullets, and case normalization.
8. **Panic stop:** preload a long queue, stop it, and prove at the observer that no further HID events arrive.
9. **Approval:** attempt send, delete, publish, cloud mutation, and broad replacement without a fresh action-specific approval; require a hard block.
10. **OCR disagreement:** show confusable glyphs and low-confidence tokens; require corroboration before mutation.

The current watched-typing regressions now cover two of the most dangerous
cross-incident combinations. Both per-key typing and the faster printer take a
fresh pixel sample before every chunk after the first; a clustered change
outside the established field releases input and stops with the exact committed
prefix. Fast prose is never cleared and replayed after an OCR mismatch. At the
burst boundary, every `unverified_*` result blocks Enter, keys, clicks, and
further text; only passive settling/evidence waits may run. `method=print`
cannot bypass that verifier when the production runtime has a watched typer,
and a caller-supplied `no_verify` field is refused before HID. These are
deterministic local protections, not proof that arbitrary notification shapes
or real Office applications are solved.

The best benchmark score is not task completion alone. It is:

- exact text match;
- zero unapproved irreversible actions;
- zero wrong-target input;
- bounded recovery actions;
- time to verified completion;
- observed rather than attempted input delivery;
- correct abstention when state or identity is uncertain.

## Validation

`tests/test_historical_pikvm_incidents.py` validates:

- all 24 session records and 4,453 call counts;
- client and model subtotals;
- unique incident IDs and valid source-session links;
- enumerated severity, category, cause, outcome, and one-shot risk;
- coverage markers for conversations with no reconstructed failure;
- absence of raw typed payload fields, URLs, email addresses, IP addresses, long encoded strings, cloud identifiers, and the supplied VNC endpoint.

`tests/test_historical_pikvm_coverage.py` validates exact critical/high
membership, non-overlap, status counts, explicit limitations, and that every
claimed pytest node exists.

Focused result:

```text
5 passed in 0.05s
```

The implementation-facing typing and submit-boundary regression slices pass
31 and 38 tests respectively. They include normal and fast-path focus theft,
control revocation, no destructive long-prose replay, exact partial-progress
reporting, and ambiguous-prose refusal before Enter.

Pytest's cache provider was disabled because the execution sandbox makes the existing cache read-only.
