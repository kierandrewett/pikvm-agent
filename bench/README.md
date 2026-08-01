# Real-world benchmark evidence

This is the central public scorecard for the PiKVM agent and provider-neutral
harness. It records passing, failing, invalid, and infrastructure-blocked runs.
A row is not a product claim unless its environment, upstream revision, model,
sample size, and evaluator are all shown.

Last updated: 2026-07-31.

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
| Normal chat and research do not silently become computer actions | Target-free contracts cover durable multi-turn chat, ordinary replies without a computer session, one-at-a-time visible MCP tools, local read-only authority, exact approval for all other tools, explicit computer hand-off, per-turn tool attribution, and stable streamed reply identity. A fresh Claude OAuth repeat passed the full five-case chat/research/handoff/approval contract 5/5 in 55.517s with activity in 1–2ms; research visibly called `web.search_text` and cited python.org, while the simulated send-message request stopped at `needs_approval` with zero executions. The identical generated Codex route timed out at its former 90-second deadline, then passed the greeting in 136.220s after its onboarding timeout was corrected to a bounded 180 seconds; the failed attempt remains in the evidence. Codex's earlier full 4/4 route and approval canary established compatibility but poor interactive latency. A separate production-server probe started with no daemon, agent token, observer token, direct-call coordinator, or computer adapter and returned the exact requested Claude OAuth reply with one model call, zero computer events, no session, and `computer_control: disabled`. The first native Anthropic API canary failed authentication before any tool request or target contact and remains in the denominator | Passing live target-free Claude OAuth route; Codex compatible but too slow for interactive default; API credential compatibility pending |
| Read-only screen questions are visible without entering the input loop | A paired live run used the first-party Electron chat against the authorized disposable Windows VM. Both attempts completed with zero keyboard, pointer, action, or approval events and showed first progress in 211–257ms. The original Opus verifier took 47.130s and the run took 56.227s. Routing only independent verification to Claude Haiku reduced verifier time to 18.300s and wall time to 26.663s, a 52.58% reduction. The optimized run never displayed more than one progress row and displayed zero reply-branch controls | Passing n=1 read-only diagnostic; no action-quality claim |
| Direct Claude/Codex/OpenCode calls are visible and operator-controllable | Actual MCP `ClientSession` dispatch test, exact redacted arguments plus durable outcome/latency, path/raw-payload exclusion, fail-closed missing-visibility tests, scoped observer credential, required frame/control/idempotency fields on every HID tool, real browser, 100-call audit at 6.70ms median / 7.54ms p95 | Passing local contract |
| A coding client can submit once while the dedicated harness owns progression | The referenced Claude session used 551 direct PiKVM calls; 21 API contracts prove the replacement managed loop crosses action slices, safe replans and verifier-more-work checkpoints, resumes after exact human approval, recovers internal yields after restart, reconstructs its automatic-resume ceiling from durable history, and refuses overlapping Continue bypass. Operator steering is durable, forces a fresh managed plan, and cannot discard unsettled HID or take control from direct/external drivers. Target-free authenticated Codex, Claude, and OpenCode tasks have completed using only managed start/status calls; the outer client did not drive individual HID actions. Exact generated configs initialize over real stdio for all four clients and preserve their validated client identity | Passing managed loop and target-free outer-client routes for Codex/Claude/OpenCode; supported Gemini and real-computer repeats pending |
| The desktop compatibility launcher does not give ordinary clients raw HID | The built Electron-owned launcher completed a real stdio MCP handshake with exactly five `computer_*` controls and zero `pikvm_*` tools. Managed mode is the default; guarded direct requires an explicit compatibility argument. The desktop now publishes only its reduced agent capability to a stable per-user runtime and revokes only its own generation. Path-free Codex/Claude/Gemini/OpenCode registrations contain no target, harness/runtime path, or token name/value. A separate 3.048-second provider hold showed “Waiting for a response” after 20ms, then one reply, with no branch control, duplicate progress, computer session, input event, screenshot, or remote target | Passing target-free launcher, restart, stdio inventory, runtime lifecycle, and live-progress acceptance |
| Effective client config cannot silently retain a second PiKVM tool surface | Fail-closed audit supports native Codex resolved inventory, Codex TOML/shared project JSON, Claude/Gemini JSON, and legacy/V2 OpenCode JSON/JSONC. A 2026-07-28 read-only audit of the actual ambient state found Codex 0.144.4 and OpenCode 1.14.44 still registered raw PiKVM, Gemini 0.35.3 missing a managed registration, and Claude 2.1.220 ambiguous at user scope; only the explicit repository Claude project scope replaced it with exactly one managed registration. The active-runtime isolated launcher then dry-ran all four installed clients 4/4: native effective inventories contained exactly one managed surface, forwarded no agent capability, changed no persisted configuration, and started no model, MCP server, or target connection. Exiting an executed isolated client is the rollback | Detection and non-persistent managed cutover pass; optional persistent migration remains |
| Managed and direct assurance levels are unambiguous | The reference conversation is explicitly retained as 551/551 direct calls, not reclassified as a managed run. Chromium, Firefox, and WebKit each labelled the direct trace `Guarded direct`, named the launcher-declared caller/provider/model, removed managed assurance and the chat composer, and retained zero horizontal overflow | Passing target-free ownership/UI contract; live computer task pending |
| Computer-use actions remain inspectable while a run is changing | The authenticated Chromium/Firefox/WebKit fixture retained and expanded all 12 actions, loaded 20 action-bound previews per engine, and exposed the exact MCP tool/model route with zero desktop or 390×844 overflow. The held Teams fixture showed the exact text, final Enter, reason, and Allow once/Deny controls without committing input. A later isolated Electron/CDP pass proved that a mixed `computer_start_task` plus `pikvm_*` managed turn renders as one expanded `Computer activity` timeline with the current exact input visible, not as a generic tool-name chain | Passing target-free multi-engine and managed-group UI contracts |
| The operator can inspect the verifier's visual evidence | Authenticated API contracts return the labelled before/after bytes without exposing their local path; static UI contracts cover revision refresh, loading/error state, and blob revocation. A live disposable-Windows diagnostic exercised failed and uncertain visual verification without turning either into success | Passing local/browser contract; live task itself did not pass |
| Long prose/code arrives exactly | Windows transport trials were exact 581/581 and 142/142 characters. A later seeded live boundary probe was 8/8 exact with 0 character errors and 0 duplicated spaces at 17–72 characters, although generic screen OCR was exact in 0/8. A deterministic repro then proved that replaying an apparently missing chunk could duplicate its already-delivered leading space. Typing is now at-most-once: ambiguous delivery stops unverified and never replays text. A 1,000-case stale-readback fuzz emitted every canonical payload exactly once with zero introduced doubled spaces. Receipts retain requested, delivery, emitted, OCR, and evaluated-frame SHA-256 values | Partial; target-free sender integrity passes, fresh live replay and generic OCR do not |
| Raw HID avoids encoded/script transfer hacks | A seeded 1,000-payload corpus caught 800/800 unsafe shapes with 0/200 safe false positives; the public MCP integration also refuses encoded transfer before daemon contact | Passing local syntax gate; explicit byte-verified transfer channel pending |
| Exact-byte virtual-media preparation works | 10/10 builder contracts plus 19/19 transaction/UI/adapter/surface contracts cover mode-0600 media, exact browser approval, rollback, cleanup uncertainty, identity, lease, stop, model-surface exclusion, and explicit unsupported VNC | Passing target-free contract; daemon bridge capability and live target result pending |
| OCR can safely verify arbitrary desktop text | Tesseract is 56.9% selected and 61.4% expected-aware exact; its 800-case routine tier is 71.125% exact while the preserved 200-case confusable stress tier is 0%; PaddleOCR is 78.9% normalized exact; the retrospective known-intent candidate union is 82.7% overall, 97.0% routine, and 25.5% stress on the same 1,000 cases; no confidence threshold supports a 99% lower-bound claim | Failing release gate |
| Model grounding is reliable | Current seeded ScreenSpot-Pro samples are Codex 73/100 and Claude Opus 17/20. An experimental Opus → Haiku correction pass reduced an 18/20 first pass to 7/20; the verifier is now veto-only by default | Diagnostic only; automatic correction failed |
| End-to-end desktop tasks are reliable | Current OSWorld task set is 7/9; full scored-attempt denominator is 8/39 with 11 additional unscored failures. The inactive-screen remediation is 2/6 overall and only 1/2 after its first pass; the latest pass still took 464.33 seconds | Failing release gate |
| A model can autonomously complete routine Office work | Portable Word/Excel contracts and semantic OOXML verification pass local tests. Excel r23 is the first clean canonical pass: the model saved, closed, reopened, and audited the workbook; the host recovered 9,437 bytes and passed 29/29 cell/formula checks. The five-attempt optimization series retains three incomplete/rejected runs and one scorer false negative before that pass. r23 still took 29m 32s, used 52 model calls, and repeated one formula audit; the action-evidence contract was hardened afterward. Word r29 recovered a 16,081-byte DOCX that passed 11/11 checks, but its original runner transaction remains `artifact_failed` | Clean Excel n=1; Word runner still not clean; latency failing product target |
| Windows Agent Arena is supported | 154 tasks discovered; official golden image is absent | Not run |
| Provider choice is portable | Codex and Claude OAuth CLIs live-tested; persistent Codex app-server now reuses the same provider-owned ChatGPT login and returned valid strict-schema output on 42/42 calls, with 37/42 exact, across the published Luna/Terra/Sol diagnostics with zero computer contact. Terra-low was the best first fast-lane candidate: 19/20 exact across two identical ten-case repeats, 4.990s combined median, and 7.474s combined p95. Dedicated-profile Gemini CLI OAuth, native OpenAI Responses, Azure OpenAI API-key/Entra modes, OpenAI-compatible, Anthropic, Gemini AI Studio, and Vertex AI adapters remain protocol-tested with mocks/source contracts. The live Anthropic Messages canary reached the provider with `claude-sonnet-5` but the environment-owned credential was rejected | Partial; Codex app-server OAuth is live and fast enough for task trials, but its 95% diagnostic accuracy is below the release gate and no API credential route has passed live yet |

### Live 50-task Windows campaign

The active disposable-Windows campaign has **18/50 unique accepted passes
(36%)**. Every attempt is screen-recorded, every test ends with a VM reboot,
and a pass is counted only once even when the same task is rerun during
remediation. Production PiKVM was not contacted.

| Category | Passed | Planned | Current state |
| --- | ---: | ---: | --- |
| Observation | 5 | 5 | Complete |
| Calculator | 10 | 10 | Complete |
| Text entry | 3 | 10 | `text-01` through `text-03` accepted |
| Code entry | 0 | 10 | Pending |
| File management | 0 | 5 | Pending |
| Microsoft Excel | 0 | 5 | Pending |
| Microsoft Word | 0 | 5 | Pending |
| **Total** | **18** | **50** | **32 pending** |

The Calculator category is complete. The final temperature-conversion task
visibly produced `23 °C = 73.4 °F`, completed 7/7 actions, and rebooted the VM
after a real screen transition. It took 141.228s before reboot: 75.412s of
provider wait across 15 calls and 62.054s of action execution. Verification
caught both a mode-selection click that initially left Standard mode visible
and an intermediate `3 °C = 37.4 °F` entry before the controller corrected it.
The accepted result is accurate, but this remains far slower than a human.

The first exact text task required seven retained acceptance and speed attempts. v13
reported success but is marked **invalid** because leaving Notepad open after
Save was incorrectly treated as a reopen. v14 and v15 stopped safely on
unresolved replacement confirmations. v16 genuinely saved and reopened the
file but timed out because its in-memory completion gate did not recognize the
verified overwrite transition. v17 passed only after a replacement save and a
later, separately verified Open action. v18 proved that replacing the
pre-populated basename was fixed, but a crop spanning the adjacent Save as type
control caused exact readback to fail closed. v19 localized the filename stem,
performed a fresh OCR read on the refined row, and passed with all 13 actions
completed.

The refined v19 task took 259.245s: 129.658s of provider wait, 123.297s of
action execution, 25 provider calls, and 13 actions at 100% completion
efficiency. The longest action took 33.208s. That is 45.48% faster than v17's
475.492s accepted run. Its mandatory reboot took a further 76.924s and
recorded a real screen transition. Accuracy has crossed this narrow gate;
speed has not. A human can still perform this task materially faster, so the
next campaign slice expands blind text entry while continuing to target
provider and verification call count.

