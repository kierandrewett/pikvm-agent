import type { ThreadMessageLike } from "@assistant-ui/react";
import type { HarnessEvent, RunSnapshot } from "@/types";

const ACTIVE_STATUSES = new Set([
  "created",
  "planning",
  "running",
  "executing",
  "verifying",
]);

const safeString = (value: unknown) =>
  typeof value === "string" ? value : "";

const safeNumber = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const eventIdentity = (event: HarnessEvent) =>
  safeString(event.data.call_id) ||
  String(safeNumber(event.data.index) ?? event.sequence);

const latestOutcome = (
  attempt: HarnessEvent,
  events: readonly HarnessEvent[],
) => {
  const identity = eventIdentity(attempt);
  return [...events]
    .reverse()
    .find(
      (event) =>
        ["action.completed", "action.failed", "action.refused_by_operator"].includes(
          event.kind,
        ) && eventIdentity(event) === identity,
    );
};

const planMarkdown = (run: RunSnapshot) => {
  const lines: string[] = [];
  if (run.plan) {
    lines.push(run.plan.summary);
    if (run.plan.steps.length) {
      lines.push(
        "",
        ...run.plan.steps.map((step, index) => `${index + 1}. ${step}`),
      );
    }
  } else if (run.status === "planning") {
    lines.push("Planning the work and defining visible completion evidence.");
  } else {
    lines.push("Working through the requested task.");
  }
  if (run.model_provider) {
    lines.push("", `Model provider: \`${run.model_provider}\``);
  }
  return lines.join("\n");
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
    return "Paused at a durable checkpoint. You can continue or give a correction.";
  }
  if (run.status === "needs_approval") {
    return "This action needs your approval before the computer can continue.";
  }
  const activity = run.active_activity;
  if (activity?.kind === "model") {
    const identity = [activity.model, activity.provider].filter(Boolean).join(" · ");
    return identity ? `Working with ${identity}.` : "The model is working.";
  }
  return "";
};

const toolParts = (run: RunSnapshot) => {
  const attempts = run.events
    .filter((event) => event.kind === "action.attempted")
    .slice(-12);
  const activeCallId = safeString(run.active_activity?.call_id);
  const activeAlreadyRepresented = activeCallId
    ? attempts.some(
        (event) => safeString(event.data.call_id) === activeCallId,
      )
    : attempts.length > 0;
  if (
    run.active_activity?.kind === "tool" &&
    !activeAlreadyRepresented
  ) {
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
    const outcome = latestOutcome(attempt, run.events);
    const isPending = !outcome && index === attempts.length - 1;
    const approval = isPending ? run.pending_approval : null;
    const approvalId = safeString(approval?.approval_id);
    const approvalRisk = safeString(approval?.risk)
      .replaceAll("_", " ")
      .trim();
    const approvalReason = safeString(approval?.reason).trim();
    const approvalDescription = [approvalRisk, approvalReason]
      .filter(Boolean)
      .join(": ");
    const failed = outcome?.kind === "action.failed";
    const result =
      outcome == null
        ? undefined
        : failed
          ? { status: "failed", error: outcome.data.error }
          : {
              status:
                outcome.kind === "action.refused_by_operator"
                  ? "refused"
                  : "completed",
              frame_id: outcome.data.frame_id,
              world_version: outcome.data.world_version,
            };
    const args =
      attempt.data.arguments &&
      typeof attempt.data.arguments === "object" &&
      !Array.isArray(attempt.data.arguments)
        ? attempt.data.arguments
        : {};
    return {
      type: "tool-call" as const,
      toolCallId: `${run.run_id}:${eventIdentity(attempt)}`,
      toolName: safeString(attempt.data.tool) || "MCP tool",
      args: args as never,
      argsText: JSON.stringify(args, null, 2),
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
