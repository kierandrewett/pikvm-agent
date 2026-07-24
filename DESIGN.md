---
name: PiKVM Operator Harness
description: A calm, evidence-first control surface for supervised computer use.
colors:
  command-blue: "#3b82f6"
  command-blue-deep: "#1e40af"
  command-blue-soft: "#172554"
  night-canvas: "#11141a"
  machine-well: "#0b0d12"
  operator-surface: "#161a23"
  raised-surface: "#1c2230"
  active-surface: "#242c3d"
  boundary: "#2b3447"
  text-primary: "#e8edf5"
  text-muted: "#9aa6bd"
  evidence-green: "#7ee29a"
  evidence-green-soft: "#14361f"
  caution-amber: "#f0c869"
  caution-amber-soft: "#3a2c0e"
  stop-red: "#f08aa0"
  stop-red-soft: "#3a1620"
typography:
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.012em"
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.006em"
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "0.02em"
  telemetry:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace"
    fontSize: "12px"
    fontWeight: 450
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  control: "6px"
  panel: "8px"
  status: "999px"
spacing:
  hairline: "1px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.command-blue-deep}"
    textColor: "{colors.text-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  button-secondary:
    backgroundColor: "{colors.raised-surface}"
    textColor: "{colors.text-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  button-danger:
    backgroundColor: "{colors.stop-red-soft}"
    textColor: "{colors.stop-red}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 14px"
  input:
    backgroundColor: "{colors.machine-well}"
    textColor: "{colors.text-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  status-chip:
    backgroundColor: "{colors.raised-surface}"
    textColor: "{colors.text-muted}"
    typography: "{typography.label}"
    rounded: "{rounded.status}"
    padding: "4px 8px"
  transaction:
    backgroundColor: "{colors.operator-surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.panel}"
    padding: "12px"
---

# Design System: PiKVM Operator Harness

## Overview

**Creative North Star: "The Night Operations Desk"**

The harness is a dim, orderly operations desk built for the person supervising
a live machine. The machine frame is the largest object, the current
transaction is the clearest object, and every supporting surface earns its
place by answering one question: what is happening, why, and who has authority
to continue it?

Density is deliberate but never noisy. Stable structure, short factual labels,
and flat tonal layers let an operator scan the whole workflow without reading a
terminal transcript. The interface rejects decorative AI theatre, generic SaaS
cards, and opaque model choreography. It should remain calm when the workflow is
not.

**Key Characteristics:**

- Live machine state dominates the composition.
- Plan, model, MCP, policy, HID action, and verification form one traceable
  transaction.
- Human approval is persistent, explicit, and visually separate from model
  output.
- Structural borders and tonal surfaces replace ornamental depth.
- The full control surface remains usable at narrow widths and 200% zoom.

## Colors

The palette is a restrained night shift: blue means operator command, green
means supported evidence, amber means unresolved consequence, and red is
reserved for stopping or rejecting work.

### Primary

- **Command Blue:** The sole interactive accent for selected navigation,
  focused controls, and the main affirmative control. Deep and soft variants
  carry pressed and contextual states.

### Secondary

- **Evidence Green:** Marks verification that is backed by an observation. It
  never means merely "the request was sent."
- **Caution Amber:** Marks an approval boundary, uncertain result, or stale
  observation requiring attention.
- **Stop Red:** Marks reject, abort, failure, and emergency intervention. It is
  never used decoratively.

### Neutral

- **Night Canvas:** The application background around the working surface.
- **Machine Well:** The deepest plane behind the remote screen and form fields.
- **Operator Surface:** The default timeline and transaction plane.
- **Raised Surface:** The active row, disclosure, and secondary-control plane.
- **Boundary:** A single-pixel structural divider between functional regions.
- **Primary Text:** High-contrast operational copy and values.
- **Muted Text:** Secondary metadata that remains WCAG AA legible.

### Named Rules

**The Semantic Signal Rule.** Blue commands, green proves, amber pauses, and red
stops. Never reuse those colors to decorate unrelated content.

**The Ten Percent Rule.** Saturated color occupies no more than ten percent of
the screen at rest. The machine and evidence, not the chrome, command attention.

## Typography

**Display Font:** Inter (with system UI fallback)
**Body Font:** Inter (with system UI fallback)
**Label/Mono Font:** System monospace stack

**Character:** Compact humanist sans-serif copy keeps the interface calm and
legible; monospace is confined to identifiers, timestamps, freshness values,
and exact machine evidence.

### Hierarchy