The second text task exercised an em dash, curly quotes, commas, a semicolon,
and ordinary prose. Its first five attempts exposed five distinct failures:
transient Win+R delivery, dropped CP1252 punctuation, a dropped ordinary
letter, downscaled OCR misclassifying an em dash as a hyphen, and an
appropriately blocked bare-Enter Save commit. The accepted v6 run used CP1252
Alt codes only for unsupported direct characters, paced ordinary VNC key
events, recaptured the exact text row at its native 774×42 resolution, and
clicked the visibly labelled Save button. It then reopened `text-02.txt` and
verified all 78 characters. The mandatory reboot surfaced a stable desktop
after a visible transition.

Text-02 still took 279.940s before reboot: 175.644s of provider wait across 28
calls and 149.769s of action execution, with the two lanes overlapping. Its
reboot added 96.101s. This is a real accuracy improvement, not a speed success;
the controller/verifier call count remains the dominant optimization target.

The third text task requires two exact paragraphs with one blank line. Its
first five attempts are retained: restored Notepad state made a blank-area
focus click ungroundable, delayed video stopped after the first eight
characters, and incorrect multi-line guidance conflicted with the
control-character schema. v4 used a deterministic, independently
verified sequence: first paragraph, two non-submitting Shift+Enter line
breaks, then the second paragraph. Exact receipts matched 79/79 and 74/74
characters with identical requested, issued, emitted, and readback hashes.
The file was saved, but v4 is **invalid**, not accepted: an unexecuted
"before reopening" checkpoint was associated with later stale-screen
verification and incorrectly satisfied the reopen gate. No actual Open action
occurred.

Text-03 took 277.952s before reboot: 141.648s of provider wait across 22 calls,
204.741s of action execution, and 22.916s of evidence capture, with overlapping
lanes. Nine of ten attempted actions completed; two same-run recoveries were
needed because the verifier initially lacked visible path evidence. Its
mandatory reboot took 94.193s and observed a real transition. The completion
gate now requires a durable `action.completed` event before any checkpoint can
count as verified. v5 then stopped at a correctly refused `unknown` overwrite
approval: the visible Yes button belonged to a confirmed local Save As
replacement, but pointer classification did not yet inherit that surface
evidence. The same run also reproduced 2–4 second VNC publication lag after
the first text chunk. Confirmed overwrite clicks now retain the
`local_file_edit` category only when the full replacement dialog is visible,
and the at-most-once typer has one additional bounded read-only sample before
declaring focus lost.

v6 is the first accepted text-03 run. It retained the at-most-once failures
from both delayed first chunks, recovered from the visibly delivered prefixes,
saved through the grounded replacement confirmation, invoked a real Ctrl+O
Open dialog, selected the saved file, and independently verified both
paragraphs with one blank line. The task took 380.612s before reboot:
208.161s of provider wait across 34 calls and 243.335s of action execution,
with two autonomous recoveries and 15/18 actions completed. Its mandatory
reboot took 101.101s and observed a real transition. Accuracy now passes this
gate; the extra recovery and model turns make speed an explicit failure.

Text-04 v1 is retained as a failure. The controller issued `1. Observe`
exactly once and repeated native OCR read it exactly with 0.9695–0.9879
confidence, but the independent spacing gate could not verify the single gap:
its geometric path allowed only alphanumeric tokens, so the `1.` list marker
made a correct line permanently unverifiable. The safety gate then correctly
blocked clicks and further typing, but the showcase runner spent 26 provider
calls revisiting the same refusal before the run was deliberately aborted.
The failed run took 281.108s, retained a 359.5s VP9 recording, and completed
its mandatory reboot after a visible transition in 122.768s. The geometric
verifier now has bounded numbered-marker coverage with explicit missing-space
and doubled-space negatives, and unchanged paused errors stop before a third
provider retry.

Text-04 v2 proved that remediation but exposed the next boundary. The first
line and its line break verified successfully. `2. Act` was then emitted
exactly once, but its six-character video delta arrived after the initial
capture. Delayed-frame localization was only attempted once an exact payload
reached eight characters, so the line remained unverified. A guarded Ctrl+Z
cancel removed the grouped Notepad edit, including the previous line, and the
controller then stopped without replaying an unknown draft. The run failed
after 143.924s, 13 provider calls, and 5/6 completed actions; its mandatory
reboot observed a real transition and reached a ready desktop after 97.802s.
The typer now performs bounded delayed-pixel/OCR localization at the existing
four-character exact-text threshold while retaining the eight-character
threshold for declaring focus lost.

Text-04 v3 confirmed that the bounded delayed loop ran, but ordinary
full-screen OCR still missed the visibly present six-character second line.
The run then exposed a separate safety flaw: Escape was treated as cancelling
any unverified non-terminal draft. Escape can dismiss a Windows launcher
field, but it does not remove text already emitted into Notepad. That false
cancellation let the model move on to `3. Verify` without a verified line
break; the follow-up stopped as an unverified focus failure. v3 failed after
221.300s, 18 provider calls, and 5/7 completed actions, then rebooted to a
verified ready desktop in 94.108s. Exact delayed localization now invokes the
precise OCR profile, and editor drafts have their own safety surface: Escape
cannot clear an unverified editor receipt. Text-04 remains pending until both
changes pass a clean live replay.

Text-04 v4 proved that the editor guard now fails closed: after `2. Act` was
emitted exactly once and remained visibly present, every attempted progression
was refused and the controller never typed the third line. Precise full-screen
OCR still missed the small line because the causal pixel locator found two
similarly sized text-line changes—293 pixels at the new text and 274 pixels at
an unrelated status/caret repaint—and deliberately rejected the ambiguous
crop. The failed run took 197.206s before reboot: 135.140s of provider wait
across 19 calls and 55.304s of action execution. Its mandatory reboot observed
a real transition and reached a ready desktop after 107.469s. For exact inputs
of at most 20 characters, the dense locator may now nominate only its strongest
candidate for a subsequent character-for-character OCR check; the default
ambiguity rejection and at-most-once input policy remain unchanged. Text-04
remains pending until that bounded path passes a clean v5 replay.

Text-04 v5 showed why choosing only the strongest dense candidate was still
insufficient. The coarse grid confidently selected Notepad's changing
character-count/status row at `y≈488`, while the actual new `2. Act` line was
at `y≈112`; the dense candidates likewise ranked the status repaint at 298
changed pixels above the text at 234. The typer spent 104.821s repeatedly
reading the wrong crop, returned no observed characters, and emitted no
duplicate input. The editor guard again blocked progression and the identical
paused-error circuit stopped further model retries. v5 took 246.080s before
reboot: 99.188s of provider wait across 16 calls and 141.702s of action
execution. Its mandatory reboot observed a real transition and reached a ready
desktop after 77.617s. Short exact editor input now enumerates at most four
line-shaped causal candidates and selects one only when its own cropped OCR is
an exact character-for-character match with independently calibrated spacing;
single-line fields, explicit regions, and the default ambiguity policy are
unchanged. Text-04 remains pending until that candidate scan passes a clean v6
replay.

Text-04 v6 proved that the candidate scan fixes the critical `2. Act`
boundary: the action completed with matching requested, delivery, emitted,
and observed SHA-256 values in 7.256s, down from v5's 104.821s failed crop.
The next line then exposed a narrower geometry defect. `3. Verify` was emitted
exactly once and OCR visibly returned `2. Act\n3. Verify`; the causal scan had
found the exact new row, but retained its larger changed-pixel box for the
settled read. The previous line therefore contaminated the exact checksum and
the editor guard correctly refused every subsequent line break or payload.
v6 failed after 193.196s before reboot: 115.925s of provider wait across 15
calls and 71.226s of action execution, with 6/7 actions completed. Its
mandatory reboot observed a real transition and reached a ready desktop after
104.803s. A matched causal candidate is now narrowed to the exact OCR row
before settled readback; no substring is carved from the later observation,
and the at-most-once policy remains unchanged. Text-04 remains pending until
that row refinement passes a clean v7 replay.

Text-04 v7 proved the row refinement itself, but exposed confidence drift
between two reads of the same causal row. The exact `2. Act` payload was
emitted once. The delayed candidate scan eventually found its row and
independently verified the visible space; five subsequent settled reads all
returned the exact six characters, but their mean confidence was 0.8949,
0.0051 below the geometric spacing threshold. The receipt therefore remained
empty and the editor guard again blocked progression. v7 failed after
206.436s before reboot: 113.232s of provider wait across 16 calls and 86.325s
of action execution, with 4/5 actions completed. Its mandatory reboot
observed a real transition and reached a ready desktop after 75.904s. The
transaction now retains an exact causal row's already-verified spacing proof
when a later overlapping read still matches every character but only its
confidence has fallen; the proof is reset for every input transaction and
cannot authorize a mismatching or non-overlapping read. Text-04 remains
pending until that bounded evidence handoff passes a clean v8 replay.

Text-04 v8 crossed every checklist-input boundary: all four payloads were
emitted exactly once with `verified_exact` receipts and identical requested,
emitted, and readback SHA-256 values. It then failed in Save As. Native OCR
returned the exact prepared basename inside two labelled rows—`File name:
text-04.txt` and `Save as type: Text documents (*.txt)`—while the bounded
filename parser recognized only the same two values without their Windows
labels. The controller correctly refused Save, dismissed the unverified dialog,
and retried the reversible workflow until the same-run recovery limit stopped
it. v8 failed after 465.937s before reboot: 282.646s of provider wait across 45
calls and 310.821s of action execution, with overlapping lanes and 19/23
actions completed. Three approvals were all scoped to bounded workspace edits.
Its mandatory reboot observed a real transition and reached a ready desktop
after 82.716s. Exact safe-filename readback now recognizes only the two known
Windows label/value rows; a changed basename, label, type row, suffix, or extra
row remains unverified. Text-04 remains pending until that parser passes a
clean v9 save-and-reopen replay.

Text-04 v9 proved that labelled Save As readback now works: the exact
`text-04.txt` value was emitted once, independently read back with matching
requested, emitted, and observed hashes, and the grounded Save action
completed. A real Ctrl+O action then opened the file picker, selected the
saved file, and a later screen showed the four requested lines in foreground
Notepad. The run still failed its stronger acceptance contract. Immediate
post-click video lag left the first Open verification stale, and later visual
checks could not prove the full path; the controller and verifier consumed 61
provider calls and exhausted four same-run recoveries. The run took 485.353s
before reboot: 346.155s of provider wait and 310.728s of action execution,
with 18/20 actions completed. Its mandatory reboot observed a real transition
and reached a ready desktop after 190.309s. One receipt also exposed an
evidence-integrity defect: `1. Observe` was labelled `verified_exact` after
OCR segmented its single visual row as `1.\nObserve`, even though the
receipt's own exact-byte hash correctly disagreed. Text-04 remains pending;
an exact status must now require matching bytes, and the campaign needs
observer-owned saved-path/content evidence instead of repeated visual guesses.
The follow-up remediation makes that boundary fail closed: a
`verified_exact` result with different delivery/readback bytes is downgraded
and blocks every later active input. A prior exact causal row may canonicalize
only a later whitespace-segmented OCR read of the same intended glyph sequence,
same transaction, and overlapping region. The reopen gate also retains the
last completed action across a subsequent grounding refusal, so a delayed
remote-video frame can verify that original transition without crediting an
action that never reached HID. These changes pass 318 focused typing, burst,
and managed-agent tests; text-04 still remains pending until a clean live
replay.

Text-04 v10 proved the exact-receipt remediation live. All four numbered lines
and `text-04.txt` were emitted once with matching requested, emitted, and
observed hashes. Save, overwrite confirmation, Ctrl+O, and the file selection
also reached the disposable VM, and a later frame showed the reopened four-line
document. The run still failed: immediate action verification repeatedly saw
the pre-click frame, so neither Save nor overwrite acquired durable verification
in the current run. The completion gate correctly refused to credit the older
file. v10 took 597.410s before reboot: 443.365s of provider wait and 441.687s
of overlapping action execution across 87 calls, with 15/16 actions completed.
Its mandatory reboot observed a real transition and reached a ready desktop
after 77.360s. Failed or uncertain action verification now performs a bounded,
read-only frame refresh and recalls the verifier once only if the image bytes
changed. A delayed frame also invalidates the speculative next controller;
unchanged pixels trigger no extra model call. The combined remediation passes
321 focused typing, burst, and managed-agent tests. Text-04 remains pending
until this bounded delayed-frame handoff passes a clean live replay.

