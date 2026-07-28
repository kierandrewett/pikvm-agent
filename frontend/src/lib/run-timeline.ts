import type { HarnessEvent } from "@/types";

export const MAX_VISIBLE_TIMELINE_EVENTS = 5_000;

const USER_VISIBLE_TIMELINE_KINDS = new Set([
  "action.attempted",
  "action.checkpointed",
  "action.completed",
  "action.completed_unverified",
  "action.failed",
  "action.pre_action_evidence_captured",
  "action.recoverable_failure",
  "action.refused_by_operator",
  "action.refused_by_policy",
  "action.refused_stale",
  "action.stale_world_refreshed",
  "action.transport_uncertain",
  "action.ungrounded_budget_exhausted",
  "action.ungrounded_refresh_failed",
  "action.ungrounded_refreshed",
  "action.ungrounded_repeated",
  "approval.required",
  "assistant.computer_handoff",
  "assistant.computer_handoff_completed",
  "assistant.computer_handoff_failed",
  "assistant.computer_handoff_started",
  "model.completed",
  "run.aborted",
  "run.blocked",
  "run.completed",
  "run.failed",
  "run.rejected",
  "target.identity_changed",
  "tool.approval_required",
  "tool.completed",
  "tool.failed",
  "tool.refused",
  "tool.started",
  "verification.completed",
  "verification.evidence_captured",
]);

export const isUserVisibleTimelineEvent = (event: HarnessEvent) =>
  USER_VISIBLE_TIMELINE_KINDS.has(event.kind);
