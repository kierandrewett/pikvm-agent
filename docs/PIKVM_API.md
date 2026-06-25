# PiKVM API & inherited tuning constants

Reverse-engineered from the battle-tested TypeScript client in
`~/dev/pikvm-desktop-agentic` (`src/pikvm-control.ts`, `src/human-typing.ts`,
`src/ambient/fingerprint.ts`, `src/agent/watched-typing.ts`,
`src/agent/regions.ts`). The Python `pikvm/` and `vision/`/`executor/` packages
reproduce these exactly. Treat this as the contract.

## Transport & auth

- **Base origin**: e.g. `https://pikvm.local`. PiKVM ships a **self-signed cert**
  → TLS verification is disabled (`verify=False` / `rejectUnauthorized: false`).
- **Auth**, either:
  - headers `X-KVMD-User: <user>` + `X-KVMD-Passwd: <pass>`, or
  - cookie `auth_token=<token>` (a logged-in browser session).
- **HID WebSocket**: `wss://<host>/api/ws` (or `ws://` for http origins).

## Screen

- **Snapshot (JPEG)**: `GET <origin>/api/streamer/snapshot?allow_offline=1`
  → `image/jpeg`. Frame size from response headers `X-UStreamer-Width` /
  `X-UStreamer-Height`; fall back to parsing JPEG SOF markers.
- **Built-in OCR (tesseract)** — our zero-dependency default OCR provider:
  `GET /api/streamer/snapshot` with params:
  `allow_offline=1, ocr=1, ocr_langs=eng, ocr_left, ocr_top, ocr_right, ocr_bottom`
  (region in **native** stream pixels; omit region for full frame). Returns text.
- **Downscale rule**: full frames are downscaled so the **long edge ≤ 1280 px**
  (`MAX_SCREENSHOT_DIM`), held fixed per session so the model's pixel sense stays
  calibrated. Crops are taken from the full-res frame, then capped.

## Keyboard

- **Per-key (HID WS)**: `{"event_type":"key","event":{"key":<code>,"state":<bool>}}`
  where `<code>` is a JS `KeyboardEvent.code` (e.g. `KeyA`, `Digit1`, `Enter`,
  `ShiftLeft`, `ControlLeft`, `AltLeft`, `MetaLeft`, `Tab`, `Backspace`, `Delete`,
  `Home`, `End`, `CapsLock`).
- **Fast server-side print (keymap printer)**: `POST /api/hid/print`, body = raw
  text, `Content-Type: text/plain`, params `limit=0` + optional `keymap=<name>`
  + (`delay=<seconds>` **or** `slow=1`). KVMD types the whole string using its
  configured keymap (layout-correct). **Strips `\r\n` → space** so it can never
  submit; the caller must verify the field after (kvmd returns 200 even when it
  drops/garbles chars under lag). Use for long *prose* only.
- **Newlines never auto-submit**: `\n` in typed text is collapsed to a space;
  Enter/submit is always a separate, explicit, reviewable key press.
- **Caps-Lock compensation**: when the target Caps-Lock LED is ON, invert Shift
  for `Key[A-Z]` strokes so the output case is correct, **without** toggling the
  target's Caps Lock (which would race the live LED). Letters only.
- **Layout**: we send physical key codes; the target keymap decides glyphs.
  US is the base map; UK ISO overrides for `" @ # ~ \ | £ ¬` (so `cd ~/...` and
  `"` come out right on a UK target). Keymap name → layout: `en-gb*`→uk,
  `en-us*`→us.

## Mouse (HID WS)

- **Absolute move**: `{"event_type":"mouse_move","event":{"to":{"x":<n>,"y":<n>}}}`
  where `x`/`y` are normalized to **−32768..32767**.
  `to_norm(px, span) = clamp(round(px / (span-1) * 65534) − 32767, −32768, 32767)`.
- **Relative nudge**: `{"event_type":"mouse_relative","event":{"delta":{"x":dx,"y":dy},"squash":true}}`.
- **Button**: `{"event_type":"mouse_button","event":{"button":"left|right|middle","state":<bool>}}`.
- **Wheel**: `{"event_type":"mouse_wheel","event":{"delta":{"x":dx,"y":dy}}}`.
  **Convention: `dy>0` scrolls UP, `dx>0` scrolls RIGHT.** A bare scroll must
  default to a real delta (e.g. `dy=3`) — never a silent `(0,0)` no-op (E10).

## KVMD state stream (server → client on the same `/api/ws`)

Inbound JSON `{"event_type":..,"event":..}`. Merge into a cached state:

- `hid` → `online`, `busy`, `connected`, `keyboard.online`,
  `keyboard.leds.{caps,scroll,num}`, `mouse.{online,absolute,outputs}`.
- `hid_keymaps` → `keymaps.{default,available}`.
- `streamer` → `source.{online,resolution.{width,height},captured_fps}`,
  `encoder.quality`.
- `ocr` → `enabled`, `langs.{default,available}`.
- `loop` → marks the initial full-state bundle complete (`ready=true`).

Derived getters: caps-lock LED, default keymap, native resolution (for OCR/coord
scaling), mouse mode (`absolute|relative|unknown`), HID online tri-state
(`true`=attached, `false`=detached → block input, `undefined`=unknown → allow).

## Perceptual fingerprint & settle (the world-change signal)

- **Fingerprint**: frame → **16×16** grayscale (mean of R,G,B) → 256-element
  `uint8`, row-major.