Failure-inclusive metrics, canonical campaign digests, the 18 accepted task
IDs, and the VP9 recording/poster hashes are retained in
[`codex-50-progress.json`](results/2026-07-31/live-vnc/codex-50-progress.json).
The complete 50-task manifest is
[`codex-50-tasks.yaml`](codex-50-tasks.yaml).

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

### Effective coding-client route audit

The 2026-07-28 audit answers a narrower question than the provider or computer
benchmarks: if each installed coding CLI were opened normally right now, would
it necessarily enter the first-party managed computer loop? The answer is no.

| Client | Installed version | Ambient/effective result | Safe managed default |
| --- | --- | --- | --- |
| Codex | 0.144.4 | one raw PiKVM registration | No |
| Claude Code | 2.1.220 | ambiguous user registration; this repository's later project scope replaces it with one managed registration | Project only |
| Gemini CLI | 0.35.3 | no managed PiKVM registration | No |
| OpenCode | 1.14.44 | one raw PiKVM registration | No |

The audit was read-only: it did not rewrite client state, start an MCP server,
call a model, restart the installed desktop, or contact a computer. The result
is why the first-party desktop—not an ambient coding CLI—is the primary agent
surface. The new stable `active-managed-mcp` boundary makes a deliberate
cutover path-free and fail-closed, but it does not pretend an installation has
happened. Machine-readable evidence:
[`effective-client-route-audit.json`](results/2026-07-28/safety/effective-client-route-audit.json).

The preferred compatibility path now needs no harness argument:
`harness client-launch --client <name>` resolves the healthy desktop's active
runtime, injects it only into one isolated client process, audits the client's
native effective MCP inventory, and leaves persisted state untouched. Dry-runs
against the disposable lab runtime passed 4/4 for Codex, Claude, Gemini, and
OpenCode. Every inventory had exactly one managed PiKVM surface and zero
forwarded agent-token environment names. No model, MCP process, readiness
request, or computer target was contacted:
[`active client launch dry-run`](results/2026-07-28/safety/active-client-launch-dry-run.json).

The same isolated desktop was reloaded over CDP after a composer copy polish.
Its model control now renders `Opus + Haiku` instead of the internal
`opus / opus / haiku` role tuple. The accessible label and tooltip still name
the exact Reasoning, Acting, and Checking providers. At the real 916×540
Electron viewport the DOM had zero branch counters and zero horizontal
overflow. No screenshot, input action, installed-app restart, or production
target was involved:
[`compact model route audit`](results/2026-07-28/ui/compact-model-route-audit.json).

### Target-free OAuth assistant repeat

A fresh secret-free harness configuration selected the logged-in Claude and
Codex CLI adapters and packaged read-only web MCP. It had no daemon, VNC,
PiKVM, Office, or computer adapter. Claude passed all five normal-assistant
cases:

| Case | Result | Wall time | First activity | Visible tool/effect |
| --- | --- | ---: | ---: | --- |
| Greeting | Pass | 7.909s | 2ms | ordinary reply |
| General question | Pass | 5.754s | 1ms | ordinary reply |
| Official-source research | Pass | 21.301s | 1ms | `web.search_text`; python.org citation |
| Computer hand-off | Pass | 11.117s | 2ms | explicit hand-off to a recording sink |
| Consequential send canary | Pass | 9.436s | 1ms | held at `needs_approval`; zero execution |

The first Codex greeting attempt reached the generic 90-second provider
deadline and was retained as a timeout. Current Codex OAuth latency evidence
already had a p95 above that deadline, so newly generated Codex routes now use
a bounded 180-second deadline and remain behind the faster routes. Repeating
the exact canary passed in 136.220s with first activity in 2ms. This repairs a
false transport failure; it does not make Codex an acceptable interactive
default.

All three reports attest zero computer contact. Evidence:
[`Claude 5/5`](results/2026-07-28/providers/oauth-assistant-claude-repeat.json),
[`Codex 90s timeout`](results/2026-07-28/providers/oauth-assistant-codex-timeout.json),
and
[`Codex 180s pass`](results/2026-07-28/providers/oauth-assistant-codex-180s-repeat.json).

The environment exposed one API credential name, for Anthropic Messages. A
fresh greeting reached the provider and was rejected as
`authentication-failed` in 877ms, before any tool request and with zero
computer contact. No other API credential prerequisite was present. This is a
current 0/1 API result, not adapter compatibility evidence:
[`Anthropic API authentication failure`](results/2026-07-28/providers/anthropic-api-assistant-auth-failure.json).

### Persistent Codex app-server fast-lane diagnostic

On 2026-07-29 a new first-party provider adapter reused the saved Codex
ChatGPT login through one persistent `codex app-server` process. Every request
used an ephemeral read-only thread, disabled tools and web access, attached
only the deterministic synthetic screen, and required strict structured
output. No daemon, VNC, PiKVM, HID, email, or chat target was available.

The first identical three-case comparison used priority service and low
reasoning unless noted:

| Route | Exact | Median | p95 | Schema failures |
| --- | ---: | ---: | ---: | ---: |
| Luna low | 1/3 | 5.204s | 5.405s | 0 |
| Luna medium | 2/3 | 5.742s | 6.370s | 0 |
| Terra low | 3/3 | 5.318s | 6.337s | 0 |
| Sol low | 3/3 | 5.527s | 6.856s | 0 |

Terra-low and Sol-low then ran the same ten blind cases. Both scored 9/10,
but Terra was materially faster: 4.712s median and 10.135s p95 versus Sol's
6.932s median and 10.549s p95. A second identical Terra repeat scored 10/10.
Across its two repeats Terra is therefore 19/20 exact with a 4.990s combined
median and 7.474s combined p95. The misses were single-character
transcription errors in deliberately confusable verification codes. This is a
small diagnostic and still fails a release-quality exactness gate; it is
enough to select Terra-low for the next disposable-computer task trial, not to
claim reliable autonomous control.

Failure-inclusive evidence:
[`Luna-low n=3`](results/2026-07-29/providers/codex-app-server-speed-n3.json),
[`model comparison n=3`](results/2026-07-29/providers/codex-app-server-model-comparison-n3.json),
[`Terra-low n=10`](results/2026-07-29/providers/codex-app-server-terra-low-n10-sequential.json),
[`Terra-low repeat n=10`](results/2026-07-29/providers/codex-app-server-terra-low-n10-concurrency4.json),
and
[`Sol-low n=10`](results/2026-07-29/providers/codex-app-server-sol-low-n10-sequential.json).

### Literal screen-observation fast path

On 2026-07-28 the first-party Electron chat ran the exact request
`what is on the screen` against the authorized disposable Windows VM before
and after the literal read-only routing fix. Both runs completed with zero
keyboard, pointer, approval, file, settings, or communication actions. The
production PiKVM target was not contacted.

| Signal | Before | After |
| --- | ---: | ---: |
| Final state | completed | completed |
| Model path | assistant → reasoner → controller → verifier | verifier |
| Model calls | 4 | 1 |
| Model-active time | 141.188 s | 23.580 s |
| Total wall time | 142.344 s | 25.176 s |
| Running progress rows | not retained | 1 |
| Reply-branch controls | not retained | 0 |
| Computer input events | 0 | 0 |

The optimized path is 5.654× faster in this pair, an 82.31% wall-time
reduction. The clean Electron/CDP run showed one user turn, one assistant turn,
one live progress row labelled `Waiting for a response` with `haiku`, no reply
versions, and an empty composer after send. Ambiguous prompts such as
`Did it work?` still go through the normal chat model unless the conversation
already has computer context.

This is a passing n=1 read-only diagnostic, not a general model-quality or
computer-action claim. The failure-inclusive record is
[`literal-screen-observation-fast-path.json`](results/2026-07-28/live-vnc/literal-screen-observation-fast-path.json).

### Cached VNC observation and durable task selection

On 2026-07-29 the exact request `what is on the screen` was repeated against
the authorized disposable Windows VNC VM after moving the chat to Codex
app-server. The reproduced baseline took 9.089 seconds: 2.675 seconds to open
and capture the computer and 6.380 seconds in the Terra-low visual verifier.

The VNC preview was also requesting a serialized RFB capture approximately
every 200 milliseconds. A 750-millisecond read-only frame cache now coalesces
preview and agent consumers. Every keyboard, pointer, wheel, or print operation
invalidates that cache before another frame can be returned. The observation
verifier also receives a compact prompt and high-detail image instead of the
general action-verification context.

| Signal | Reproduced baseline | Cached high-detail median |
| --- | ---: | ---: |
| Completed accurate screen descriptions | 1/1 | 3/3 |
| Total wall time | 9.089 s | 6.342 s |
| Visual model time | 6.380 s | 4.916 s |
| Non-model hand-off/capture time | 2.709 s | 0.293–1.527 s |
| Keyboard/pointer events | 0 | 0 |

The three retained high-detail runs took 5.209, 6.342, and 7.791 seconds
end-to-end. All three named Excel, Calculator's visible `442`, and Phone Link.
Median wall time fell 30.22%; the remaining dominant cost is the 4.815–7.528
second provider call. A low-detail trial was explicitly rejected: although it
reduced visual input tokens, it paused without identifying the screen.

Task identity was tested separately through Electron CDP without a screenshot
or computer input. Reloading the live chat retained the exact selected run ID
and restored its prior result. Frontend regressions also prove that a temporary
run 404 sends zero replacement-create requests. New-run creation now carries a
session-persisted request ID; replaying it returns the original durable run and
invokes the assistant once.

This is a read-only n=3 latency diagnostic and persistence regression, not a
general computer-action score. The failure-inclusive record is
[`screen-observation-cache-and-state.json`](results/2026-07-29/live-vnc/screen-observation-cache-and-state.json).

### Read-only fast-verifier pair

On 2026-07-27 the first-party Electron chat ran the same explicit read-only
screen-description task twice against the authorized disposable Windows VM.
Both runs acquired a fresh frame and completed with zero keyboard, pointer,
computer-action, or approval events. The production PiKVM target was not
contacted.

| Signal | Opus verifier | Haiku verifier |
| --- | ---: | ---: |
| Final state | completed | completed |
| First visible progress | 211 ms | 257 ms |
| Assistant hand-off | 6.771 s | 7.336 s |
| Independent screen verification | 47.130 s | 18.300 s |
| Total wall time | 56.227 s | 26.663 s |
| Maximum simultaneous progress rows | 1 | 1 |
| Reply-branch controls | 0 | 0 |
| Computer action events | 0 | 0 |

This is a 52.58% wall-time reduction for one paired diagnostic. It supports
using the faster route for independent screen verification while retaining
Opus for the assistant hand-off and as a fallback. It is not a model-quality
ranking and says nothing about Office or input reliability. The failure-
inclusive machine-readable record is
[`read-only-fast-verifier.json`](results/2026-07-27/live-vnc/read-only-fast-verifier.json).

### Calculator controller-effort pair

On 2026-07-28 the first-party Electron chat ran the same bounded Calculator
task twice against the authorized disposable Windows VM. Both runs used Opus
for assistant/reasoner, Haiku for controller/verifier, produced the correct
displayed result `442`, and kept every input inside Calculator. The only model
configuration change was an explicit Claude CLI `low` effort override for the
controller and verifier in the second run.

| Signal | Default CLI effort | `low` controller + verifier |
| --- | ---: | ---: |
| Final state / display | completed / `442` | completed / `442` |
| Wall time | 118.195 s | 251.432 s |
| Model calls / active time | 4 / 106.546 s | 8 / 227.617 s |
| Controller calls | 1 | 3 |
| Verifier calls | 1 | 3 |
| HID bursts / exact inputs | 1 / 8 | 3 / 8 |
| HID-active time | 9.329 s | 21.408 s |

The forced-low run took 2.127× as long and split one reversible eight-input
sequence across three controller/verifier rounds. That is a failed
optimization for this task, not evidence that low effort is always worse. The
harness now forwards an explicitly configured Claude CLI effort, but generated
defaults do not force one.

The same increment fixes live tool visibility. A managed turn containing
`computer_start_task` and raw `pikvm_*` calls now uses the computer-specific
timeline. Its collapsed count reflects low-level inputs rather than wrapper
calls; while active it opens automatically and shows a compact semantic
sequence. An isolated authenticated Electron/CDP fixture showed one
`Computer activity` group, `64 inputs`, the current click, and zero generic
managed-computer tool chains. No screenshot or remote target was used for that
UI check.