- **Headline** (650, 20px, 1.2): Product title and empty-state orientation only.
- **Title** (650, 15px, 1.35): Region and active-transaction headings.
- **Body** (400, 14px, 1.5): Task text, explanations, and approval reasons,
  constrained to roughly 70 characters per line where prose is continuous.
- **Label** (600, 12px, 0.02em): Controls, status labels, event roles, and terse
  metadata.
- **Telemetry** (450, 12px, 1.5): Tool names, IDs, arguments, timestamps, and
  freshness stamps.

### Named Rules

**The Evidence Mono Rule.** Monospace identifies machine facts; it never becomes
the overall visual theme or turns the product into a faux terminal.

## Elevation

The system uses no shadows. Depth is structural: progressively lighter tonal
surfaces, one-pixel boundaries, and state color establish hierarchy without
floating cards. Native dialogs may use the browser's backdrop, but their panel
remains flat and bounded.

### Named Rules

**The Flat Operations Rule.** A surface may become lighter when active, but it
never floats for decoration. If a panel can be removed without losing
structure, remove the panel.

## Components

Components are compact, factual, and immediately responsive. Every interactive
state has a visible focus treatment and every disabled state explains itself in
nearby text.

### Buttons

- **Shape:** Gently compact corners (6px) without pill-shaped action controls.
- **Primary:** Deep Command Blue with Primary Text and 9px by 14px padding.
- **Hover / Focus:** Lighten one tonal step over 160ms; focus uses a two-pixel
  Command Blue outline with a two-pixel offset.
- **Secondary / Ghost:** Raised Surface with a Boundary stroke; ghost buttons
  appear only in dense toolbars.
- **Danger:** Stop Red Soft with Stop Red copy; abort always requires an
  explicit confirmation step.

### Chips

- **Style:** Fully rounded status indicators with 4px by 8px padding. Each chip
  combines a text label with a dot or icon; color alone never carries state.
- **State:** Running and verified use Evidence Green, approval and stale state
  use Caution Amber, failures use Stop Red, and inactive metadata stays neutral.

### Cards / Containers

- **Corner Style:** Quietly rounded functional groups (8px).
- **Background:** Operator Surface at rest and Raised Surface only for the
  active transaction or disclosure.
- **Shadow Strategy:** No shadows; follow the Flat Operations Rule.
- **Border:** One-pixel Boundary only where it separates functions.
- **Internal Padding:** 12px compact, 16px standard, 24px only for empty states.

### Inputs / Fields

- **Style:** Machine Well fill, one-pixel Boundary stroke, and 6px corners.
- **Focus:** Command Blue border and a two-pixel external focus outline.
- **Error / Disabled:** Stop Red border for invalid data; disabled fields retain
  legible text and expose the blocking reason.

### Navigation

The run rail is a compact chronological list, not a card gallery. The active run
uses Raised Surface, a slim Command Blue inset marker, and a textual state. On
narrow screens it becomes a selectable drawer above the live machine rather
than reducing the screen to a thumbnail.

### Transaction Timeline

Plan, model, MCP, policy, HID, and verification events use stable role icons and
plain-language summaries. Raw JSON lives inside keyboard-accessible
disclosures. The active transaction remains expanded; completed groups collapse
without disappearing.

### Approval Shelf

Approval is a persistent amber shelf spanning the working area, never a fleeting
toast and never content inside the model transcript. It states the intended
effect, exact non-secret action, risk, freshness stamp, and approval ID before
showing Reject and Approve controls.

## Do's and Don'ts

### Do:

- **Do** make the live machine the largest region and preserve its aspect ratio.
- **Do** connect intent, exact tool arguments, freshness, outcome, and verifier
  evidence in one transaction.
- **Do** keep pause, reject, take-over, and abort reachable by keyboard and
  pointer.
- **Do** use Primary Text and Muted Text at WCAG 2.2 AA contrast.
- **Do** reduce to one working column below 720px instead of shrinking critical
  controls.
- **Do** make state changes immediate and restrained, with no animation longer
  than 160ms.

### Don't:

- **Don't** recreate opaque Claude Code or Codex loops where the user sees prose
  but not the live screen and exact action transaction.
- **Don't** build terminal-log walls that expose volume instead of meaning.
- **Don't** use generic card-heavy SaaS dashboards that bury the active machine
  and approval.
- **Don't** add decorative "AI control centre" visuals, glowing glass panels,
  gradients, bloom, or gratuitous animation.
- **Don't** make pause, reject, take-over, or panic stop hard to reach.
- **Don't** turn every event into a bordered card; use shared rails, rows, and
  disclosures.
- **Don't** put access tokens in URLs, event-stream query strings, rendered
  markup, or persisted local storage.