- **`fp_diff(a,b)`** = `sum(|a−b|) / n / 255` ∈ [0,1] (mean abs diff, normalized).
- **`fp_variance(a)`** = population std-dev of bytes; `< 6` ⇒ blank/no-signal.
- Thresholds (single source of truth): `FP_MOVE = 0.04` (actively changing),
  `FP_SETTLE = 0.015` (settled), `FP_MEANINGFUL = 0.05` (a settled frame must
  differ from baseline by ≥ this to matter / to count as a world change).
- **Settle**: declare ready after **K=2 consecutive** frames with step-diff
  `< FP_SETTLE`; poll **150 ms**, timeout **1200 ms**; a null source ⇒ ready
  immediately (graceful degrade). A lone stable frame is not enough.

## Grid (field localisation & region watches)

- **Grid**: frame → **96 cols × 54 rows** grayscale (mean RGB), row-major `uint8`.
- **Region watch change**: project the pixel region onto the grid by ratio,
  compare baseline vs current cells with `fp_diff`, change when `> FP_MEANINGFUL`
  (0.05); refresh baseline so each change reports once.
- **Region segmenter recompute gate**: `CELL_DELTA = 22`, `RECOMPUTE_FRACTION =
  0.06` (>6% of cells shifted by >22 ⇒ layout materially changed).

## World-version model

There is no integer counter in the TS code; the functional equivalent is the
**freshness stale-guard**: stamp a fingerprint baseline on each full-frame look
(`mark_agent_look`), and before any action check `look_freshness()` — `changed`
when `fp_diff(baseline, current) > FP_MEANINGFUL` (or never looked). In this
Python runtime we make it explicit: an integer `world_version` is incremented on
each meaningful change (active app / modal / toast over target / keyboard state /
pager / black screen / focus mode change), and every operator decision must cite
the `frame_id` + `world_version` it was planned against.

## Watched-typing tuning constants (executor/typing + verification)

```text
CELL_DELTA            = 18    # grayscale delta for a grid cell to count as changed
MIN_CHANGED_CELLS     = 2     # fewer (after prune) ⇒ nothing landed
LOCATE_MIN_CHARS      = 5     # only auto-locate once first chunk ≥ this
ABORT_MIN_CHARS       = 8     # only HARD-fail "no focus" when ≥ this typed
MAX_BOX_HEIGHT_FRAC   = 0.6   # a change taller than this frac of screen = repaint
CHUNK_TARGET          = 16    # word-boundary chunk target length
MAX_TOTAL_CORRECTIONS = 1     # one clean retry; never a compounding loop
MAX_BACKSPACES        = 400   # safety cap on a correction's clear
FAST_PRINT_MIN        = 40    # above this, plain text takes the fast print path
```

Clearing a field for a retype is **Home + forward-Delete×N**, never Ctrl+A
(in a terminal Ctrl+A = line-start → would duplicate) and **never Enter**.

## Verification (read-back classification) — reproduce exactly

- **Quote fold (always)**: `QUOTE_RE = /['"`´‘’“”′″]/g` → `'`.
- **Confusables (non-precise only)**: `0→o, 1→l, i→l, 5→s, 8→b, 2→z, 9→g, q→g,
  6→g` (applied after lowercasing). **`|` is NOT folded** — it must stay a
  distinguishing symbol (folding it once hid a real `| → l` layout slip).
- **Prompt strip (read-back only, never intended)**:
  `PROMPT_RE = /^\s*(?:PS\s+[^>\n]*>|[A-Za-z]:\\[^>\n]*>|[^\s@]+@[^\s@]+[^$#%>\n]*[$#%>]|[$#%>❯➜λ»])\s+/`.
- **Strict ("precise") required when** `code` flagged OR `is_exact_text(s)`:
  shell metachars `| < > $ ` ~ \`, flags/paths `-x`/`--x`/`/abs`/`~/`/`./`,
  URL scheme, or a common command head (`sudo git npm yarn pnpm node cd ls cat
  grep find echo rm mkdir cp mv chmod chown ssh scp curl wget docker kubectl
  systemctl tar sed awk`). Precise mode does NOT fold confusables and fails
  closed on any symbol/case difference.
- **Verdicts**: `match | contains | mismatch | unverified`
  (`ok = verdict != "mismatch"`). Order: empty→unverified; exact→match;
  contains→contains; prefix-only (truncated)→unverified; alnum-foldcase equal but
  raw differ→mismatch (layout/caps slip); caught-extra (read ≫ intent &
  not-containing)→unverified; precise & overlap<0.5→unverified else mismatch;
  confusable-equal→match/contains; bounded Levenshtein ≤ ceil(0.08·len)→match;
  overlap<0.5→unverified; else mismatch.
- **High-risk chars** (force strict verification, split Enter):
  `| & ; > < $ ` ~ * " ' \ ! { } [ ] ( )`.

## E1–E10 incidents (regression fixtures, Phase 6)

```text
E1  find README symbol/case   README→readme and |→~ must NOT verify; block Enter after fail
E2  Teams stale double-Enter  block 2nd Enter after world change; auto-screenshot after action
E3  Caps Lock / background AVD read caps before typing; fast print disabled when caps on
E4  shell prompt false miss   strip leading $/#/%/user@host/PS/drive: prompt before compare
E5  truncated readback        prefix-only read is unverified, NOT a destructive retype
E6  VS Code Quick Open region OCR over results dropdown is wrong-region, not typed-wrong
E7  long Teams text slow/voice long plain prose uses the fast print path + style card
E8  Teams autoformat          prepend/reorder ⇒ prepend-autocorrect, confirm-then-retype
E9  git pager trap            terminal.pager blocks shell typing; q recovery / no-pager
E10 scroll no-op              scroll(direction,amount) must never become scroll(0,0)
```