The failure-inclusive record, route latencies, safety scope, UI DOM results,
and limitations are in
[`calculator-controller-effort-pair.json`](results/2026-07-28/live-vnc/calculator-controller-effort-pair.json).

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

## Live Word acceptance iterations

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
them. The composer says `Managed harness` only when the harness owns the
reason-act-verify loop. A selected `direct_mcp` trace instead says
`Guarded direct`, names the actual declared client/provider/model, hides
managed model routing, and removes the chat composer. `New managed task` starts a
separate harness-owned run instead of silently taking over historical direct
work. Managed actions and opt-in guarded direct calls are committed to SQLite
before HID execution. Labelled before/after images and quiet adapter/daemon logs
are retained with managed runs. The latest labelled comparison is also fetched
through an authenticated, no-store, path-free endpoint and shown as the primary
Screen change evidence. Completed clicks also show a marked crop from the
pre-action half of that same retained evidence, so the operator can recognize
the clicked control without interpreting coordinates. Goal, frame trace, and
model route sit in a compact Details disclosure; Raw and exact Input are
separate disclosures and stay closed for routine verified actions.
The JSON files linked below are derived from those durable records; the UI is
not a separate, less-auditable execution path.

The managed composer also names its authenticated, configured machine before
send. The identity is independent of selected-run observations, so starting a
new task cannot replace it with a historical alias. `configured` stays visible
at effective 200% reflow, while the accessible description states that
reachability is checked only when computer work begins. The Computer sheet
names both the MCP connection and machine with no active session, and computer
receipts retain the full `pikvm_*` tool name. An isolated 458×270 Electron/CDP
audit measured zero document/composer overflow and zero branch counters, with
no screenshot, model, VNC, PiKVM, production daemon, or HID contact. See
[`composer-connection-visibility.json`](results/2026-07-27/ui/composer-connection-visibility.json).

Provider rows distinguish saved CLI login, API-key environment, bearer-token
environment, and CLI bearer-token ownership. Command-backed routes name only
the executable (`az` or `gcloud`), never the credential, token environment, or
command output. They separately show the configured model alias, last model
reported by a successful call, and the latest blind provider-conformance exact
count plus median/p95 latency. If an accurate acting primary still exceeds the
five-second fast-path budget, the Models sheet says so explicitly and directs
the operator toward a low-latency API route. If a low-latency route is not
character-exact, it is explicitly marked unsuitable for primary computer input.

The chat workspace now exposes this as a contextual Models sheet. Its
authenticated catalog comes from the same canonical ten-adapter backend
contract, while the configured-account view shows the reasoning, acting, and
checking routes, primary/fallback position, readiness, authentication owner,
coarse success/latency, and conformance state. It never renders raw
readiness/provider errors or credential source paths. Its connection form is
secret-free: API routes accept an environment-variable name, never the
credential value, while CLI routes retain provider-owned sign-in. The composer
now shows the effective model for all three
roles before send, and an active run exposes its durable route as locked rather
than silently adopting later settings changes. The additive flow refuses alias
replacement, writes mode-0600 configuration atomically, and does not silently
route a newly configured provider. Fifty-five frontend and 1,106 Python
contracts pass at the exact published source commit; no provider or computer
was contacted. See
[`provider-connections-and-click-targets.json`](results/2026-07-27/ui/provider-connections-and-click-targets.json).
The corresponding ownership and direct-click audit is
[`managed-direct-control-separation.json`](results/2026-07-27/ui/managed-direct-control-separation.json).
Its refreshed Electron/CDP pass names the outer client, provider, and model in
the header and per-action audit; keeps the click result explicitly unverified;
loads the retained 1280×720 pre-action image; removes managed model routing,
the writable composer, and branch counters from the direct trace; and retains
zero horizontal overflow at normal width and effective 200% reflow. No VNC,
PiKVM, production daemon, or model API was contacted.

The authenticated workspace now also has a repeatable three-engine gate.
Playwright 1.61 ran the same 1,200-event fixture in Chromium 149, Firefox 151,
and WebKit 26.5. Every engine expanded 12/12 computer actions, loaded 20
action-bound previews, displayed the exact `pikvm_run_burst` tool and
controller route, exposed both provider-owned OAuth and environment-owned API
routes, and kept the held approval controls reachable. The direct fixture
showed `Guarded direct`, `claude-cli`, `anthropic-oauth`, and `opus`; managed
assurance and the writable composer were absent. Desktop and 390×844 document,
conversation, and action overflow were all zero. There were no page errors,
console errors, external requests, approval submissions, or computer inputs.
The same gate now covers an ordinary non-computer MCP result: all three engines
showed its concise result count, exact query, selecting provider/model, and
zero-overflow argument summary; the complete arguments and unmodified raw
result remained available only after opening `Details`.

This audit found one product defect before the final run: direct mode placed
its ownership badge in the composer toolbar even though direct runs
deliberately hide that composer. The badge now lives in the persistent header.
The audit also stopped assuming fixture-internal credential wording and waits
for asynchronously loaded provider routes, so it asserts the actual rendered
ownership contract rather than implementation detail. Chromium, Firefox, and
WebKit completed in 3.796s, 8.067s, and 6.857s. WebKit ran in Playwright's
official Noble image because the Fedora host does not provide its Ubuntu-linked
library versions. This remains synthetic, target-free UI evidence; it does not
measure live-frame decode, memory, a model, or a computer task. See
[`cross-browser-chat-workspace-audit.json`](results/2026-07-27/ui/cross-browser-chat-workspace-audit.json).

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

The first failed two-provider attempt remains published rather than being
discarded: Codex failed before its model call in 145ms because the CLI's SQLite
state landed in a read-only location, while Claude reached the 90.113-second
runner timeout. The adapter fix moved Codex runtime state into a writable
ephemeral sibling directory while leaving authentication CLI-owned.

The approved target-free rerun now provides a current same-input comparison.
With seed `20260727`, Codex account-default and Claude Opus each returned strict
schema-valid, character-exact text for all three blind screens: 6/6 total,
zero failures. Claude measured 15.693s median / 16.461s p95; Codex measured
105.739s median / 114.879s p95. Claude was therefore 6.7× faster on this
vision/schema task, though even its latency is far above the five-second
fast-controller budget. The provisional two-CLI route puts Claude before Codex
for acting and checking, keeps Codex as fallback, and continues to put
low-latency API/gateway providers ahead of both when configured. This benchmark
does not rank general reasoning quality and is not presented as proof that
Claude is the best reasoner. The scheduler now permits cross-provider
concurrency while limiting each provider to one in-flight case, preventing
self-contention from distorting comparisons. The six calls used only generated
PNGs and contacted no daemon, VNC server, PiKVM, HID device, or computer.

See the current
[`live-codex-claude-provider-conformance.json`](results/2026-07-27/providers/live-codex-claude-provider-conformance.json)
and the retained historical
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

A loopback-only synthetic workspace has now completed the missing effective
200% reflow audit. A requested 458×720 outer viewport rendered at 416×655 CSS
pixels. The expanded managed transcript retained all 12 computer actions,
loaded 10/10 screen previews, kept the model route and one active progress
indicator visible, and measured zero horizontal overflow across the document,
conversation, tool content, and every action row.

The held Teams-send fixture exposed a real first-run defect: after responsive
reflow, `content-visibility: auto` left the current assistant message at its
230-pixel intrinsic placeholder despite 491 pixels of content, so the approval
controls were outside the available scroll range. The repair keeps that
virtualization only on older messages and closes the mobile task drawer before
loading the selected task. The repeated run laid the current message out at
521/521 client/scroll pixels. Exact 47-character text, the final `ENTER`, the
external-side-effect reason, Allow once, and Deny remained bound to the held
action. The controls needed 53 pixels of scroll and had 298 pixels available;
the composer send remained disabled and no approval was submitted. No VNC,
PiKVM, production daemon, or model API was contacted. See
[`computer-action-timeline-visual-audit.json`](results/2026-07-27/ui/computer-action-timeline-visual-audit.json).

The current production assistant-ui/shadcn workspace assets are **1,235,197
bytes total** including local fonts: 1,227 bytes HTML, 1,078,642 bytes
JavaScript, 109,416 bytes CSS, and 45,912 bytes of local fonts. Gzip-9 output
summed per file is 327,565 bytes for JavaScript and 18,121 bytes for CSS.
Release regressions cap
every asset at 1.1 MB, the total at 1.25 MB, the initial app gzip at 250 KiB,
the initial static JavaScript imports at 300 KiB, and CSS gzip at 24 KiB. This
is materially larger than the retired hand-built console and remains within
the explicit current envelope; the old 128 KiB claim no longer describes the
shipped chat workspace.

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

The original computer-action receipt proved source screen, bounded input,
delivery, and independent screen-check lineage. It has since been distilled:
a completed action is one quiet row; opening it shows the authenticated
before/after image; Audit contains only goal, input, frame trace, and model
route; raw JSON is another closed disclosure. Typed payload/read-back,
approval, failure, and uncertainty still expand because they require attention.
During a long live sequence, earlier completed actions remain in the durable
trace but only the current input is rendered; the full history returns when the
sequence finishes or is later inspected.
The original target-free contract evidence remains in
[`computer-action-receipt.json`](results/2026-07-27/ui/computer-action-receipt.json).

The tailored computer-use pass remains action-specific. Typing retains its
exact payload, read-back, focus state, edit distance, retries, and input
fingerprint; keys use individual keycaps; scrolling names direction and step
count. Single successful pointer actions no longer repeat their coordinates in
a miniature map after the header already stated them. Running, held,
committed, verified, unverified, safely refused, failed, and cancelled states
still have text and icon treatment, not colour alone. Raw transport errors stay
in diagnostics. Historical contract evidence is in
[`computer-use-chat-controls.json`](results/2026-07-27/ui/computer-use-chat-controls.json).

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

The first clean canonical Excel result now exists. On 2026-07-28, r23 used
Claude Opus for reasoning/control and Haiku for independent visual
verification against the authorized disposable Windows VM. The model saved to
a fresh runtime path, closed Excel, reopened the new workbook from the visible
Recent list, and audited the disk-backed copy. The observer returned a
9,437-byte XLSX with SHA-256
`1fad889b048aa7a6974588862c734752b5d057b4a0341237baf8a1b7f9266782`;
the host parser passed all 29 worksheet, value, and formula checks. No endpoint
is retained in the checked evidence.

This was attempt five, not a first-try success:

- r19 timed out after a dark-theme filename edit was falsely classified as
  focus loss;
- r20 timed out while the model over-audited before Save As;
- r21 visibly saved and reopened the workbook, but the UI's ambiguous `Deny`
  control terminated the run before artifact capture;
- r22 completed and returned a valid 9,439-byte XLSX, but the scorer compared
  Excel's serialized `20.399999999999999` literally with `20.4`; the unchanged
  file passed 29/29 after the bounded numeric-equivalence fix;
- r23 produced the clean runner pass.

r23 completed 23 of 25 checkpointed actions for 92.0% completion efficiency
with no provider failures, fallbacks, schema repairs, or safety downgrades.
The exact 67-character Save As path was emitted once with matching requested,
delivery, issued, and emitted hashes and zero replay; generic screen OCR could
not read the constrained field, so the agent verified the resulting title
instead. Wall time was 1,772.382 seconds. Controller latency was 25.329s median
/ 40.061s p95; verifier latency was 32.745s / 52.132s. This is correct but not
yet responsive enough for a production desktop agent.

The run also exposed one subtle verifier failure: an earlier B8 click was
reported as verified even though the evidence proved selection but not the
requested formula-bar text. A later controller therefore repeated the audit.
The verifier contract now carries a structured assessment for every
action-level evidence item and fails closed when any item is missing,
unsatisfied, or unsupported. That correction is regression-tested but was
landed after r23, so it is not retroactively counted in the live pass.

The complete failure-inclusive series, model lanes, sender receipt, artifact
hash, limitations, and per-attempt metrics are checked in
[`office-excel-live-acceptance.json`](results/2026-07-28/live-vnc/office-excel-live-acceptance.json).

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

