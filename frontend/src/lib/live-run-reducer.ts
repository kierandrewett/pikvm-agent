import type {
  HarnessEvent,
  RunSnapshot,
  RunSummary,
  RunStatus,
} from "@/types";

const MAX_VISIBLE_EVENTS = 500;

const TERMINAL_STATUS_BY_EVENT: Partial<Record<string, RunStatus>> = {
  "run.completed": "completed",
  "run.paused": "paused",
  "run.blocked": "blocked",
  "run.rejected": "rejected",
  "run.aborted": "aborted",
  "run.failed": "failed",
};

const MODEL_ACTIVITY_CLOSED = new Set([
  "model.provider_completed",
  "model.provider_failed",
  "model.provider_skipped",
  "model.provider_budget_blocked",
  "model.failed",
]);

const TOOL_ACTIVITY_CLOSED = new Set([
  "action.completed",
  "action.failed",
  "action.refused_stale",
  "action.refused_by_operator",
  "action.stale_world_refreshed",
  "action.stale_world_retry_checkpointed",
  "action.transport_uncertain",
  "action.completed_unverified",
  "action.recoverable_failure",
  "approval.required",
  "target.identity_changed",
  "tool.completed",
  "tool.failed",
  "tool.refused",
  "tool.approval_required",
]);

const RUN_ACTIVITY_CLOSED = new Set(Object.keys(TERMINAL_STATUS_BY_EVENT));

const stringValue = (value: unknown) =>
  typeof value === "string" && value ? value : undefined;

const numberValue = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const objectValue = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

export const preferNewestRunRevision = <T extends RunSummary>(
  current: T | null | undefined,
  incoming: T,
): T => {
  if (!current || current.run_id !== incoming.run_id) return incoming;
  if (incoming.event_cursor !== current.event_cursor) {
    return incoming.event_cursor > current.event_cursor ? incoming : current;
  }
  return Date.parse(incoming.updated_at) >= Date.parse(current.updated_at)
    ? incoming
    : current;
};

const activityMatches = (
  activity: NonNullable<RunSnapshot["active_activity"]>,
  data: Record<string, unknown>,
) =>
  (["role", "provider", "attempt", "call_id"] as const).every((field) => {
    const expected = activity[field];
    const observed = data[field];
    return expected == null || observed == null || expected === observed;
  });

const modelActivity = (
  current: RunSnapshot["active_activity"],
  event: HarnessEvent,
  phase: NonNullable<
    NonNullable<RunSnapshot["active_activity"]>["phase"]
  >,
) => {
  const prior = current?.kind === "model" ? current : undefined;
  return {
    kind: "model" as const,
    started_at: prior?.started_at ?? event.at,
    phase,
    role: stringValue(event.data.role) ?? prior?.role,
    provider:
      stringValue(event.data.provider) ??
      stringValue(event.data.to_provider) ??
      prior?.provider,
    model:
      stringValue(event.data.model) ??
      (event.kind === "model.provider_failover" ? undefined : prior?.model),
    attempt: numberValue(event.data.attempt) ?? prior?.attempt,
  };
};

const activityAfterEvent = (
  current: RunSnapshot["active_activity"],
  event: HarnessEvent,
): RunSnapshot["active_activity"] => {
  if (event.kind === "model.started") {
    return modelActivity(undefined, event, "queued");
  }
  const modelPhase = {
    "model.provider_started": "provider_selected",
    "model.provider_request_sent": "request_sent",
    "model.provider_output_received": "output_received",
    "model.provider_validating": "validating",
    "model.provider_schema_repair": "schema_repair",
    "model.provider_failover": "failover",
  } as const;
  const phase = modelPhase[event.kind as keyof typeof modelPhase];
  if (phase) return modelActivity(current, event, phase);

  if (event.kind === "action.attempted" || event.kind === "tool.started") {
    return {
      kind: "tool",
      started_at: event.at,
      tool:
        stringValue(event.data.tool) ??
        (event.kind === "action.attempted" ? "MCP tool" : "Tool"),
      call_id: stringValue(event.data.call_id),
      attempt: numberValue(event.data.attempt),
      arguments: objectValue(event.data.arguments),
    };
  }
  if (
    (MODEL_ACTIVITY_CLOSED.has(event.kind) && current?.kind === "model") ||
    (TOOL_ACTIVITY_CLOSED.has(event.kind) && current?.kind === "tool")
  ) {
    return activityMatches(current, event.data) ? null : current;
  }
  if (RUN_ACTIVITY_CLOSED.has(event.kind)) return null;
  return current;
};

