import type { ThreadMessageLike } from "@assistant-ui/react";
import type { HarnessEvent, RunSnapshot } from "@/types";

const ACTIVE_STATUSES = new Set([
  "created",
  "planning",
  "running",
  "executing",
  "verifying",
]);

const safeString = (value: unknown) => (typeof value === "string" ? value : "");

const safeNumber = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const elapsedLabel = (startedAt: string) => {
  const started = Date.parse(startedAt);
  if (!Number.isFinite(started)) return "";
  const elapsedSeconds = Math.max(
    0,
    Math.floor((Date.now() - started) / 1_000),
  );
  if (elapsedSeconds < 60) return `${elapsedSeconds}s`;
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
};

const eventIdentity = (event: HarnessEvent) =>
  safeString(event.data.call_id) ||
  String(safeNumber(event.data.index) ?? event.sequence);

const SAFE_REFUSAL_OUTCOMES = new Set([
  "action.refused_by_operator",
  "action.refused_stale",
  "action.stale_world_refreshed",
  "action.ungrounded_refreshed",
]);

const FAILED_OUTCOMES = new Set([
  "action.failed",
  "action.recoverable_failure",
  "action.transport_uncertain",
  "action.ungrounded_refresh_failed",
  "action.ungrounded_repeated",
  "action.ungrounded_budget_exhausted",
  "target.identity_changed",
]);

const ACTION_OUTCOMES = new Set([
  "action.completed",
  "action.completed_unverified",
  ...SAFE_REFUSAL_OUTCOMES,
  ...FAILED_OUTCOMES,
]);

const outcomeForAttempt = (
  attempt: HarnessEvent,
  events: readonly HarnessEvent[],
) => {
  const identity = eventIdentity(attempt);
  const nextAttempt = events.find(
    (event) =>
      event.sequence > attempt.sequence &&
      event.kind === "action.attempted" &&
      eventIdentity(event) === identity,
  );
  return events.find(
    (event) =>
      event.sequence > attempt.sequence &&
      (nextAttempt == null || event.sequence < nextAttempt.sequence) &&
      ACTION_OUTCOMES.has(event.kind) &&
      eventIdentity(event) === identity,
  );
};

const checkpointForAttempt = (
  attempt: HarnessEvent,
  events: readonly HarnessEvent[],
) => {
  const idempotencyKey = safeString(attempt.data.idempotency_key);
  const index = safeNumber(attempt.data.index);
  return [...events]
    .reverse()
    .find(
      (event) =>
        event.sequence < attempt.sequence &&
        event.kind === "action.checkpointed" &&
        (idempotencyKey
          ? safeString(event.data.idempotency_key) === idempotencyKey
          : safeNumber(event.data.index) === index),
    );
};

const verificationForOutcome = (
  outcome: HarnessEvent | undefined,
  events: readonly HarnessEvent[],
) => {
  if (!outcome) return undefined;
  const nextAttempt = events.find(
    (event) =>
      event.sequence > outcome.sequence && event.kind === "action.attempted",
  );
  return events.find(
    (event) =>
      event.sequence > outcome.sequence &&
      (nextAttempt == null || event.sequence < nextAttempt.sequence) &&
      (event.kind === "verification.completed" ||
        (event.kind === "model.completed" &&
          safeString(event.data.role) === "verifier")),
  );
};

const evidenceForOutcome = (
  outcome: HarnessEvent | undefined,
  events: readonly HarnessEvent[],
) => {
  if (!outcome) return undefined;
  const nextAttempt = events.find(
    (event) =>
      event.sequence > outcome.sequence && event.kind === "action.attempted",
  );
  return events.find(
    (event) =>
      event.sequence > outcome.sequence &&
      (nextAttempt == null || event.sequence < nextAttempt.sequence) &&
      event.kind === "verification.evidence_captured",
  );
};

const controllerForCheckpoint = (
  checkpoint: HarnessEvent | undefined,
  events: readonly HarnessEvent[],
) => {
  if (!checkpoint) return undefined;
  const previousAttempt = [...events]
    .reverse()
    .find(
      (event) =>
        event.sequence < checkpoint.sequence &&
        event.kind === "action.attempted",
    );
  return [...events]
    .reverse()
    .find(
      (event) =>
        event.sequence < checkpoint.sequence &&
        (previousAttempt == null ||
          event.sequence > previousAttempt.sequence) &&
        event.kind === "model.completed" &&
        safeString(event.data.role) === "controller",
    );
};

const modelReceipt = (event: HarnessEvent | undefined) =>
  event
    ? {
        provider: safeString(event.data.provider),
        model: safeString(event.data.model),
        latency_ms: safeNumber(event.data.latency_ms),
      }
    : undefined;

