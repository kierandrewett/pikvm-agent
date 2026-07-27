---
name: PiKVM Agent
description: A chat-first desktop workspace for supervised computer use.
colors:
  accent: "#5f75ee"
  accent-strong: "#7185f4"
  accent-soft: "#22294d"
  canvas: "#111216"
  sidebar: "#15161b"
  conversation: "#191a20"
  well: "#0c0d10"
  raised: "#22232a"
  active: "#292b34"
  boundary: "#30323b"
  text-primary: "#f1f2f5"
  text-muted: "#a8abb5"
  evidence: "#78d69b"
  evidence-soft: "#173423"
  caution: "#e7bd67"
  caution-soft: "#392d13"
  stop: "#ef8d9d"
  stop-soft: "#3b1820"
typography:
  title:
    fontFamily: "Geist Variable, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.006em"
  body:
    fontFamily: "Geist Variable, ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "Geist Variable, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "normal"
  telemetry:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace"
    fontSize: "12px"
    fontWeight: 450
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  control: "7px"
  panel: "10px"
  composer: "14px"
  status: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
---

# Design System: PiKVM Agent

## Creative North Star

**“A coding agent with a computer beside it.”**

The workspace should feel immediately familiar to someone who has used Codex,
Claude, or a modern editor assistant. Conversation is the centre of gravity.
The remote computer is a persistent companion, not a dashboard hero. Tool calls
read as part of the assistant's work, and operational data stays folded until a
user asks for it.

The interface is dark because the user may leave it open beside a bright remote
desktop for long sessions. Restrained neutral surfaces reduce glare; one violet
accent distinguishes user commands from machine evidence without turning the
app into an “AI control centre.”

## Application Structure

At desktop widths the app uses two persistent regions:

- **Conversation rail, 228–256px.** New chat, recent tasks, settings.
- **Conversation, flexible.** User requests, assistant activity, approvals, and
  the task composer.

The live computer and diagnostics open as contextual sheets. Neither is a
permanent stream competing with the conversation.

Below 900px, the conversation rail becomes a drawer. The composer remains fixed
to the bottom of the conversation, and contextual sheets use the available
viewport.

## Color

The palette is restrained:

- **Accent** selects the current chat, model, or primary submission.
- **Evidence green** means an observed transition or independent verification,
  not merely a completed request.
- **Caution amber** means unresolved consequence or required approval.
- **Stop red** is reserved for rejection, abort, and failure.
- Neutral surfaces carry the rest of the interface.

Saturated color should occupy less than ten percent of the resting screen.

## Typography

Use the locally packaged Geist variable family throughout with system fallbacks.
Titles are compact rather than display-sized. Continuous assistant prose is
limited to roughly 72 characters per line. Monospace is reserved for tool names,
arguments, identifiers, freshness values, and hashes.

## Components

### Conversation rail

Rows are flat and quiet. The selected conversation uses a tonal fill and a
two-pixel inset accent. Status appears as short text, not a separate metric
card.

### Conversation

User messages are compact, right-aligned bubbles with restrained accent tint.
Assistant turns are unboxed prose with a subtle role icon. Long-running work
updates the same assistant turn instead of creating a wall of status messages.

### Tool activity

Each action is an inline disclosure:

- input-specific summary such as click coordinates, typed-text preview, key
  chord, scroll direction, or multi-step sequence;
- visible running, completed, refused, failed, or approval-needed state;
- MCP tool name, input count, character count, freshness, result, and
  verification evidence inside;
- exact non-secret arguments behind a second disclosure;
- red/amber state only when intervention is actually needed.

### Composer

The composer is the primary control. It contains:

- multiline task input;
- selected model;
- visible managed MCP connection state;
- send button.

Enter sends and Shift+Enter inserts a newline. Provider and connection choices
remain visible before submission.

### Computer sheet

The current frame uses the available sheet area while preserving aspect ratio.
The toolbar shows the machine alias, connection layer, run state, frame number,
pause, and continue. It stays closed during routine conversation.

### Approval boundary

Approval opens its containing computer-action disclosure automatically. It
states the effect, risk, exact request, target freshness, and operator note
before showing Deny and Allow once. Consequential actions require a second
confirmation. Model output can never activate either control.

### Diagnostics drawer

Provider readiness, latency, budget, event timeline, raw JSON, and support
information live here. The drawer is useful for developers and support without
teaching normal users to operate the product through telemetry.

## Interaction and Motion

All controls have default, hover, focus, active, disabled, loading, and error
states. Transitions are 120–180ms ease-out and communicate state only. Reduced
motion removes transforms and animated progress. Loading uses local skeletons,
never a blocking page spinner.

## Rules

- Keep the conversation as the primary reading order.
- Keep model and managed MCP connection visible at send time.
- Keep pause and stop reachable without opening diagnostics.
- Keep completed tool calls collapsed by default; open approvals automatically.
- Never show credentials, VNC addresses, or raw provider error bodies.
- Never turn every event into a card.
- Never use decorative gradients, glass effects, wide shadows, or oversized
  rounded panels.
- Never let a diagnostic metric compete with the user's task.