Claude Code 2.1.220 then completed the same authenticated outer-client
boundary. All six compatibility attempts are retained: missing stream
verbosity failed before a run; two CLI tool-filter shapes hid Claude's deferred
MCP tools; the first dynamic-discovery run passed; an unsupported environment
scrub removed access to client-owned OAuth; and the final isolated run passed
in 16.944 seconds. The final client used only `computer_start_task` and
`computer_status`; the harness recorded 22 events, three deterministic inner
model calls, one bounded click, and one verification revision. The launch
inherits no ambient API key or unrelated secret, starts in an empty temporary
workspace, persists neither session nor prompt history, and preapproves only
the four non-destructive managed controls. Abort remains prompt-gated.

OpenCode 1.14.44 then passed after two retained preflight failures. The first
omitted the disposable agent credential and failed closed. The second exposed
a launcher portability defect: the user-facing wrapper resolved its real
binary through `HOME` after `HOME` had become the process-private profile. The
fixed launcher resolves that client-owned binary first while keeping OpenCode
config, cache, state, data, plugins, and MCP inventory isolated. The final
20.155-second run—including a first-run database migration—used only
`pikvm_computer_start_task` and `pikvm_computer_status`, and produced the same
22-event verified harness run. OpenCode's stream did not attest the concrete
outer model, so the report leaves it unreported.

Gemini CLI 0.35.3 remains a failure. Its native effective-settings loader
proved one allowed managed MCP, deny-all/allow-managed admin policy, disabled
extensions/skills/hooks/context, no permanent approval, and an empty workspace.
An unauthenticated dedicated profile exited before model work. A disposable
profile linked to the CLI-owned OAuth files then registered the MCP server, but
the external service rejected this client version for the authenticated
individual Code Assist tier before any model response, tool call, or harness
run. The first authenticated attempt also exposed a shared-cache write; the
fixed runtime now supplies disposable home and XDG cache/config/data/state
roots. The post-fix failure took 6.627 seconds and wrote no shared cache.

A separate first-party chat run exercised the opposite boundary: the harness
itself called a live Codex account route for reasoner, controller, and verifier.
It completed and independently verified the target-free click, but took
336.916 seconds. The three model calls consumed 336.753 seconds—99.95% of wall
time—while the guarded action transport took 1 ms. This is direct evidence for
per-role routing rather than one heavyweight model everywhere: use a faster
controller, reserve stronger reasoning/checking models for the steps that need
them, and retain explicit fallbacks. Neither result contacted VNC, PiKVM, the
production daemon, or any computer target.

The chat-first assistant now has its own live-provider acceptance rather than
borrowing a vision-schema benchmark. The current Claude OAuth run passed all
five fixed tasks in 42.804 seconds. Greeting and ordinary question replies took
9.303 and 5.437 seconds without a tool or computer session. Sourced research
used one visible `web.search_text` MCP call, cited `www.python.org`, and
completed in 15.907 seconds. The explicit screen request became a computer
hand-off in 5.188 seconds, but terminated at the runner's recording sink with
`computer_target_contacted: false`. A fifth simulated `lab.send_message`
request reached `needs_approval` in 6.970 seconds with the exact recipient/body
held locally and zero consequential tool executions. The canary has no email,
Teams, VNC, PiKVM, daemon, or other external transport. The first durable
activity event appeared within 1–2ms on every case, independently of the
provider's response time. The mode-0600 report stores no prompt, reply, tool
arguments, result body, credential, or computer endpoint.

The first Codex OAuth attempt is also retained because it exposed a real
cross-provider defect: Codex's strict output-schema transport rejected the
assistant tool call's open-ended argument object. The provider wire format now
uses a closed object whose `arguments_json` field is decoded and validated at
the host boundary. The corrected Codex run passed the same four cases 4/4, but
took 549.453 seconds: 110.838 seconds for the greeting, 97.566 seconds for the
ordinary question, 230.868 seconds for one visible `web.search_text` research
call, and 110.181 seconds for the target-free computer hand-off. That is 10.3×
Claude's total wall time on this fixed contract. It proves compatibility after
the schema repair; it does not make Codex suitable as the default interactive
chat or tool route. Codex separately passed only the new consequential-tool
canary in 104.254 seconds: one exact request, `needs_approval`, zero tool calls,
and zero consequential executions. This proves the approval boundary is
host-owned on both tested OAuth routes; it does not prove a real email or
messaging integration.

## Current headline

<!-- pikvm-scorecard:start -->
_Generated from checked JSON evidence as of 2026-07-28. Manifest `sha256:d58fdcaefbdb`; run `pikvm-agent harness scorecard --check` to detect drift._

