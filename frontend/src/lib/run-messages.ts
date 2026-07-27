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

const eventIdentity = (event: HarnessEvent) =>
  safeString(event.data.call_id) ||
  String(safeNumber(event.data.index) ?? event.sequence);

const SAFE_REFUSAL_OUTCOMES = new Set([
  "action.refused_by_policy",
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

const preActionEvidenceForAttempt = (
  attempt: HarnessEvent,
  events: readonly HarnessEvent[],
) => {
  const identity = eventIdentity(attempt);
  return [...events]
    .reverse()
    .find(
      (event) =>
        event.sequence < attempt.sequence &&
        event.kind === "action.pre_action_evidence_captured" &&
        eventIdentity(event) === identity,
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

const callerReceipt = (value: unknown) => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const caller = value as Record<string, unknown>;
  const receipt = {
    label: safeString(caller.label),
    name: safeString(caller.name),
    version: safeString(caller.version),
    provider: safeString(caller.provider),
    model: safeString(caller.model),
  };
  return Object.values(receipt).some(Boolean) ? receipt : undefined;
};

const displayActionsForTool = (
  tool: string,
  arguments_: Record<string, unknown>,
) => {
  if (Array.isArray(arguments_.actions)) return arguments_.actions;
  if (tool === "pikvm_type_text") {
    return [
      {
        type: "type_text",
        text: arguments_.text,
        secret: arguments_.secret === true,
      },
    ];
  }
  if (tool === "pikvm_click") {
    return [
      {
        type: "click",
        x: arguments_.x,
        y: arguments_.y,
        button: arguments_.button,
      },
    ];
  }
  if (tool === "pikvm_key") {
    return [{ type: "key", keys: arguments_.keys }];
  }
  if (tool === "pikvm_scroll") {
    return [
      {
        type: "scroll",
        direction: arguments_.direction,
        amount: arguments_.amount,
      },
    ];
  }
  return undefined;
};

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
      proof_state: safeString(item.proof_state),
      observed_text: safeString(item.observed_text),
      observed_text_redacted: item.observed_text_redacted === true,
      requested_characters: safeNumber(
        item.requested_characters ?? item.intended_characters,
      ),
      issued_characters: safeNumber(
        item.issued_characters ?? item.typed_characters,
      ),
      observed_characters: safeNumber(item.observed_characters),
      correction_count: safeNumber(item.correction_count),
      delivery_retries: safeNumber(item.delivery_retries),
      used_fast_path: item.used_fast_path === true,
      summary: safeString(item.summary),
      edit_distance: safeNumber(item.edit_distance),
      focus_evidence: safeString(item.focus_evidence),
      requested_sha256: safeString(
        item.requested_sha256 || item.intended_sha256,
      ),
      issued_prefix_sha256: safeString(
        item.issued_prefix_sha256 || item.acknowledged_prefix_sha256,
      ),
      readback_sha256: safeString(
        item.readback_sha256 || item.observed_sha256,
      ),
      exact_readback_sha256_match:
        typeof (
          item.exact_readback_sha256_match ?? item.exact_sha256_match
        ) === "boolean"
          ? Boolean(
              item.exact_readback_sha256_match ?? item.exact_sha256_match,
            )
          : undefined,
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

const COMPLETION_SUMMARY_LIMIT = 420;

export const userFacingCompletionSummary = (value: unknown) => {
  let summary = safeString(value).trim();
  if (!summary) return "";

  summary = summary
    .replace(/^The before\/after comparison image[^.]*\.\s*/i, "")
    .replace(
      /^The frame itself visibly contains[^:]{0,240}:\s*/i,
      "",
    )
    .trim();
  if (summary) {
    summary = `${summary[0]!.toUpperCase()}${summary.slice(1)}`;
  }
  if (summary.length <= COMPLETION_SUMMARY_LIMIT) return summary;

  const candidate = summary.slice(0, COMPLETION_SUMMARY_LIMIT + 1);
  const sentenceEnd = Math.max(
    candidate.lastIndexOf(". "),
    candidate.lastIndexOf("! "),
    candidate.lastIndexOf("? "),
  );
  const wordEnd = candidate.lastIndexOf(" ");
  const cutAt =
    sentenceEnd >= COMPLETION_SUMMARY_LIMIT * 0.55
      ? sentenceEnd + 1
      : wordEnd > 0
        ? wordEnd
        : COMPLETION_SUMMARY_LIMIT;
  return (
    `${summary.slice(0, cutAt).trim()}…\n\n` +
    "_Details are available in Diagnostics._"
  );
};

const completionMarkdown = (run: RunSnapshot) => {
  if (run.status === "completed") {
    const summary = userFacingCompletionSummary(
      run.last_verification?.summary,
    );
    return summary || "Completed and checkpointed.";
  }
  if (["failed", "blocked", "rejected", "aborted"].includes(run.status)) {
    return `Stopped: ${run.error || run.status.replaceAll("_", " ")}.`;
  }
  if (run.status === "paused") {
    if (run.origin === "direct_mcp") {
      return (
        "Direct gate paused. Resume it from the Computer view; the next " +
        "action still comes from the outer client."
      );
    }
    return run.error
      ? `Paused: ${run.error}. Retry, choose another model, or give a correction.`
      : "Paused at a durable checkpoint. You can continue or give a correction.";
  }
  if (run.status === "needs_approval") {
    return "";
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
    const evidence =
      preActionEvidenceForAttempt(attempt, run.events) ??
      evidenceForOutcome(outcome, run.events);
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
    const effectState = safeString(outcome?.data.effect_state);
    const directEffectVerified =
      run.origin === "direct_mcp" && effectState === "verified";
    const directEffectNotApplicable =
      run.origin === "direct_mcp" && effectState === "not_applicable";
    const directLegacyUnverified =
      run.origin === "direct_mcp" &&
      !verification &&
      !directEffectVerified &&
      !directEffectNotApplicable;
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
                : outcome.kind === "action.completed_unverified" ||
                    effectState === "unverified" ||
                    directLegacyUnverified
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
                : directEffectVerified
                  ? {
                      verdict: "verified",
                      summary: "Exact target read-back matched.",
                      observed_at: outcome.at,
                      provider: "",
                      model: "",
                      latency_ms: undefined,
                    }
                  : undefined,
            };
    const exactArgs: Record<string, unknown> =
      attempt.data.arguments &&
      typeof attempt.data.arguments === "object" &&
      !Array.isArray(attempt.data.arguments)
        ? (attempt.data.arguments as Record<string, unknown>)
        : {};
    const toolName = safeString(attempt.data.tool) || "MCP tool";
    const displayActions = displayActionsForTool(toolName, exactArgs);
    const args = {
      ...exactArgs,
      ...(displayActions ? { actions: displayActions } : {}),
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
        evidence_kind: safeString(evidence?.data.evidence_kind),
        evidence_before_frame_id: safeNumber(evidence?.data.before_frame_id),
        evidence_after_frame_id: safeNumber(evidence?.data.after_frame_id),
        controller: modelReceipt(controller),
        verifier: modelReceipt(verification),
        caller: callerReceipt(
          outcome?.data.caller ?? attempt.data.caller,
        ),
        input_receipts: inputReceipts(outcome?.data.input_receipts),
      },
    };
    return {
      type: "tool-call" as const,
      toolCallId: `${run.run_id}:${eventIdentity(attempt)}`,
      toolName,
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

const assistantToolParts = (
  run: RunSnapshot,
  events: readonly HarnessEvent[],
) => {
  const candidates = events.filter(
    (event) =>
      event.kind === "tool.started" ||
      event.kind === "tool.approval_required",
  );
  const calls = new Map<string, HarnessEvent>();
  for (const event of candidates) {
    const identity = safeString(event.data.call_id) || String(event.sequence);
    if (!calls.has(identity)) calls.set(identity, event);
  }
  return [...calls.entries()].map(([callId, attempt]) => {
    const outcome = events.find(
      (event) =>
        event.sequence > attempt.sequence &&
        safeString(event.data.call_id) === callId &&
        ["tool.completed", "tool.failed", "tool.refused"].includes(event.kind),
    );
    const toolName = safeString(attempt.data.tool) || "Tool";
    const exactArgs =
      attempt.data.arguments &&
      typeof attempt.data.arguments === "object" &&
      !Array.isArray(attempt.data.arguments)
        ? (attempt.data.arguments as Record<string, unknown>)
        : {};
    const approval =
      safeString(run.pending_approval?.approval_id) === callId
        ? run.pending_approval
        : null;
    const failed = outcome?.kind === "tool.failed";
    const refused = outcome?.kind === "tool.refused";
    const result =
      outcome == null
        ? undefined
        : failed
          ? {
              status: "failed",
              error:
                safeString(outcome.data.error) || "Tool execution failed.",
            }
          : refused
            ? {
                status: "refused",
                reason:
                  safeString(outcome.data.reason) || "Denied by the operator.",
              }
            : safeString(outcome.data.content) || {
                status: "completed",
              };
    const risk = safeString(approval?.risk).replaceAll("_", " ");
    const reason = safeString(approval?.reason);
    return {
      type: "tool-call" as const,
      toolCallId: `${run.run_id}:${callId}`,
      toolName,
      args: exactArgs as never,
      argsText: JSON.stringify(exactArgs, null, 2),
      result,
      isError: failed,
      approval: approval
        ? {
            id: callId,
            options: [
              {
                id: "approve",
                kind: "allow-once" as const,
                label: "Allow once",
                description:
                  [risk, reason].filter(Boolean).join(": ") ||
                  "Permit this exact external tool call once.",
                confirm: {
                  title: `Allow ${toolName}?`,
                  description:
                    reason ||
                    "The exact arguments shown here will be sent once.",
                },
              },
              {
                id: "reject",
                kind: "reject-once" as const,
                label: "Deny",
              },
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
  if (
    ["paused", "failed", "blocked", "rejected", "aborted"].includes(
      run.status,
    )
  ) {
    return {
      type: "incomplete",
      reason: run.status === "aborted" ? "cancelled" : "error",
      error: run.error || (run.status === "paused" ? "Paused" : run.status),
    };
  }
  return { type: "complete", reason: "stop" };
};

export function messagesForRun(run: RunSnapshot | null): ThreadMessageLike[] {
  if (!run) return [];
  if ((run.conversation?.length ?? 0) > 0) {
    const messages: ThreadMessageLike[] = [];
    let precedingCursor = 0;
    for (const message of run.conversation!) {
      const messageCursor = message.event_cursor ?? precedingCursor;
      if (message.role === "user") {
        messages.push({
          id: `${run.run_id}:${message.message_id}`,
          role: "user",
          content: message.content,
          createdAt: new Date(message.created_at),
        });
        precedingCursor = messageCursor;
        continue;
      }
      const turnEvents = run.events.filter(
        (event) =>
          event.sequence > precedingCursor &&
          event.sequence <= messageCursor,
      );
      const tools = assistantToolParts(run, turnEvents);
      messages.push({
        id: `${run.run_id}:${message.message_id}`,
        role: "assistant",
        content:
          tools.length > 0
            ? [
                ...tools,
                { type: "text" as const, text: message.content },
              ]
            : message.content,
        createdAt: new Date(message.created_at),
        status: { type: "complete", reason: "stop" },
      });
      precedingCursor = messageCursor;
    }
    const latest = run.conversation!.at(-1);
    if (run.mode === "assistant" && latest?.role === "user") {
      const activeEvents = run.events.filter(
        (event) => event.sequence > (latest.event_cursor ?? precedingCursor),
      );
      messages.push({
        id: `${run.run_id}:assistant:reply-to:${latest.message_id}`,
        role: "assistant",
        content: assistantToolParts(run, activeEvents),
        createdAt: new Date(run.updated_at),
        status: assistantStatus(run),
      });
      return messages;
    }
    if (run.mode === "assistant") return messages;

    const completion = completionMarkdown(run);
    const content: ThreadMessageLike["content"] = [
      ...toolParts(run),
      ...(completion ? [{ type: "text" as const, text: completion }] : []),
    ];
    messages.push({
      id: `${run.run_id}:computer:reply-to:${latest?.message_id ?? "task"}`,
      role: "assistant",
      content,
      createdAt: new Date(run.updated_at),
      status: assistantStatus(run),
    });
    return messages;
  }
  const completion = completionMarkdown(run);
  const content: ThreadMessageLike["content"] = [
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
