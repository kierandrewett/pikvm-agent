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
      result: {
        status: "completed",
        frame_id: 12,
        world_version: 9,
        attempted_at: "2026-07-27T12:00:00Z",
        completed_at: "2026-07-27T12:00:01Z",
      },
    });
  });

  it("attaches independent screen verification to the action it follows", () => {
    const snapshot = run({
      events: [
        ...run().events,
        {
          sequence: 3,
          at: "2026-07-27T12:00:02Z",
          kind: "verification.completed",
          data: {
            verdict: "verified",
            summary: "Calculator now shows 42.",
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      result: {
        verification: {
          verdict: "verified",
          summary: "Calculator now shows 42.",
          observed_at: "2026-07-27T12:00:02Z",
        },
      },
    });
  });

  it("does not attach a later verification after the next action starts", () => {
    const snapshot = run({
      events: [
        ...run().events,
        {
          sequence: 3,
          at: "2026-07-27T12:00:02Z",
          kind: "action.attempted",
          data: {
            call_id: "call-2",
            tool: "pikvm_key",
            arguments: { actions: [{ type: "key", keys: ["ENTER"] }] },
          },
        },
        {
          sequence: 4,
          at: "2026-07-27T12:00:03Z",
          kind: "verification.completed",
          data: { verdict: "verified", summary: "Second action verified." },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tools = Array.isArray(assistant?.content)
      ? assistant.content.filter((part) => part.type === "tool-call")
      : [];

    expect(tools[0]).toMatchObject({
      result: { verification: undefined },
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

  it("keeps the plan summary in chat without flooding it with execution steps", () => {
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
    const serialized = JSON.stringify(messages.at(-1)?.content);

    expect(serialized).toContain("Use the smallest verifiable sequence.");
    expect(serialized).not.toContain("Open Calculator");
    expect(serialized).not.toContain("Enter the expression");
    expect(serialized).not.toContain("Model provider");
    expect(serialized).not.toContain("chain-of-thought");
    expect(serialized).not.toContain("success_criteria");
  });

  it("names the live model lane without inventing hidden reasoning", () => {
    const assistant = messagesForRun(
      run({
        active_activity: {
          kind: "model",
          started_at: new Date(Date.now() - 5_000).toISOString(),
          role: "verifier",
          provider: "codex-account",
          model: "gpt-5",
        },
      }),
    ).at(-1);

    expect(JSON.stringify(assistant?.content)).toContain(
      "Checking the screen · gpt-5 · codex-account · 5s",
    );
  });

  it("surfaces the actual reason a run paused", () => {
    const assistant = messagesForRun(
      run({
        status: "paused",
        error: "all providers unavailable for reasoner: codex-account=timeout",
      }),
    ).at(-1);

    expect(JSON.stringify(assistant?.content)).toContain(
      "Paused: all providers unavailable for reasoner: codex-account=timeout.",
    );
  });

  it("shows a stale-world refusal as safe recovery, not a running input", () => {
    const snapshot = run({
      events: [
        run().events[0]!,
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "action.stale_world_refreshed",
          data: {
            call_id: "call-1",
            status: "stale_world",
            refused_world_version: 9,
            fresh_world_version: 10,
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      result: {
        status: "refused",
        reason: "stale world; screen refreshed",
        world_version: 10,
      },
    });
  });
});