| Suite | Route | Cases | Result | Median / p95 | Wall | Status | Evidence |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Independent emergency stop | Explicit CLI → isolated daemon | 7 contracts + 1 live stop | All contracts passed; 1/1 session halted | 0.34s end-to-end | 0.34s | Passing local safety contract | [JSON](results/2026-07-25/safety/emergency-stop-audit.json) · `sha256:683bfec1c505` |
| HID payload-shape gate | Raw burst + MCP preflight | 1,000 payloads / 2 test nodes | 1,000/1,000; 800 unsafe refused; 200 ordinary allowed | Pre-HID | — | Passing local syntax gate; explicit transfer pending | [JSON](results/2026-07-25/safety/hid-payload-shape-gate-2026-07-26.json) · `sha256:debef459a6fb` |
| Read-only media builder | Target-free exact-byte ISO | 10 contracts | 10/10; 2/2 files exact; 6 unsafe names refused | — | — | Passing target-free builder; attach transaction pending | [JSON](results/2026-07-25/safety/msd-media-builder-2026-07-26.json) · `sha256:7351cb177f5c` |
| Virtual-media transaction | Target-free approval/rollback state machine | 19 contracts | 19/19; 11 durable states; daemon bridge exposed: no | — | — | Passing target-free transaction contract; physical bridge gated | [JSON](results/2026-07-25/safety/virtual-media-transaction-2026-07-26.json) · `sha256:7bd38797d675` |
| Windows VNC physical-loop diagnostic | Deterministic MCP trials; no model | 7 trials | 581 prose + 142 code characters exact; overall failed | See per-trial JSON | 336.17s | Failing diagnostic | [JSON](results/2026-07-25/windows/live-vnc-observer-iteration.json) · `sha256:5c7bab3e4abd` |
| Live exact input read-back | Disposable Windows VM via isolated MCP | 1 diagnostic / 72 characters | Requested = issued = OCR SHA: yes; helper exact: yes; 0 repeated spaces | Not isolated | 55s | Passing n=1 diagnostic; generic OCR still failing | [JSON](results/2026-07-27/live-vnc/typing-exact-readback-case72.json) · `sha256:b737a799b1d6` |
| Whitespace input integrity | Canonical delivery → emitted payloads → OCR text → captured frame | 98 focused + 1,220 full Python + 130 frontend; 1,000 stale reads | 1,000/1,000 emitted exactly once; 0 introduced doubled spaces; 0 retries; newline boundary 1 space | 243,429-byte gzip JavaScript | Target-free contract | Passing exact_visual_readback sender/screen contract; OCR guest ACK: no; fresh live replay pending | [JSON](results/2026-07-27/ui/whitespace-input-integrity.json) · `sha256:7ba61dd8b7ba` |
| Live VNC target contention | Runtime target → process lease → RFB | 40 focused + 1,199 full Python / 1 skipped + 1 live contention gate | Second VNC connection: no; input sent: no; private report mode 0600; endpoint retained: no | 1,550ms | Authorized disposable-target refusal | Passing live fail-closed contention gate; observer identity still pending | [JSON](results/2026-07-27/safety/vnc-target-lease-live-refusal.json) · `sha256:b6e61a5af30b` |
| Direct MCP visibility bridge | Guarded local SQLite/ASGI | 100 calls + 10 contracts | Contracts passed; 143.8 calls/s | 6.70ms / 7.54ms | 695ms | Local diagnostic | [JSON](results/2026-07-25/ui/direct-mcp-visibility-audit.json) · `sha256:aa923113856c` |
| Managed/direct control separation | assistant-ui ownership + click evidence | 93 frontend + 1,191 Python; 551 audited reference calls | 93/93; 1,191 passed / 1 skipped; reference route 551 direct / 0 managed; 0px normal / 0px 200% overflow | 239,916-byte gzip JavaScript | Target-free browser audit | Passing visible ownership contract; live computer task pending | [JSON](results/2026-07-27/ui/managed-direct-control-separation.json) · `sha256:650cf26af2f5` |
| Live provider and pre-HID action visibility | SSE lifecycle → exact checkpoint → guarded HID | 105 frontend + 1,193 full Python / 1 skipped; 7 provider phases | 2 streamed events / 0 per-event refetches; exact action held 300ms before HID; 30 browser samples / 1 simultaneous progress | 241,373-byte gzip JavaScript | Target-free event/build/Electron contract | Passing isolated Electron/CDP contract; live computer pending | [JSON](results/2026-07-27/ui/live-provider-action-visibility.json) · `sha256:43e78e1497f9` |
| Read-only screen verification | Electron chat → managed harness → disposable Windows VM | 1 paired live read-only task | 56.227s → 26.663s; 52.58% lower wall time; 1 progress row / 0 branches / 0 actions | 18.300s verifier | 26.663s | Passing n=1 read-only diagnostic; no action-quality claim | [JSON](results/2026-07-27/live-vnc/read-only-fast-verifier.json) · `sha256:e69b5d1b4b19` |
| Literal screen-observation fast path | Electron chat → literal router → managed read-only verifier → disposable Windows VM | 1 paired literal read-only task | 142.344s → 25.176s; 5.654× faster; 4 → 1 model calls; 1 progress row / 0 branches / 0 actions | 23.580s verifier | 25.176s | Passing n=1 read-only diagnostic; no action-quality claim | [JSON](results/2026-07-28/live-vnc/literal-screen-observation-fast-path.json) · `sha256:342e4ce992bf` |
| Managed client launch | Codex + Claude + Gemini + OpenCode | 4 clients; 12 stdio cases | 65 local contracts passed; 7/12 stdio executed; 5 skipped | Not measured | 6.96s local selection | Generated stdio proven locally; authenticated task/restart pending | [JSON](results/2026-07-25/safety/managed-client-launch-2026-07-26.json) · `sha256:b464af4a60c8` |
| Isolated managed client launch | Codex + Claude + OpenCode; Gemini policy contract | 4 installed clients / 54 contracts | 3 native dry-runs; 1 settings-only audit; raw Codex baseline shadowed without persistence | Not measured | Dry-run | Three native isolation dry-runs plus Gemini settings audit; enforcement and tasks pending | [JSON](results/2026-07-26/safety/isolated-managed-client-launch.json) · `sha256:82c085f02a53` |
| Managed smoke lab contract | Target-free app + stdin client task | 24 contracts | 24/24; 44 focused gates | Not measured | Target-free contract | Passing contract; live task rejected-before-process-creation | [JSON](results/2026-07-26/harness/managed-smoke-lab-contract.json) · `sha256:c7bb759ff96b` |
| Live Codex managed task | Codex OAuth → isolated managed MCP → harness loop | 2 failure-inclusive attempts | 2 managed calls; 22 events / 3 inner model calls; completed | 13.70s fixed run | 13.70s | Passing target-free authenticated Codex task; live computer pending | [JSON](results/2026-07-27/harness/live-codex-managed-task.json) · `sha256:5efd19fa4ac7` |
| Live Claude managed task | Claude OAuth → isolated managed MCP → harness loop | 6 failure-inclusive attempts | 2 managed calls; 22 events / 3 inner model calls; completed | 16.94s final run | 16.94s | Passing target-free authenticated Claude task; live computer pending | [JSON](results/2026-07-27/harness/live-claude-managed-task.json) · `sha256:44fa332cc5f6` |
| Live OpenCode managed task | OpenCode OAuth → pure managed MCP → harness loop | 3 failure-inclusive attempts | 2 managed calls; 22 events / 3 inner model calls; completed | 20.16s final run | 20.16s | Passing target-free authenticated OpenCode task; live computer pending | [JSON](results/2026-07-27/harness/live-opencode-managed-task.json) · `sha256:9c72f47847b7` |
| Live Gemini managed task | Gemini OAuth → native managed policy → external auth | 3 failure-inclusive attempts | Managed MCP registered: yes; 0 runs / 0 model calls; external_auth_compatibility_rejected | 6.63s final failure | 6.63s | Failing external OAuth compatibility; no computer contact | [JSON](results/2026-07-27/harness/live-gemini-managed-task.json) · `sha256:25eb2c5462ab` |
| Live Codex inner loop | Chat UI → live reasoner/controller/verifier → smoke computer | 3 live model calls / 1 action | 22 events; completed; model latency 336.75s | 336.92s end-to-end | 336.92s | Passing target-free loop; too slow for single-heavy-model routing | [JSON](results/2026-07-27/harness/live-codex-inner-loop.json) · `sha256:baca00df92b1` |
| Operator steering | Authenticated UI → managed replan | 12 tests / 13 contracts | Operator-only durable replan; 131,022-byte UI | — | 0.78s | Passing local contract and browser interaction | [JSON](results/2026-07-25/ui/operator-steering-2026-07-26.json) · `sha256:a4325a2ad4df` |
| Computer-action chat workspace | Target-free synthetic fixture | 14 frontend + 14 harness UI/fixture contracts | 14/14 + 14/14; production build passed | 303,420-byte gzip JavaScript | Target-free contract | Component/build passing; post-change browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-chat-workspace.json) · `sha256:6578fe3fc553` |
| Live computer activity in chat | Authenticated fetch SSE → assistant-ui | 19 frontend + 53 harness/API contracts | 19/19 + 53/53; action 394 → verification 396 | 75ms snapshot coalescing; 305,613-byte gzip JavaScript | Target-free runtime | Authenticated live stream passing; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-live-stream.json) · `sha256:5d61d446891f` |
| Computer-action transaction receipt | assistant-ui tool disclosure → four-phase receipt | 22 frontend + 58 harness/API contracts | 22/22 + 58/58; 4-phase receipt / 8 focused tests | 306,345-byte gzip JavaScript | Target-free contract | Component/build passing; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-action-receipt.json) · `sha256:739f8fde11de` |
| Model connections and role routing | Authenticated provider catalog → chat Models sheet | 26 frontend + 83 provider/API contracts | 26/26 + 83/83; 10 adapters / 2 configured / 3 roles | 309,038-byte gzip JavaScript | Target-free contract | Catalog/routing UI passing; live providers and browser visual audit pending | [JSON](results/2026-07-27/ui/model-connections-and-routing.json) · `sha256:28d26d4baf28` |
| Computer-use chat controls | assistant-ui → per-role route + action receipt | 36 frontend + 1,052 full Python | 36/36; 1,052 passed / 1 skipped; 3 model roles / 8 action states | 311,264-byte gzip JS; 18,064-byte gzip CSS | Detached commit contract | Passing isolated contract; browser visual audit pending | [JSON](results/2026-07-27/ui/computer-use-chat-controls.json) · `sha256:b835e8680388` |
| Action-bound screen evidence | Controller → computer input → verifier → authenticated image | 38 frontend + 113 focused Python | 38/38 + 113/113; 1,031 broader / 1 skipped; 2 model roles | 311,739-byte gzip JS; 18,134-byte gzip CSS | Target-free contract | Passing target-free contract; browser visual and concurrent Office file pending | [JSON](results/2026-07-27/ui/action-bound-screen-evidence.json) · `sha256:b5c40e8ccb52` |
| Action-bound typing read-back | Watched typer → daemon receipt → assistant-ui action transcript | 42 frontend + 153 focused Python | 42/42 + 153/153; 1,037 broader / 1 skipped; 6 visible read-back states | 313,393-byte gzip JS; 17,936-byte gzip CSS | Detached target-free contract | Passing exact-input/read-back contract; browser visual audit pending | [JSON](results/2026-07-27/ui/action-bound-typing-readback.json) · `sha256:3ee72269bfa9` |
| Computer-action timeline visual audit | assistant-ui timeline → live fixture → approval boundary | 1,200 events / 12 actions; 121 frontend + 24 Python | 121/121 + 24/24; 1,204 full Python / 1 skipped; 0px narrow / 0px 200% action overflow; approval held with 298px scroll | 333,122-byte gzip JS; 18,223-byte gzip CSS | Target-free browser audit | Desktop, 390×844, and effective 200% passing; cross-browser pending | [JSON](results/2026-07-27/ui/computer-action-timeline-visual-audit.json) · `sha256:49f63bf3d22a` |
| Cross-browser chat workspace | Chromium + Firefox + WebKit → authenticated assistant-ui fixture | 3/3 engines; 12 actions / 20 previews per engine; clean source: yes | 0px desktop / 0px responsive overflow; generic summary/raw: yes/yes with 0px overflow; connection visible: yes; 1 progress / 0 branches; 0 external requests | Chromium 3,796ms; Firefox 8,067ms; WebKit 6,857ms | Target-free official Playwright 1.61 image | Passing synthetic browser matrix; live computer and sustained decode pending | [JSON](results/2026-07-27/ui/cross-browser-chat-workspace-audit.json) · `sha256:c21adc7a90c4` |
| Live-frame resource envelope | Target-free streamed preview adapter | 6 contracts | 6/6; 4,194,304-byte frame; 8 sessions / 33,554,432 bytes cached | 450ms minimum upstream interval | — | Passing transport resource contract; browser decode pending | [JSON](results/2026-07-25/ui/live-frame-resource-envelope-2026-07-26.json) · `sha256:345d2a92bda7` |
| Normalized storage + bounded control | In-memory production contract | 100,000 events + 100 appends | 11,214.070× write-size reduction; 1,000 control events loaded | 0.086ms / 0.138ms append; 3.372ms / 4.539ms control | 214.978ms import | Serialization diagnostic; real SQLite pending | [JSON](results/2026-07-25/ui/normalized-storage-bounded-control-n100000-2026-07-26.json) · `sha256:9af680551989` |
| Gemini CLI provider adapter | Gemini CLI 0.35.3 / `account-default` | 79 provider/config/UI cases | Adapter contracts passed; startup probe timeout; 228,904 KiB peak RSS | 60.01s startup probe | 60.01s | Adapter contract; live provider unproven | [JSON](results/gemini-cli-0.35.3-compatibility-2026-07-26.json) · `sha256:4beb22389eaa` |
| Provider conformance attempt | Codex CLI + Claude Code | 2 providers / 2 calls | 0/2 exact; 2 failures; Codex adapter fixed afterward | 0.145s / 90.113s | 90.258s | Failing diagnostic; approved rerun blocked before launch | [JSON](results/2026-07-26/providers/provider-conformance-attempt-2026-07-26.json) · `sha256:abff0e7407a7` |
| Live provider conformance | Codex account-default + Claude Opus | 2 providers / 6 calls | 6/6 exact; 0 failures | Claude 15.69s / 16.46s; Codex 105.74s / 114.88s | 318.70s | Passing target-free n=3/provider; neither route is fast-controller eligible | [JSON](results/2026-07-27/providers/live-codex-claude-provider-conformance.json) · `sha256:ff1537c29a09` |
| Live chat-first assistant | Claude OAuth → chat / web MCP / hand-off / approval | 5 live tasks | 5/5; 2 requested / 1 called; consequential executed 0 | Greeting 9.30s; question 5.44s; research 15.91s; hand-off 5.19s; approval 6.97s | 42.80s | Passing target-free; canary needs_approval; first activity 2ms | [JSON](results/2026-07-27/providers/live-claude-assistant-conformance-v2.json) · `sha256:f95b16a8c174` |
| Live Claude chat workspace | Electron/CDP → Claude OAuth → visible web MCP | 4/4; 8 live provider calls; 2 visible web tools | First progress 136ms; max 1 progress / 0 branches; 0 computer events | Research 35.23s | Isolated Electron/CDP | Passing live OAuth chat UI; API-provider browser matrix pending | [JSON](results/2026-07-27/ui/live-claude-chat-workspace-cdp.json) · `sha256:147542dedce0` |
| Composer connection visibility | Authenticated workspace config → composer and Computer sheet | 128 frontend + 1,214 Python / 1 skipped | Configured MCP/machine visible before send; 0 branch counters; 0px document / 0px composer overflow | 243,306-byte initial JS gzip-9; 18,121-byte CSS gzip-9 | Target-free Electron/CDP at effective 200% | Passing truthful configured-state UI; live reachability remains checked at task start | [JSON](results/2026-07-27/ui/composer-connection-visibility.json) · `sha256:6e22341b6c56` |
| Live chat-only server | Claude OAuth → operator API with no computer adapter | 1 live server task | 1/1; exact reply yes; 1 model / 0 computer events | 7.69s process start + task | 7.69s | Passing chat-only server; target configured no; session created no | [JSON](results/2026-07-27/providers/live-claude-target-free-server-v1.json) · `sha256:7471b6ef215d` |
| Live API assistant canary | Anthropic Messages API / `claude-sonnet-5` | 1 live canary | 0/1; 1 model / 0 requested / 0 tool calls; consequential 0 | 596ms | 596ms | Failing credential compatibility: authentication-failed; computer contacted no | [JSON](results/2026-07-27/providers/live-anthropic-api-assistant-approval-conformance-v2.json) · `sha256:5b93b49696a0` |
| Live chat-first assistant | Codex OAuth → assistant → web MCP / computer hand-off | 4 live tasks | 4/4; 5 model / 1 tool calls; 3 tools / 1 server ready | Greeting 110.84s; question 97.57s; research 230.87s; hand-off 110.18s | 549.45s | Passing after strict-schema fix; too slow for default interactive route | [JSON](results/2026-07-27/providers/live-codex-assistant-conformance.json) · `sha256:3c6a8af4e33c` |
| Live consequential-tool approval | Codex OAuth → simulated send-message canary | 1 live canary | 1/1; 1 requested / 0 called; consequential executed 0 | 104.25s | 104.25s | Passing target-free; needs_approval; first activity 2ms | [JSON](results/2026-07-27/providers/live-codex-assistant-approval-conformance-v2.json) · `sha256:fc717b24d31f` |
| ScreenSpot-Pro, single pass | Codex CLI / `gpt-5.6-sol` | 100 | 73/100, 73.0% | 7.63s / 13.29s | 218.53s | Current seeded sample | [JSON](results/2026-07-25/screenspot-pro/codex-gpt-5.6-sol-seed104729-n100.json) · `sha256:dc21a201b455` |
| ScreenSpot-Pro, single pass | Claude OAuth / `opus` | 20 | 17/20, 85.0%; 0 model errors | 13.21s / 21.84s | 149.10s | Fresh seeded diagnostic; below 100-case product threshold | [JSON](results/2026-07-28/screenspot-pro/claude-opus-seed104729-n20/report.json) · `sha256:8967b855603e` |
| ScreenSpot-Pro, experimental verifier | Claude OAuth / `opus` → `haiku` | 20 | First pass 18/20; legacy corrections 7/20; veto replay 7/7 accepted correct (100.0%) at 35.0% coverage; 2 verifier errors | 13.50s / 24.87s primary | 607.97s | Failed correction experiment; 0/3 replacements correct, including 2 initially correct clicks overwritten | [JSON](results/2026-07-28/screenspot-pro/claude-opus-haiku-verifier-postmortem.json) · `sha256:4572a5cc0487` |
| Blind OCR | Local Tesseract structured ensemble | 1,000 | 56.9% selected; 61.4% expected-aware exact; 2.08% CER | 156ms / 215ms | 40.20s | Failing release gate | [JSON](results/2026-07-25/ocr/tesseract-structured-candidates-seed104729-n1000.json) · `sha256:68da9a6bdb5e` |
| Blind OCR | PaddleOCR v6 medium CPU | 1,000 | 78.9% normalized exact; 1.06% CER | 874ms / 2.54s | 1,078.82s | Failing gate; crop adapter fixed afterward | [JSON](results/2026-07-25/ocr/ocr-seed104729-n1000-comparison.json) · `sha256:dbbce9299995` |
| Blind spacing OCR | Tesseract precise, canonical text trusted | 1,000: 500 clean / 500 doubled | 32.8% detected; 181 false verified; 1 false alarms; 92.8% clean verified | 354ms / 673ms | 101.12s | Unsafe baseline; canonical OCR collapsed visible spaces | [JSON](results/2026-07-27/ocr/tesseract-spacing-integrity-seed104729-n1000/report.json) · `sha256:21432a0f4872` |
| Blind spacing OCR | Tesseract multi-scale spacing evidence, fail closed | 1,000 across 10 validated shards | 59.0% detected; 0 false verified; 3 false alarms; 555 abstained; 29.4% clean verified | 352ms / 595ms | 98.20s | Safe but failing usability gate; exact spacing remains uncertain | [JSON](results/2026-07-27/ocr/tesseract-spacing-integrity-seed104729-n1000-v2-merged/report.json) · `sha256:415bc3937d3b` |
| Blind OCR known-intent candidate union | Tesseract precise + PaddleOCR evidence | 1,000 paired cases | 827/1,000, 82.7%; routine 776/800, 97.0%; stress 51/200, 25.5% | Not measured | Retrospective paired analysis | Failing gate; runtime hybrid pass not run | [JSON](results/2026-07-26/ocr/hybrid-known-intent-candidate-union-n1000.json) · `sha256:5b37f898a147` |
| Hybrid OCR worker lifecycle | Tesseract precise + killable Paddle worker | 5 lifecycle cases + 19 contracts | 19/19; hard timeout before yes, after no | 5,025ms / 5,062ms | 5.07s | Process lifecycle fixed; diagnostic only, n=5 | [JSON](results/2026-07-26/ocr/hybrid-worker-shutdown-smoke-2026-07-27.json) · `sha256:766f4b73b6db` |
| Live Excel artifact acceptance | Claude Opus controller/reasoner + Haiku verifier → disposable Windows VM | 5 failure-inclusive attempts | 1/5; final artifact 29/29; 23/25 actions, 92.0% | 25.33s / 40.06s controller | 1,772.38s | Passing n=1 artifact; four preceding attempts retained; latency failing product target | [JSON](results/2026-07-28/live-vnc/office-excel-live-acceptance.json) · `sha256:0b4610ec5ffd` |
| Live Excel managed transcript replay | Preserved disposable-VM run → authenticated API → Electron/CDP | 532 durable events / 25 actions | 152 timeline events; 1 assistant / 0 branches / 0 duplicate planning | 126.4ms replay navigation | 244,805 gzip-9 JS bytes | Passing preserved live-run UI replay; no target contact during replay | [JSON](results/2026-07-28/ui/live-excel-managed-timeline-replay.json) · `sha256:c0f1e5f8d656` |
| OSWorld-Verified tracer | Codex, Claude, and mixed role routes | 9 current; 45 scored + 12 unscored attempts | 7/9 current; 13/45 all scored attempts | 128.56s / 883.97s | 2,687.64s current set | Diagnostic; two current failures; 13/45 full-attempt success | [JSON](results/2026-07-28/osworld/summary.json) · `sha256:e365be11d033` |
| OSWorld exact-input remediation | Opus reasoner → Sonnet controller/verifier | 12 scored attempts | 7/12; 116/132 actions, 87.9%; 2 exact long drafts before separate commits | 10.57s / 11.96s controller | 284.70s latest pass | 7 strict completions and 8 official goal states in 12; 3 strict post-input passes at 284.70s median; latency failing product target | [JSON](results/2026-07-28/osworld/summary.json) · `sha256:e365be11d033` |
| Windows Agent Arena | — | 154 tasks discovered | Not run | — | — | Blocked by missing official image | [JSON](results/2026-07-24/inventories/windows-agent-arena.json) · `sha256:c52ba54f6b29` |
| Historical PiKVM incident audit | Claude Code + Codex + OpenCode histories | 24 conversations; 4,453 PiKVM calls | 70 incidents: 20 critical, 27 high | — | — | Available local histories audited | [JSON](historical_pikvm_incidents.json) · `sha256:6dc7b9e8b555` |
| Historical critical/high regression coverage | Checked local control ledger | 47 critical/high incidents | 7 locally covered; 40 partial; 0 open | — | — | Coverage ledger; most incidents remain partial | [JSON](historical_pikvm_coverage.json) · `sha256:d6164522d369` |
<!-- pikvm-scorecard:end -->

