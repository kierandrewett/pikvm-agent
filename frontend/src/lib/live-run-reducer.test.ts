import { describe, expect, it } from "vitest";
import {
  eventNeedsSnapshotReconciliation,
  preferNewestRunRevision,
  reduceRunEvent,
  reduceRunState,
} from "@/lib/live-run-reducer";
import type { HarnessEvent, RunSnapshot } from "@/types";

const run = (): RunSnapshot => ({
  run_id: "run-live",
  task: "Type the exact text",
  status: "running",
  origin: "managed",
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:00:00Z",
  event_count: 1,
  event_cursor: 1,
  operator_guidance: [],
  events: [
    {
      sequence: 1,
      at: "2026-07-27T12:00:00Z",
      kind: "model.started",
      data: { role: "controller" },
    },
  ],
  events_truncated: false,
});

const event = (
  sequence: number,
  kind: string,
  data: Record<string, unknown>,
): HarnessEvent => ({
  sequence,
  at: `2026-07-27T12:00:0${sequence}Z`,
  kind,
  data,
});

describe("live run reducer", () => {
  it("does not let an older snapshot overwrite newer streamed state", () => {
    const current: RunSnapshot = {
      ...run(),
      status: "running" as const,
      updated_at: "2026-07-27T12:00:03Z",
      event_count: 3,
      event_cursor: 3,
    };
    const olderCursor: RunSnapshot = {
      ...run(),
      status: "planning" as const,
      updated_at: "2026-07-27T12:00:04Z",
      event_count: 2,
      event_cursor: 2,
    };
    const olderAtSameCursor: RunSnapshot = {
      ...current,
      status: "planning" as const,
      updated_at: "2026-07-27T12:00:02Z",
    };

    expect(preferNewestRunRevision(current, olderCursor)).toBe(current);
    expect(preferNewestRunRevision(current, olderAtSameCursor)).toBe(current);
    expect(
      preferNewestRunRevision(current, {
        ...current,
        status: "verifying",
        updated_at: "2026-07-27T12:00:04Z",
      }).status,
    ).toBe("verifying");
  });

  it("projects provider phases immediately without fetching a snapshot", () => {
    const sent = reduceRunEvent(
      run(),
      event(2, "model.provider_request_sent", {
        role: "controller",
        provider: "fast-controller",
        model: "flash",
        attempt: 1,
      }),
    );
    expect(sent.gap).toBe(false);
    expect(sent.run.active_activity).toMatchObject({
      kind: "model",
      phase: "request_sent",
      role: "controller",
      provider: "fast-controller",
      model: "flash",
      attempt: 1,
    });

    const validating = reduceRunEvent(
      sent.run,
      event(3, "model.provider_validating", {
        role: "controller",
        provider: "fast-controller",
        model: "flash",
        attempt: 1,
      }),
    );
    expect(validating.run.active_activity?.phase).toBe("validating");
    expect(validating.run.events.map((item) => item.sequence)).toEqual([
      1, 2, 3,
    ]);

    const failover = reduceRunEvent(
      validating.run,
      event(4, "model.provider_failover", {
        role: "controller",
        from_provider: "fast-controller",
        to_provider: "strong-controller",
        provider: "strong-controller",
        attempt: 1,
      }),
    );
    expect(failover.run.active_activity).toMatchObject({
      phase: "failover",
      provider: "strong-controller",
    });
    expect(failover.run.active_activity?.model).toBeUndefined();
  });

  it("refuses to invent missing stream history", () => {
    const initial = run();
    const reduction = reduceRunEvent(
      initial,
      event(4, "model.provider_validating", {
        role: "controller",
      }),
    );

    expect(reduction.gap).toBe(true);
    expect(reduction.changed).toBe(false);
    expect(reduction.run).toBe(initial);
  });

  it("retains an exact action checkpoint before an attempt exists", () => {
    const checkpoint = reduceRunEvent(
      run(),
      event(2, "action.checkpointed", {
        index: 0,
        idempotency_key: "run-live:action:0:abc",
        intent: "Type the exact text",
        actions: [{ type: "type_text", text: "one space" }],
        expected_evidence: ["The exact text is visible."],
      }),
    );

    expect(checkpoint.run.events.at(-1)?.kind).toBe(
      "action.checkpointed",
    );
    expect(checkpoint.run.active_activity).toBeUndefined();
  });

  it("applies compact state messages without discarding streamed events", () => {
    const updated = reduceRunState(run(), {
      status: "running",
      active_activity: {
        kind: "model",
        started_at: "2026-07-27T12:00:00Z",
        phase: "failover",
        role: "controller",
        provider: "strong-controller",
      },
    });

    expect(updated.active_activity).toMatchObject({
      phase: "failover",
      provider: "strong-controller",
    });
    expect(updated.events).toHaveLength(1);
  });

  it("reconciles snapshots when a computer hand-off resolves", () => {
    expect(
      eventNeedsSnapshotReconciliation(
        event(2, "assistant.computer_handoff_started", {
          call_id: "handoff-1",
        }),
      ),
    ).toBe(true);
    expect(
      eventNeedsSnapshotReconciliation(
        event(2, "assistant.computer_handoff_failed", {
          call_id: "handoff-1",
        }),
      ),
    ).toBe(true);
    expect(
      eventNeedsSnapshotReconciliation(
        event(2, "assistant.computer_handoff_completed", {
          call_id: "handoff-1",
        }),
      ),
    ).toBe(true);
  });
});