export const isHarnessEvent = (value: unknown): value is HarnessEvent => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.sequence === "number" &&
    Number.isSafeInteger(candidate.sequence) &&
    candidate.sequence > 0 &&
    typeof candidate.at === "string" &&
    typeof candidate.kind === "string" &&
    candidate.data != null &&
    typeof candidate.data === "object" &&
    !Array.isArray(candidate.data)
  );
};

export type RunEventReduction = {
  run: RunSnapshot;
  changed: boolean;
  gap: boolean;
};

export const reduceRunEvent = (
  run: RunSnapshot,
  event: HarnessEvent,
): RunEventReduction => {
  if (event.sequence <= run.event_cursor) {
    return { run, changed: false, gap: false };
  }
  if (event.sequence !== run.event_cursor + 1) {
    return { run, changed: false, gap: true };
  }
  const events = [...run.events, event].slice(-MAX_VISIBLE_EVENTS);
  const status =
    TERMINAL_STATUS_BY_EVENT[event.kind] ??
    (event.kind === "approval.required" ||
    event.kind === "tool.approval_required"
      ? "needs_approval"
      : run.status);
  const pendingApproval =
    event.kind === "approval.required"
      ? Object.keys(objectValue(event.data.request)).length
        ? objectValue(event.data.request)
        : run.pending_approval
      : event.kind === "tool.approval_required"
        ? {
            kind: "assistant_tool",
            approval_id: event.data.call_id,
            tool: event.data.tool,
            arguments: objectValue(event.data.arguments),
            risk: event.data.risk,
          }
        : run.pending_approval;
  return {
    changed: true,
    gap: false,
    run: {
      ...run,
      status,
      updated_at: event.at,
      event_count: Math.max(run.event_count, event.sequence),
      event_cursor: event.sequence,
      events,
      events_truncated:
        run.events_truncated ||
        run.events.length + 1 > MAX_VISIBLE_EVENTS,
      active_activity: activityAfterEvent(run.active_activity, event),
      pending_approval: pendingApproval,
    },
  };
};

const RUN_STATUSES = new Set<RunStatus>([
  "created",
  "planning",
  "running",
  "executing",
  "verifying",
  "paused",
  "needs_approval",
  "blocked",
  "completed",
  "failed",
  "rejected",
  "aborted",
]);

export const reduceRunState = (
  run: RunSnapshot,
  value: unknown,
): RunSnapshot => {
  const state = objectValue(value);
  const status = stringValue(state.status);
  const active = state.active_activity;
  return {
    ...run,
    status:
      status && RUN_STATUSES.has(status as RunStatus)
        ? (status as RunStatus)
        : run.status,
    active_activity:
      active === null
        ? null
        : Object.keys(objectValue(active)).length
          ? (objectValue(active) as NonNullable<
              RunSnapshot["active_activity"]
            >)
          : run.active_activity,
  };
};

export const eventNeedsSnapshotReconciliation = (event: HarnessEvent) =>
  event.kind === "assistant.computer_handoff" ||
  event.kind === "assistant.computer_handoff_started" ||
  event.kind === "assistant.computer_handoff_completed" ||
  event.kind === "assistant.computer_handoff_failed" ||
  event.kind === "run.completed" ||
  event.kind === "run.failed" ||
  event.kind === "run.blocked" ||
  event.kind === "run.rejected" ||
  event.kind === "run.aborted";