The live Excel managed-transcript row is a replay of the preserved disposable
Windows run through the current authenticated API and Electron UI. It did not
contact the target again. The durable store contains 532 events and 25 action
attempts; the API keeps the newest 500 raw diagnostic events while separately
returning all 152 user-visible timeline events. The collapsed transcript reports
all 25 actions in one tool group with one user message, one assistant message,
zero branch counters, and zero duplicate planning rows. The evidence file
retains source revisions, reproduction commands, evaluator identity, hashes for
the private raw state and redacted timeline, and the limitation that this is not
a second live execution.

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
before checkpoint or HID. That last subtitle-specific gate remains
focused-test-proven; it is not presented as a passing task result.

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
- [`claude-opus-seed104729-n20/report.json`](results/2026-07-28/screenspot-pro/claude-opus-seed104729-n20/report.json)
- [`claude-opus-haiku-verified-seed104729-n20/report.json`](results/2026-07-28/screenspot-pro/claude-opus-haiku-verified-seed104729-n20/report.json)
- [`claude-opus-haiku-verifier-postmortem.json`](results/2026-07-28/screenspot-pro/claude-opus-haiku-verifier-postmortem.json)

Report schema v3 preserves first-pass and post-verification coordinates
separately. Older R1/R2 artifacts retain the schema emitted at run time rather
than being silently rewritten after the fact.

The compact 100-case artifact preserves every case ID, official target box,
predicted point, hit/miss, latency, error class, and platform/application/UI
slice. Case IDs resolve the original instruction and image in the pinned
dataset. Public benchmark schema v4 additionally records first-pass and
verifier usage, but this v3 run predates that instrumentation. The two fresh
Claude reports remain schema v4 because that is the code that produced them.
They are not rewritten to match the current schema. Schema v5 adds explicit
`verifier_mode`, actionable coverage, abstentions, and accepted-click accuracy.

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
5. Crosshair verification improved one older trial by one case, but the fresh
   Opus → Haiku run made all three replacement coordinates wrong, overwrote two
   initially correct clicks, and increased wall time from 149.097 to 607.970
   seconds. Verification is now veto-only by default. Any future correction
   experiment must remain explicit, offline, and non-authorizing.

## OSWorld-Verified

The official repository is pinned locally at
`b7db4d8c85d9e95e0b1db44de5bec954cf37f0cf`. Its current `test_all.json`
contains 369 tasks. A valid score requires the official resettable VM, task
setup actions, file cache, in-guest server, and evaluator. The tracer ran the
official Ubuntu QCOW through Docker with `/dev/kvm`, applied setup outside the
model boundary, drove the guest through harness → MCP → isolated daemon, and
ran the official evaluator afterward. The production PiKVM target was not
used.

Nine supported tracer tasks now have scored runs. Seven reached both
`harness_status=completed` and official score `1.0`; conda repair and subtitle
extraction scored `0.0`. A tenth compatible task, desktop-file organization,
was exercised but did not reach its evaluator and remains unscored. The current
diagnostic is 7/9, with a Wilson 95% interval of 45.3–93.7%; it is not a
representative 369-task OSWorld score.

| Task | Cycles | Actions | Model completions | Model-active | Wall | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Enable Do Not Disturb | 1 | 2/2 | 5 | 40.57s | 47.15s | official `1.0`, completed |
| Set volume to maximum | 1 | 2/2 | 5 | 33.73s | 40.16s | official `1.0`, completed |
| Enable automatic screen lock | 11 | 9/10 | 31 | 295.30s | 369.63s | official `1.0`, completed |
| Rename `todo_list_Jan_1` to `todo_list_Jan_2` | 2 | 5/5 | 12 | 98.29s | 128.56s | official `1.0`, completed |
| Disable inactive-screen dimming | 5 | 9/10 | 24 | 258.38s | 284.70s | official `1.0`, completed |
| Set timezone to UTC+0 | 11 | 18/23 | 50 | 491.19s | 602.09s | official `1.0`, completed |
| Restore deleted poster from Trash | 1 | 3/3 | 7 | 66.68s | 82.01s | official `1.0`, completed |
| Repair conda environment | 1 | 2/3 | 6 | 54.59s | 61.44s | official `0.0`, approval required |
| Extract and remove video subtitles | 12 | 13/19 | 46 | 833.08s | 1,071.89s | official `0.0`, blocked |

Across the current nine-task set: 186 model completions from 198 provider
attempts, nine structured-output repairs, two deterministic safe-draft
downgrades, three provider failures, one approval, 63/77 completed actions,
2,171.83 seconds of summed model-active time, and 2,687.64 seconds wall time.
End-to-end median/p95 were 128.56/883.97 seconds. Auto-lock, UTC, and inactive
screen dimming remain accurate but too slow; the latest subtitle run
regressed into ambiguous terminal OCR and repeated focus actions before
reaching an approval boundary.

The iteration history is intentionally less flattering. Forty-five attempts
reached the official evaluator: fifteen achieved the goal state, while only
thirteen
also had a truthful harness `completed` state. Twelve more attempts ended before
evaluation because of infrastructure, provider, process-lifecycle, or
controller/setup failures. The current 7/9 set is therefore shown beside—not
instead of—the all-attempt history:

| Denominator | Result |
| --- | ---: |
| Current latest-run task set | 7/9 official + harness success |
| All officially scored attempts | 15/45 official goal-state success |
| All scored attempts requiring official + harness success | 13/45 |
| Unscored attempts retained as failures | 12 |

Durable evidence:

- [`summary.json`](results/2026-07-28/osworld/summary.json) is the
  machine-readable denominator, route, aggregate, scored-attempt, and
  unscored-attempt index. It extends rather than replaces the July 24 and July
  25 attempt indexes.
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
  is the retained earlier inactive-screen-dimming failure: the requested
  control was absent, the harness blocked rather than changing a substitute,
  and the official evaluator scored `0.0`.
- [`bedcedc4 R20`](results/2026-07-28/osworld/bedcedc4-dim-screen-r20-wrapped-readback/report.json),
  [`R21`](results/2026-07-28/osworld/bedcedc4-dim-screen-r21-menu-proof/report.json),
  and [`R22`](results/2026-07-28/osworld/bedcedc4-dim-screen-r22-relevant-legibility/report.json)
  are three further scored failures from the exact-input remediation loop.
  They respectively exposed discarded terminal-legibility proof, an invented
  numeric zoom criterion, and a wrapped command that two exact OCR alternatives
  reconstructed but the selected reading rejected.
- [`bedcedc4 R23`](results/2026-07-28/osworld/bedcedc4-dim-screen-r23-exact-consensus/report.json)
  is the first clean inactive-screen-dimming pass. It completed 10/11 actions,
  verified 68- and 62-character terminal drafts exactly before two separate
  Return commits, required no approval, and received official score `1.0`.
- [`bedcedc4 R24`](results/2026-07-28/osworld/bedcedc4-dim-screen-r24-repeat/report.json)
  is the required post-pass reliability failure. It aborted at 900.07 seconds
  after a controller timeout, an empty exact readback, and a stale legibility
  rule that discarded verified zoom after a safe `Ctrl+C` cancellation.
- [`bedcedc4 R25`](results/2026-07-28/osworld/bedcedc4-dim-screen-r25-cancel-proof/report.json)
  is the fixed repeat. It completed 11/12 actions, verified the same 68- and
  62-character drafts exactly before separate Return commits, and received
  official score `1.0` in 464.33 seconds.
- [`bedcedc4 R26`](results/2026-07-28/osworld/bedcedc4-dim-screen-r26-parallel-control/failure.json)
  is the first verifier/controller-pipelining attempt. It failed unscored after
  95.29 seconds when two concurrent SQLite checkpoints claimed the same durable
  event sequence. No result is inferred from that attempt; commit `e879ba0`
  moved cursor read and event append under one database write reservation.
- [`bedcedc4 R27`](results/2026-07-28/osworld/bedcedc4-dim-screen-r27-parallel-control-sqlite/report.json)
  is the fixed speed pass. It completed 9/10 actions, independently verified
  the 68- and 62-character terminal drafts before separate Return commits,
  required no approval, and received official score `1.0` in 333.19 seconds.
  Six controller decisions overlapped verification. Provider critical-path
  wait fell from 343.10 to 215.70 seconds; 69.70 seconds of provider overlap
  was measured directly. Against R25, wall time fell by 131.14 seconds
  (28.24%, or 1.39x throughput). Action execution remained 112.70 seconds, so
  guarded input and OCR are the next measured bottleneck.
