# Product

## Register

product

## Users

People who want an AI agent to do real work on a remote physical or virtual
computer. They should be able to bring the model account they already use,
connect an MCP computer target, describe an outcome in plain language, and stay
in control without learning an operations dashboard.

## Product Purpose

Provide a chat-first desktop workspace for supervised computer use. The primary
experience is a conversation: choose a model, connect a computer through the
managed MCP server, ask for work, then watch the agent reason, call tools, use
the machine, and verify its result inline.

The harness is infrastructure behind that experience. It owns state,
idempotency, policy, verification, and approvals, but does not make those
concepts the product's information architecture.

## Core Experience

1. **Bring a model.** Choose any configured OAuth-, CLI-, API-, or
   OpenAI-compatible provider before sending a task.
2. **Connect a computer.** The workspace shows which managed MCP server and
   machine are active. Raw PiKVM credentials and VNC addresses stay out of the
   conversation and browser.
3. **Ask normally.** The composer accepts the same kind of request a user would
   give Codex or Claude Code: “make a spreadsheet,” “write this essay,” or
   “change this setting.”
4. **Watch the work.** Assistant prose, model stages, exact non-secret MCP tool
   calls, live screen transitions, and verification appear as one chronological
   conversation.
5. **Keep authority.** Consequential one-shot actions interrupt the conversation
   with an explicit approval request. Pause and stop are always available.
6. **Inspect only when needed.** Performance, provider health, event history,
   freshness counters, and raw JSON live in a diagnostics drawer.

## Brand Personality

Quiet, capable, direct. The app should feel like a serious coding agent adapted
to a real computer: familiar enough to use immediately, precise enough to trust
when an action matters.

## Anti-references

- Operations dashboards as the main product surface.
- Three-column telemetry walls that make the user reconstruct a conversation.
- Raw coding-CLI transcripts with an invisible remote screen.
- Chat shells that hide exact tool calls, model identity, or approval state.
- Provider setup that assumes one vendor or forces credentials into the UI.
- Decorative “AI control centre” visuals, glass panels, glowing gradients, and
  gratuitous animation.

## Design Principles

1. **Conversation first.** The user's request and the agent's work form the
   primary reading order.
2. **The computer is present, not dominant.** A persistent companion pane keeps
   the live screen and immediate controls visible without displacing the chat.
3. **Tools belong in the turn.** Intent, exact non-secret MCP arguments,
   freshness, outcome, and verification are one expandable assistant activity.
4. **Choice is explicit.** The selected model and managed MCP connection are
   visible beside the composer before a task is submitted.
5. **Human authority interrupts.** Approval is an inline conversational boundary
   with Reject and Approve once controls, never a toast or buried event.
6. **Diagnostics are progressive disclosure.** Operational detail remains
   available without becoming the default experience.
7. **Provider flexibility does not change safety.** Bringing a different model
   changes capability, latency, and price—not policy, idempotency, approval, or
   verification.

## Accessibility & Inclusion

Target WCAG 2.2 AA. Conversation, composer, tool disclosures, model and computer
selectors, approvals, and diagnostics are keyboard accessible. Focus is always
visible, status never relies on color alone, body text maintains at least 4.5:1
contrast, and motion respects reduced-motion preferences. The workspace remains
usable at 200% zoom and collapses the computer companion into a drawer on narrow
screens.
