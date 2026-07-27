import { describe, expect, it } from "vitest";
import { messagesForRun } from "@/lib/run-messages";
import type { RunSnapshot } from "@/types";

const run = (overrides: Partial<RunSnapshot> = {}): RunSnapshot => ({
  run_id: "run-1",
  task: "Open Calculator and add 40 and 2",
  status: "running",
  origin: "managed",
  model_provider: "fast-controller",
  session_id: "session-1",
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:00:01Z",
  event_count: 2,
  event_cursor: 2,
  operator_guidance: [],
  events: [
    {
      sequence: 1,
      at: "2026-07-27T12:00:00Z",
      kind: "action.attempted",
      data: {
        call_id: "call-1",
        tool: "pikvm_run_burst",
        arguments: { actions: [{ type: "click", x: 400, y: 300 }] },
      },
    },
    {
      sequence: 2,
      at: "2026-07-27T12:00:01Z",
      kind: "action.completed",
      data: { call_id: "call-1", frame_id: 12, world_version: 9 },
    },
  ],
  events_truncated: false,
  ...overrides,
});

describe("messagesForRun", () => {
  it("turns harness events into structured assistant-ui tool parts", () => {
    const messages = messagesForRun(run());
    const assistant = messages.at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(messages[0]?.content).toBe("Open Calculator and add 40 and 2");
    expect(tool).toMatchObject({
      type: "tool-call",
      toolName: "pikvm_run_burst",
      args: { actions: [{ type: "click", x: 400, y: 300 }] },
      result: { status: "completed", frame_id: 12, world_version: 9 },
    });
  });

  it("exposes a pending dangerous action as inline approval choices", () => {
    const pending = run({
      status: "needs_approval",
      event_count: 1,
      event_cursor: 1,
      events: run().events.slice(0, 1),
      pending_approval: { approval_id: "approval-9" },
    });
    const assistant = messagesForRun(pending).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(assistant?.status).toEqual({
      type: "requires-action",
      reason: "tool-calls",
    });
    expect(tool).toMatchObject({
      approval: {
        id: "approval-9",
        options: [
          {
            kind: "allow-once",
            label: "Allow once",
            confirm: { title: "Allow this computer action?" },
          },
          { kind: "reject-once", label: "Deny" },
        ],
      },
    });
  });

  it("does not duplicate a call when active activity has no call id", () => {
    const snapshot = run({
      active_activity: {
        kind: "tool",
        started_at: "2026-07-27T12:00:00Z",
        tool: "pikvm_run_burst",
        arguments: { actions: [{ type: "click", x: 400, y: 300 }] },
      },
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tools = Array.isArray(assistant?.content)
      ? assistant.content.filter((part) => part.type === "tool-call")
      : [];

    expect(tools).toHaveLength(1);
  });

  it("shows explicit plans and status without inventing hidden reasoning", () => {
    const messages = messagesForRun(
      run({
        plan: {
          summary: "Use the smallest verifiable sequence.",
          steps: ["Open Calculator", "Enter the expression"],
          success_criteria: ["Result reads 42"],
          constraints: ["Do not send messages"],
        },
      }),
    );
    const serialized = JSON.stringify(messages);

    expect(serialized).toContain("Use the smallest verifiable sequence.");
    expect(serialized).toContain("Model provider");
    expect(serialized).not.toContain("chain-of-thought");
    expect(serialized).not.toContain("success_criteria");
  });
});