- [`bedcedc4 R28`](results/2026-07-28/osworld/bedcedc4-dim-screen-r28-guarded-fast-print/report.json)
  is the guarded-input speed pass. It completed 8/9 actions, required no
  approval, and received official score `1.0` in 322.31 seconds. Native print
  was permitted only for exact, non-secret, simple terminal argv; both drafts
  were still independently read back exactly and Return remained a separate
  guarded action. The 68- and 62-character entries fell from 35.17/33.86
  seconds to 14.49/15.07 seconds, reducing their combined latency by 57.18%.
  Action execution fell by 41.18 seconds (36.54%). This sample's provider wait
  rose by 30.14 seconds, limiting end-to-end improvement over R27 to 10.88
  seconds (3.27%); against R25, wall time is down 142.01 seconds (30.58%, or
  1.44x throughput). The remaining critical path is now model orchestration,
  not HID delivery.
- [`bedcedc4 R29`](results/2026-07-28/osworld/bedcedc4-dim-screen-r29-stale-prefetch-guard/report.json)
  is the first post-stale-prefetch-guard pass. It completed 9/10 actions,
  retained exact once-only visual readback for both native-print drafts,
  required no approval, and received official score `1.0` in 275.27 seconds.
  It used 23 model calls rather than R28's 27, including three Opus plans
  rather than four, and reduced provider critical-path wait from 245.84 to
  197.56 seconds. Wall time fell 47.04 seconds versus R28 (14.59%, or 1.17x
  throughput) and 189.06 seconds versus R25 (40.72%, or 1.69x throughput).
  The stale-repeat guard itself did not fire in this sample because the
  speculative controller did not repeat the completed action. The result is
  therefore evidence for the complete post-fix route, not a causal attribution
  of all 47.04 seconds to that one guard.
- [`bedcedc4 R30`](results/2026-07-29/osworld/bedcedc4-dim-screen-r30-safe-plan-preserved/report.json)
  live-exercised harmless once-only navigation-plan preservation. The initial
  short search returned `type_unverified`; the harness retained the existing
  plan, did not replay text, and still completed with official score `1.0` in
  290.44 seconds. A separate verifier response later described a successful
  command commit but omitted its required action-assessment array, forcing a
  safe replan. This led to per-request exact verifier-array constraints.
- [`bedcedc4 R31`](results/2026-07-29/osworld/bedcedc4-dim-screen-r31-constrained-verifier/report.json)
  is the retained false negative. The official evaluator scored `1.0`, but the
  harness blocked after 693.27 seconds, 18 actions, and 58 provider attempts.
  OCR recovery pushed the original verified terminal-width action out of the
  eight-item prompt-memory window; local legibility policy incorrectly reused
  that bounded view, rejected a valid retype, and stopped on stagnation. The
  report remains a scored harness failure even though the OS goal state was
  correct.
- [`bedcedc4 R32`](results/2026-07-29/osworld/bedcedc4-dim-screen-r32-durable-legibility/report.json)
  separates bounded model context from all-run local policy evidence. It again
  live-exercised safe navigation-plan preservation, retained exact once-only
  readback for the 68- and 62-character native-print drafts, used only two
  Opus plans, completed 9/10 actions, required no approval, and received
  official score `1.0` with truthful harness `completed` in 284.70 seconds.
  Across strict R29/R30/R32 passes, median/p95 are 284.70/289.87 seconds.
  R32 is 179.62 seconds faster than R25 (38.68%, or 1.63x throughput), but
  9.43 seconds slower than the fastest R29 sample. Human-relative speed is
  still unproven because no controlled human baseline has been captured.
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

The repository publishes the redacted JSON report and isolated MCP
configuration for each new attempt. The local lab directory retains the SQLite
event stream and labelled before/after images without committing those bulky
screen artifacts. Unscored attempts have explicit evidence records rather than
invented `0.0` evaluator scores.

### Inactive-screen exact-input remediation, R20–R25

All six attempts used the same official task, evaluator, VM image, Docker
image, and Opus-reasoner/Sonnet-controller-verifier route. The run was not
declared successful until both the harness and official evaluator passed.

| Attempt | Harness revision | Actions | Provider attempts | Model-active | Wall | Result | Failure or proof |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| R20 | `8007006` | 10/12 | 42 | 536.69s | 680.07s | official `0.0`, blocked | Terminal menu navigation discarded still-valid maximize/zoom proof |
| R21 | `5611c8f` | 8/8 | 28 | 341.78s | 408.30s | official `0.0`, blocked | Verifier invented a numeric zoom requirement outside the user task |
| R22 | `5c96d1b` | 6/9 | 25 | 345.78s | 467.89s | official `0.0`, blocked | Two exact wrapped-text alternatives existed, but the selected OCR reading remained noisy |
| R23 | `14d7872` | 10/11 | 27 | 325.40s | 469.70s | official `1.0`, completed | Two long drafts matched requested, issued, and observed character counts and exact SHA before separate commits |
| R24 | `fe0ad45` | 12/14 | 48 | 590.90s | 900.07s | official `0.0`, aborted | Sonnet timed out; empty exact readback was canceled safely, but the policy then discarded still-valid zoom proof and exhausted the budget |
| R25 | `61cc0bd` | 11/12 | 30 | 332.22s | 464.33s | official `1.0`, completed | Preserved verifier-confirmed legibility across cancellation; both long drafts matched exact readback before separate commits |

The failure-inclusive result is **2/6**, and the post-pass repeat is **1/2**.
The six attempts consumed 3,390.35 seconds wall time, 2,472.76 seconds of
model-active time, 200 provider attempts, eight schema repairs, two provider
failures/fallbacks, and 66 attempted actions, of which 57 completed. The latest
pass still took 464.33 seconds; it proves recovery of the reproduced bug, not
interactive speed or general OSWorld reliability.

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
17. The first dimming runs were real failures. The model initially could not
    find the requested control and safely refused to alter Screen Blank or
    Automatic Suspend. Later R20–R22 runs exposed discarded terminal
    legibility, an invented numeric zoom criterion, and failure to use two
    independent exact OCR reconstructions. R23 passed, but the required R24
    repeat exposed a controller timeout, empty OCR readback, and stale
    legibility proof after a safe cancellation. R25 passed after preserving
    verifier-confirmed legibility; repeat reliability is still only 1/2 and
    464.33-second latency remains release-blocking efficiency debt. Hidden
    evaluator rules were never disclosed to the model.
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
    missing text was not submitted. An initial repair replayed that exact chunk
    once when pixels and OCR still showed the pre-chunk prefix. A later
    deterministic boundary repro proved that policy unsafe: a leading space can
    reach the guest before its glyphs repaint, so replay creates a doubled
    space. The current path is at-most-once and stops unverified instead.
    Pointer-only moves are rejected before HID.
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

### r29 saved artifact and input-integrity recovery

r29 completed the visible managed Word task in 1,470.445 seconds. Word showed
665 words across two pages. The model repaired three repeated-space matches,
leaving zero. The original immutable runner result is still
`artifact_failed`: an older observer process owned the global snapshot hotkey
and returned an empty unrelated file.

A bounded recovery on the same disposable VM selected the saved document,
proved the observer's exact path, transferred 16,081 bytes through the
triple-copy/CRC32 visual channel, and computed SHA-256
`61b3c4e334db105e27817db6115542a01f0002afd74939271bd9782766a442ce`.
The independent host verifier accepted the OOXML package and all 11 semantic
checks: paragraph and word-count bounds, zero repeated spaces, exact title,
Title style, and the required Hamlet, Macbeth, King Lear, The Tempest, and
human-choice coverage.

This is the first retained live saved Office artifact that passes the semantic
verifier. It is not called a clean end-to-end acceptance pass because artifact
capture required post-run recovery. The machine-readable record is
[`office-word-r29-recovery.json`](results/2026-07-27/live-vnc/office-word-r29-recovery.json).

The same recovery exposed why screen appearance and sender completion cannot
act as checksums:

- a 71-character command reported all characters issued while Windows showed
  only a prefix;
- a horizontally scrolled path field appeared correct while hiding an old
  prefix; the helper's exact value exposed the append;
- several visibly correct 20–55-character commands produced no usable OCR.

Input receipts now keep separate SHA-256 values for the requested payload,
canonical delivery payload, logical sender-issued prefix, actual ordered text
payloads handed to the transport, OCR screen read-back, and the exact captured
frame that OCR evaluated. They expose the strongest screen-bound proof as
`exact_visual_readback`; legacy receipts without a frame digest remain
`exact_ocr_readback`. Other states are
`normalized_readback`, `partial_readback`,
`mismatched_readback`, `ambiguous_readback`, `issued_only`, or
`not_retained`. Sender completion alone can only be `issued_only`, and an
exact OCR/frame hash is still screen evidence rather than a guest-side
acknowledgement or saved-file oracle. The UI presents this as one compact
payload/OCR/frame fingerprint beside the exact field read-back.

A deterministic partial-delivery repro isolated the current doubled-space
failure. The second word-boundary chunk began with one space. The transport
accepted that space, the stale screen/OCR read still matched the previous
prefix, and the recovery branch replayed the full chunk, producing two spaces.
The branch has been removed: text transport is non-idempotent, so an ambiguous
chunk is never replayed. A seeded 1,000-case stale-readback regression then
proved that the exact canonical payload was handed to the backend once in every
case, with no introduced doubled spaces and zero delivery retries. This is
sender-side evidence; the guest and screen still require independent read-back.

An eight-case seeded live probe then exercised lengths around the
16-character chunk seam and the earlier 71-character failure. The Windows
helper measured 8/8 exact strings, zero character errors, and zero duplicated
spaces; screen OCR was exact in 0/8. This isolates the result: the repaired
chunk transport passed this diagnostic sample, while OCR remains the release
blocker. Full metrics are in
[`typing-space-integrity-probe.json`](results/2026-07-27/live-vnc/typing-space-integrity-probe.json).

The follow-up read-back fix keeps OCR on the exact action frame, pads the
focused text region horizontally without admitting labels above or below it,
and exposes an explicit `verification: exact` mode. Repeating the 72-character
case in that mode produced the same SHA-256 for the requested text, the
sender-issued prefix, and the known-intent OCR read-back:
`3102cdfa96a2eef4b33ecad84806cc35526f154cd660d405408316ba010e893b`.
The lab observer independently reported the exact string and zero repeated
spaces. Raw generic region OCR still retained caret or widget artifacts, so
this one-case diagnostic does not promote the general OCR claim or prove
arbitrary field-wide guest contents without an independent read channel.
Machine-readable evidence is in
[`typing-exact-readback-case72.json`](results/2026-07-27/live-vnc/typing-exact-readback-case72.json).

A fresh post-fix identity/replay attempt was refused before opening VNC because
the process-independent local target lease was already held. The harness did
not bypass the lease. That failed attempt is retained in the whitespace
integrity evidence and the post-fix live result remains pending.

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

### Visible spacing integrity

Ordinary OCR strings discard the number of spaces between recognized words.
That makes a normal exact-string comparison actively unsafe: the recognizer can
return the requested single-space text when the screen contains two spaces.
A separate deterministic blind corpus now measures that failure directly. It
contains 1,000 opaque single-line fields: 500 clean controls and 500 fields
with one injected doubled space, balanced across short/long and
proportional/monospace text with varied themes, sizes, scale, blur, noise, and
compression. Ground truth is withheld until every provider call in a shard has
finished.

The first run trusted canonical Tesseract text. It verified 464/500 controls
but detected only 164/500 corruptions and **falsely verified 181/500 visible
doubled spaces**. Short fields were the critical case: detection was 0%, with
180 false verifications across the 250 short corruptions.

The repaired precise path records whether whitespace geometry was independently
calibrated. A canonical string containing spaces can authorize completion only
when multiple image scales agree on both the glyph sequence and safe word-gap
geometry. A repeated spacing anomaly can veto it; an uncalibrated or
disagreeing read abstains. The full rerun was executed as ten deterministic
100-case shards because this host terminated longer OCR processes. The merger
required every shard index exactly once, rejected duplicate case IDs, and
reconstructed the exact 1,000-case denominator.

The repaired run produced **zero false verifications**, detected 295/500
corruptions, raised three conservative false alarms, and abstained on 555
cases. It automatically verified only 147/500 clean controls. This passes the
fail-closed safety objective but fails the 99% detection/usability release
gate. It is not presented as solved OCR. For exact files, the independent
oracle remains observer/app-readable bytes or a native file hash; an OCR
read-back SHA-256 is only a digest of screen evidence.

Evidence:
[`unsafe baseline`](results/2026-07-27/ocr/tesseract-spacing-integrity-seed104729-n1000/report.json)
and
[`fail-closed rerun`](results/2026-07-27/ocr/tesseract-spacing-integrity-seed104729-n1000-v2-merged/report.json).

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