const inputReceipts = (value: unknown) =>
  (Array.isArray(value) ? value : [])
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item),
    )
    .slice(0, 20)
    .map((item) => ({
      index: safeNumber(item.index),
      type: safeString(item.type),
      status: safeString(item.status),
      verdict: safeString(item.verdict),
      observed_text: safeString(item.observed_text),
      observed_text_redacted: item.observed_text_redacted === true,
      typed_characters: safeNumber(item.typed_characters),
      intended_characters: safeNumber(item.intended_characters),
      correction_count: safeNumber(item.correction_count),
      delivery_retries: safeNumber(item.delivery_retries),
      used_fast_path: item.used_fast_path === true,
      summary: safeString(item.summary),
      edit_distance: safeNumber(item.edit_distance),
      focus_evidence: safeString(item.focus_evidence),
    }));

const verificationVerdict = (event: HarnessEvent) => {
  const verdict = safeString(event.data.verdict) || "verified";
  return verdict === "complete" ? "verified" : verdict;
};

const outcomeReason = (outcome: HarnessEvent) => {
  if (outcome.kind === "action.stale_world_refreshed") {
    const refused = safeString(outcome.data.status).replaceAll("_", " ").trim();
    return `${refused || "stale input"}; screen refreshed`;
  }
  if (outcome.kind === "action.ungrounded_refreshed") {
    return "click target was not independently grounded; screen refreshed";
  }
  return (
    safeString(outcome.data.error) ||
    safeString(outcome.data.reason) ||
    safeString(outcome.data.status).replaceAll("_", " ").trim()
  );
};

const planMarkdown = (run: RunSnapshot) => {
  if (run.plan) {
    return run.plan.summary;
  }
  if (run.status === "planning") {
    return "Planning the work and defining visible completion evidence.";
  }
  return "Working through the requested task.";
};

const completionMarkdown = (run: RunSnapshot) => {
  if (run.status === "completed") {
    const summary = safeString(run.last_verification?.summary);
    return summary || "Completed and checkpointed.";
  }
  if (["failed", "blocked", "rejected", "aborted"].includes(run.status)) {
    return `Stopped: ${run.error || run.status.replaceAll("_", " ")}.`;
  }
  if (run.status === "paused") {
    return run.error
      ? `Paused: ${run.error}. Retry, choose another model, or give a correction.`
      : "Paused at a durable checkpoint. You can continue or give a correction.";
  }
  if (run.status === "needs_approval") {
    return "";
  }
  const activity = run.active_activity;
  if (activity?.kind === "model") {
    const verb =
      activity.role === "reasoner"
        ? "Planning"
        : activity.role === "controller"
          ? "Choosing the next input"
          : activity.role === "verifier"
            ? "Checking the screen"
            : "Model working";
    const identity = [activity.model, activity.provider]
      .filter(Boolean)
      .join(" · ");
    return [verb, identity, elapsedLabel(activity.started_at)]
      .filter(Boolean)
      .join(" · ");
  }
  return "";
};

