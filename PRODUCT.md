# Product

## Register

product

## Users

Developers, IT operators, and technical teams supervising AI-driven work on
remote physical or virtual computers. They need to understand what the agent is
seeing, planning, calling, typing, and verifying while it happens, and they need
to intervene immediately when risk or uncertainty appears.

## Product Purpose

Provide a responsive, provider-neutral computer-use harness that turns PiKVM's
raw video and HID surface into a visible, checkpointed, human-supervised
workflow. Success means routine remote work can run across OAuth- and API-backed
models without hiding tool calls, repeating ambiguous input, silently approving
consequential actions, or forcing the operator to reconstruct events from a
coding CLI transcript.

## Brand Personality

Precise, calm, transparent. The product should feel technically serious and
immediately trustworthy under pressure. Its voice is direct and factual: current
state, evidence, risk, and available control are always clear.

## Anti-references

- Opaque Claude Code or Codex loops where the user sees prose but not the live
  screen and exact action transaction.
- Terminal-log walls that expose volume instead of meaning.
- Generic card-heavy SaaS dashboards that bury the active machine and approval.
- Decorative "AI control centre" visuals, glowing glass panels, and gratuitous
  animation.
- Interfaces that make pause, reject, take-over, or panic stop hard to reach.

## Design Principles

1. **Show the transaction.** Keep the live frame, model role, intent, exact
   non-secret tool arguments, freshness stamp, and verifier evidence connected.
2. **Human authority is visible.** Consequential actions stop in a persistent,
   unmistakable approval shelf; approval is never inferred from model output.
3. **Structure before logs.** Group the timeline by plan, model, MCP, policy,
   action, and verification, with raw JSON available on demand.
4. **Fast oversight.** Initial state renders immediately, updates stream without
   page churn, and pause/abort remain reachable with keyboard and pointer.
5. **Provider flexibility without behavioral drift.** Provider changes affect
   reasoning capacity and latency, not safety, state, approvals, or audit shape.

## Accessibility & Inclusion

Target WCAG 2.2 AA. All controls and timeline disclosures are keyboard
accessible, focus is always visible, status never relies on color alone, body
text maintains at least 4.5:1 contrast, and motion respects reduced-motion
preferences. Dense technical information remains legible at 200% zoom and on a
narrow viewport.