const toolParts = (run: RunSnapshot) => {
  const attempts = run.events
    .filter((event) => event.kind === "action.attempted")
    .slice(-12);
  const activeCallId = safeString(run.active_activity?.call_id);
  const activeAlreadyRepresented = activeCallId
    ? attempts.some((event) => safeString(event.data.call_id) === activeCallId)
    : attempts.length > 0;
  if (run.active_activity?.kind === "tool" && !activeAlreadyRepresented) {
    attempts.push({
      sequence: run.event_cursor + 1,
      at: run.active_activity.started_at,
      kind: "action.attempted",
      data: {
        call_id: run.active_activity.call_id,
        attempt: run.active_activity.attempt,
        tool: run.active_activity.tool,
        arguments: run.active_activity.arguments ?? {},
      },
    });
  }

  return attempts.map((attempt, index) => {
    const checkpoint = checkpointForAttempt(attempt, run.events);
    const outcome = outcomeForAttempt(attempt, run.events);
    const verification = verificationForOutcome(outcome, run.events);
    const evidence = evidenceForOutcome(outcome, run.events);
    const controller = controllerForCheckpoint(checkpoint, run.events);
    const isPending = !outcome && index === attempts.length - 1;
    const approval = isPending ? run.pending_approval : null;
    const approvalId = safeString(approval?.approval_id);
    const approvalRisk = safeString(approval?.risk).replaceAll("_", " ").trim();
    const approvalReason = safeString(approval?.reason).trim();
    const approvalDescription = [approvalRisk, approvalReason]
      .filter(Boolean)
      .join(": ");
    const failed = outcome ? FAILED_OUTCOMES.has(outcome.kind) : false;
    const result =
      outcome == null
        ? undefined
        : failed
          ? {
              status: "failed",
              error: outcomeReason(outcome) || "The computer input failed.",
              attempt: safeNumber(attempt.data.attempt),
              attempted_at: attempt.at,
              completed_at: outcome.at,
            }
          : {
              status: SAFE_REFUSAL_OUTCOMES.has(outcome.kind)
                ? "refused"
                : outcome.kind === "action.completed_unverified"
                  ? "unverified"
                  : "completed",
              reason: outcomeReason(outcome) || undefined,
              frame_id: outcome.data.frame_id ?? outcome.data.fresh_frame_id,
              world_version:
                outcome.data.world_version ?? outcome.data.fresh_world_version,
              attempt: safeNumber(attempt.data.attempt),
              attempted_at: attempt.at,
              completed_at: outcome.at,
              verification: verification
                ? {
                    verdict: verificationVerdict(verification),
                    summary: safeString(verification.data.summary),
                    observed_at: verification.at,
                    provider: safeString(verification.data.provider),
                    model: safeString(verification.data.model),
                    latency_ms: safeNumber(verification.data.latency_ms),
                  }
                : undefined,
            };
    const exactArgs: Record<string, unknown> =
      attempt.data.arguments &&
      typeof attempt.data.arguments === "object" &&
      !Array.isArray(attempt.data.arguments)
        ? (attempt.data.arguments as Record<string, unknown>)
        : {};
    const args = {
      ...exactArgs,
      __receipt: {
        intent: safeString(checkpoint?.data.intent),
        expected_evidence: Array.isArray(checkpoint?.data.expected_evidence)
          ? checkpoint.data.expected_evidence.map(String)
          : [],
        attempt: safeNumber(attempt.data.attempt),
        latency_ms: safeNumber(outcome?.data.latency_ms),
        idempotency_key:
          safeString(attempt.data.idempotency_key) ||
          safeString(exactArgs.idempotency_key),
        evidence_revision: safeNumber(evidence?.data.revision),
        evidence_before_frame_id: safeNumber(evidence?.data.before_frame_id),
        evidence_after_frame_id: safeNumber(evidence?.data.after_frame_id),
        controller: modelReceipt(controller),
        verifier: modelReceipt(verification),
        input_receipts: inputReceipts(outcome?.data.input_receipts),
      },
    };
    return {
      type: "tool-call" as const,
      toolCallId: `${run.run_id}:${eventIdentity(attempt)}`,
      toolName: safeString(attempt.data.tool) || "MCP tool",
      args: args as never,
      argsText: JSON.stringify(exactArgs, null, 2),
      result,
      isError: failed,
      approval: approvalId
        ? {
            id: approvalId,
            options: [
              {
                id: "approve",
                kind: "allow-once" as const,
                label: "Allow once",
                description:
                  approvalDescription ||
                  "Permit this exact computer input one time.",
                confirm: {
                  title: "Allow this computer action?",
                  description:
                    approvalDescription ||
                    "The exact input shown above will be sent once.",
                },
              },
              { id: "reject", kind: "reject-once" as const, label: "Deny" },
            ],
          }
        : undefined,
    };
  });
};

const assistantStatus = (
  run: RunSnapshot,
): NonNullable<ThreadMessageLike["status"]> => {
  if (run.status === "needs_approval") {
    return { type: "requires-action", reason: "tool-calls" };
  }
  if (ACTIVE_STATUSES.has(run.status)) return { type: "running" };
  if (["failed", "blocked", "rejected", "aborted"].includes(run.status)) {
    return {
      type: "incomplete",
      reason: run.status === "aborted" ? "cancelled" : "error",
      error: run.error || run.status,
    };
  }
  return { type: "complete", reason: "stop" };
};

export function messagesForRun(run: RunSnapshot | null): ThreadMessageLike[] {
  if (!run) return [];
  const completion = completionMarkdown(run);
  const content: ThreadMessageLike["content"] = [
    { type: "text", text: planMarkdown(run) },
    ...toolParts(run),
    ...(completion ? [{ type: "text" as const, text: completion }] : []),
  ];

  const messages: ThreadMessageLike[] = [
    {
      id: `${run.run_id}:user`,
      role: "user",
      content: run.task,
      createdAt: new Date(run.created_at),
    },
  ];
  for (const [index, guidance] of run.operator_guidance.entries()) {
    messages.push({
      id: `${run.run_id}:guidance:${index}`,
      role: "user",
      content: guidance,
      createdAt: new Date(run.updated_at),
    });
  }
  messages.push({
    id: `${run.run_id}:assistant`,
    role: "assistant",
    content,
    createdAt: new Date(run.updated_at),
    status: assistantStatus(run),
  });
  return messages;
}
