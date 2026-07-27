import { describe, expect, it } from "vitest";
import {
  messagesForRun,
  userFacingCompletionSummary,
} from "@/lib/run-messages";
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
      sequence: 0,
      at: "2026-07-27T11:59:59Z",
      kind: "action.checkpointed",
      data: {
        index: 1,
        idempotency_key: "run-1:action:1:abc",
        intent: "Activate the visible calculator button",
        expected_evidence: ["The button shows its pressed state."],
      },
    },
    {
      sequence: 1,
      at: "2026-07-27T12:00:00Z",
      kind: "action.attempted",
      data: {
        call_id: "call-1",
        index: 1,
        attempt: 2,
        idempotency_key: "run-1:action:1:abc",
        tool: "pikvm_run_burst",
        arguments: {
          actions: [{ type: "click", x: 400, y: 300 }],
          idempotency_key: "run-1:action:1:abc",
        },
      },
    },
    {
      sequence: 2,
      at: "2026-07-27T12:00:01Z",
      kind: "action.completed",
      data: {
        call_id: "call-1",
        frame_id: 12,
        world_version: 9,
        latency_ms: 742,
      },
    },
  ],
  events_truncated: false,
  ...overrides,
});

describe("messagesForRun", () => {
  it("shows the exact checkpointed input before HID dispatch starts", () => {
    const assistant = messagesForRun(
      run({
        event_count: 1,
        event_cursor: 1,
        events: [
          {
            sequence: 1,
            at: "2026-07-27T12:00:00Z",
            kind: "action.checkpointed",
            data: {
              index: 0,
              idempotency_key: "run-1:action:0:exact",
              intent: "Type the requested words exactly.",
              actions: [
                {
                  type: "type_text",
                  text: "exactly one space",
                },
              ],
              expected_evidence: [
                "The target field contains exactly one space.",
              ],
            },
          },
        ],
      }),
    ).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      toolName: "pikvm_run_burst",
      args: {
        actions: [
          {
            type: "type_text",
            text: "exactly one space",
          },
        ],
        __receipt: {
          phase: "checkpointed",
          intent: "Type the requested words exactly.",
        },
      },
      result: undefined,
    });
  });

  it("keeps the tool identity when a checkpointed action starts dispatching", () => {
    const checkpoint = {
      sequence: 1,
      at: "2026-07-27T12:00:00Z",
      kind: "action.checkpointed",
      data: {
        index: 0,
        idempotency_key: "run-1:action:0:exact",
        intent: "Type the requested words exactly.",
        actions: [{ type: "type_text", text: "exactly one space" }],
        expected_evidence: ["The target field contains exactly one space."],
      },
    };
    const before = run({
      event_count: 1,
      event_cursor: 1,
      events: [checkpoint],
    });
    const after = run({
      event_count: 2,
      event_cursor: 2,
      events: [
        checkpoint,
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "action.attempted",
          data: {
            index: 0,
            attempt: 1,
            idempotency_key: "run-1:action:0:exact",
            call_id: "run-1:action:0:exact:attempt:1",
            tool: "pikvm_run_burst",
            arguments: {
              actions: [{ type: "type_text", text: "exactly one space" }],
            },
          },
        },
      ],
    });
    const toolCallId = (snapshot: RunSnapshot) => {
      const assistant = messagesForRun(snapshot).at(-1);
      return Array.isArray(assistant?.content)
        ? assistant.content.find((part) => part.type === "tool-call")
            ?.toolCallId
        : undefined;
    };

    expect(toolCallId(after)).toBe(toolCallId(before));
  });

  it("renders durable assistant conversation turns instead of treating chat as one computer task", () => {
    const messages = messagesForRun(
      run({
        mode: "assistant",
        status: "completed",
        conversation: [
          {
            message_id: "user-1",
            role: "user",
            content: "What are quarterly earnings?",
            created_at: "2026-07-27T12:00:00Z",
            event_cursor: 1,
          },
          {
            message_id: "assistant-1",
            role: "assistant",
            content: "Results published every three months.",
            created_at: "2026-07-27T12:00:01Z",
            event_cursor: 3,
          },
          {
            message_id: "user-2",
            role: "user",
            content: "Explain fiscal quarters too.",
            created_at: "2026-07-27T12:00:02Z",
            event_cursor: 4,
          },
          {
            message_id: "assistant-2",
            role: "assistant",
            content: "A fiscal year can start in any month.",
            created_at: "2026-07-27T12:00:03Z",
            event_cursor: 6,
          },
        ],
        events: [],
      }),
    );

    expect(messages.map((message) => message.content)).toEqual([
      "What are quarterly earnings?",
      "Results published every three months.",
      "Explain fiscal quarters too.",
      "A fiscal year can start in any month.",
    ]);
  });

  it("shows a general MCP tool call, exact arguments, result, and approval inline", () => {
    const snapshot = run({
      mode: "assistant",
      status: "needs_approval",
      conversation: [
        {
          message_id: "user-1",
          role: "user",
          content: "Send this email",
          created_at: "2026-07-27T12:00:00Z",
          event_cursor: 1,
        },
      ],
      pending_approval: {
        kind: "assistant_tool",
        approval_id: "approval-mail",
        tool: "mail.send",
        arguments: { to: "person@example.test", body: "Hello" },
        risk: "side_effect",
        reason: "This external tool may change state.",
      },
      events: [
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "tool.approval_required",
          data: {
            call_id: "approval-mail",
            tool: "mail.send",
            arguments: { to: "person@example.test", body: "Hello" },
            risk: "side_effect",
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      type: "tool-call",
      toolName: "mail.send",
      args: { to: "person@example.test", body: "Hello" },
      argsText: JSON.stringify(
        { to: "person@example.test", body: "Hello" },
        null,
        2,
      ),
      approval: {
        id: "approval-mail",
        options: [
          { kind: "allow-once", label: "Allow once" },
          { kind: "reject-once", label: "Deny" },
        ],
      },
    });
    expect(assistant?.status).toEqual({
      type: "requires-action",
      reason: "tool-calls",
    });
  });

  it("keeps completed general tool evidence in the assistant turn that used it", () => {
    const messages = messagesForRun(
      run({
        mode: "assistant",
        status: "completed",
        conversation: [
          {
            message_id: "user-1",
            role: "user",
            content: "Find the source",
            created_at: "2026-07-27T12:00:00Z",
            event_cursor: 1,
          },
          {
            message_id: "assistant-1",
            role: "assistant",
            content: "The source is available.",
            created_at: "2026-07-27T12:00:03Z",
            event_cursor: 5,
          },
          {
            message_id: "user-2",
            role: "user",
            content: "Now answer without a tool",
            created_at: "2026-07-27T12:00:04Z",
            event_cursor: 6,
          },
          {
            message_id: "assistant-2",
            role: "assistant",
            content: "This answer used no tool.",
            created_at: "2026-07-27T12:00:05Z",
            event_cursor: 8,
          },
        ],
        events: [
          {
            sequence: 2,
            at: "2026-07-27T12:00:01Z",
            kind: "tool.started",
            data: {
              call_id: "search-1",
              tool: "web.search",
              arguments: { query: "source" },
            },
          },
          {
            sequence: 3,
            at: "2026-07-27T12:00:02Z",
            kind: "tool.completed",
            data: {
              call_id: "search-1",
              tool: "web.search",
              content: '{"url":"https://example.test"}',
            },
          },
        ],
      }),
    );
    const firstAssistant = messages[1];
    const secondAssistant = messages[3];

    expect(firstAssistant?.content).toEqual([
      expect.objectContaining({
        type: "tool-call",
        toolName: "web.search",
        args: { query: "source" },
        result: '{"url":"https://example.test"}',
      }),
      { type: "text", text: "The source is available." },
    ]);
    expect(JSON.stringify(secondAssistant?.content)).not.toContain("web.search");
  });

  it("renders ordinary assistant conversation without a computer tool card", () => {
    const messages = messagesForRun(
      run({
        task: "hi",
        mode: "assistant",
        status: "completed",
        session_id: null,
        conversation: [
          {
            message_id: "user-1",
            role: "user",
            content: "hi",
            created_at: "2026-07-27T12:00:00Z",
          },
          {
            message_id: "assistant-1",
            role: "assistant",
            content: "Hi! How can I help?",
            created_at: "2026-07-27T12:00:01Z",
          },
        ],
        events: [],
        event_count: 0,
        event_cursor: 0,
      }),
    );

    expect(messages).toHaveLength(2);
    expect(messages[0]?.content).toBe("hi");
    expect(messages[1]?.content).toBe("Hi! How can I help?");
  });

  it("keeps one in-progress reply across streamed event updates", () => {
    const conversation = [
      {
        message_id: "user-turn-1",
        role: "user" as const,
        content: "what is on the screen",
        created_at: "2026-07-27T12:00:00Z",
        event_cursor: 0,
      },
    ];
    const before = run({
      task: "what is on the screen",
      mode: "assistant",
      status: "running",
      conversation,
      events: [],
      event_count: 1,
      event_cursor: 1,
    });
    const after = run({
      ...before,
      events: [
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "model.provider_started",
          data: { role: "assistant", provider: "claude-account" },
        },
      ],
      event_count: 2,
      event_cursor: 2,
    });

    expect(messagesForRun(before).at(-1)?.id).toBe(
      "run-1:assistant:reply-to:user-turn-1",
    );
    expect(messagesForRun(after).at(-1)?.id).toBe(
      messagesForRun(before).at(-1)?.id,
    );
  });

  it("keeps the reply identity when an in-progress assistant turn becomes durable", () => {
    const user = {
      message_id: "user-turn-1",
      role: "user" as const,
      content: "what is on the screen",
      created_at: "2026-07-27T12:00:00Z",
      event_cursor: 0,
    };
    const before = run({
      task: user.content,
      mode: "assistant",
      status: "running",
      conversation: [user],
      events: [],
      event_count: 1,
      event_cursor: 1,
    });
    const after = run({
      ...before,
      status: "completed",
      conversation: [
        user,
        {
          message_id: "server-generated-assistant-id",
          role: "assistant",
          content: "Here is what I can see.",
          created_at: "2026-07-27T12:00:01Z",
          event_cursor: 2,
        },
      ],
      event_count: 3,
      event_cursor: 3,
    });

    expect(messagesForRun(after).at(-1)?.id).toBe(
      messagesForRun(before).at(-1)?.id,
    );
  });

  it("keeps the hand-off reply identity when computer progress is appended", () => {
    const user = {
      message_id: "user-turn-1",
      role: "user" as const,
      content: "what is on the screen",
      created_at: "2026-07-27T12:00:00Z",
      event_cursor: 0,
    };
    const before = run({
      task: user.content,
      mode: "assistant",
      status: "running",
      conversation: [user],
      events: [],
      event_count: 1,
      event_cursor: 1,
    });
    const after = run({
      ...before,
      mode: "computer",
      status: "planning",
      conversation: [
        user,
        {
          message_id: "server-generated-handoff-id",
          role: "assistant",
          content: "Let me take a look at the screen.",
          created_at: "2026-07-27T12:00:01Z",
          event_cursor: 2,
        },
      ],
      events: [
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "assistant.computer_handoff",
          data: { task: "Inspect the connected screen." },
        },
      ],
      event_count: 2,
      event_cursor: 2,
    });

    const beforeReply = messagesForRun(before).at(-1);
    const afterMessages = messagesForRun(after);
    expect(afterMessages.at(-2)?.id).toBe(beforeReply?.id);
    expect(afterMessages.at(-1)?.id).not.toBe(beforeReply?.id);
  });

  it("shows a paused assistant turn as incomplete instead of a blank success", () => {
    const assistant = messagesForRun(
      run({
        task: "research the latest release",
        mode: "assistant",
        status: "paused",
        error: "assistant tool-round limit reached",
        conversation: [
          {
            message_id: "user-turn-1",
            role: "user",
            content: "research the latest release",
            created_at: "2026-07-27T12:00:00Z",
            event_cursor: 0,
          },
        ],
        events: [],
      }),
    ).at(-1);

    expect(assistant?.status).toEqual({
      type: "incomplete",
      reason: "error",
      error: "assistant tool-round limit reached",
    });
  });

  it("removes verifier mechanics and bounds legacy completion walls", () => {
    const summary = userFacingCompletionSummary(
      "The before/after comparison image shows two pixel-equivalent frames: " +
        "no input occurred (same frame_id 1, control_epoch 0). " +
        "The frame itself visibly contains every element the plan's success " +
        "criteria require: the Oracle Cloud console is open to Resource " +
        "Explorer in UK South (London), with the packer-image-build " +
        "compartment selected. ".repeat(40),
    );

    expect(summary).not.toContain("before/after comparison");
    expect(summary).not.toContain("success criteria");
    expect(summary).not.toContain("frame_id");
    expect(summary).toContain("The Oracle Cloud console");
    expect(summary).toContain("Details are available in Diagnostics");
    expect(summary.length).toBeLessThan(520);
  });

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
      args: {
        actions: [{ type: "click", x: 400, y: 300 }],
        __receipt: {
          intent: "Activate the visible calculator button",
          expected_evidence: ["The button shows its pressed state."],
          attempt: 2,
          latency_ms: 742,
          idempotency_key: "run-1:action:1:abc",
        },
      },
      result: {
        status: "completed",
        frame_id: 12,
        world_version: 9,
        attempted_at: "2026-07-27T12:00:00Z",
        completed_at: "2026-07-27T12:00:01Z",
      },
    });
    expect(tool).toMatchObject({
      argsText: expect.not.stringContaining("__receipt"),
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

  it("binds daemon typing read-back to the exact computer action", () => {
    const snapshot = run({
      events: [
        run().events[0]!,
        run().events[1]!,
        {
          ...run().events[2]!,
          data: {
            ...run().events[2]!.data,
            input_receipts: [
              {
                index: 0,
                type: "type_text",
                status: "verified_exact",
                verdict: "match",
                observed_text: "hello world",
                observed_text_redacted: false,
                typed_characters: 11,
                intended_characters: 11,
                correction_count: 1,
                delivery_retries: 0,
                used_fast_path: false,
                summary: "Typed and verified.",
                edit_distance: 0,
                focus_evidence: "read_back_verified",
                intended_sha256: "a".repeat(64),
                acknowledged_prefix_sha256: "a".repeat(64),
                observed_sha256: "a".repeat(64),
                exact_sha256_match: true,
              },
            ],
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      args: {
        __receipt: {
          input_receipts: [
            {
              index: 0,
              observed_text: "hello world",
              edit_distance: 0,
              focus_evidence: "read_back_verified",
              requested_characters: 11,
              issued_characters: 11,
              requested_sha256: "a".repeat(64),
              issued_prefix_sha256: "a".repeat(64),
              readback_sha256: "a".repeat(64),
              exact_readback_sha256_match: true,
            },
          ],
        },
      },
      argsText: expect.not.stringContaining("hello world"),
    });
  });

  it("binds production model and before-after evidence events to the action receipt", () => {
    const snapshot = run({
      events: [
        {
          sequence: 1,
          at: "2026-07-27T11:59:58Z",
          kind: "model.completed",
          data: {
            role: "controller",
            provider: "gemini-account",
            model: "gemini-3-flash",
            latency_ms: 320,
          },
        },
        {
          ...run().events[0]!,
          sequence: 2,
        },
        {
          ...run().events[1]!,
          sequence: 3,
        },
        {
          ...run().events[2]!,
          sequence: 4,
        },
        {
          sequence: 5,
          at: "2026-07-27T12:00:01Z",
          kind: "verification.evidence_captured",
          data: {
            revision: 7,
            action_index: 1,
            before_frame_id: 11,
            after_frame_id: 12,
          },
        },
        {
          sequence: 6,
          at: "2026-07-27T12:00:02Z",
          kind: "model.completed",
          data: {
            role: "verifier",
            provider: "claude-account",
            model: "claude-opus-4-8",
            verdict: "verified",
            summary: "Calculator now shows 42.",
            latency_ms: 940,
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      args: {
        __receipt: {
          evidence_revision: 7,
          evidence_before_frame_id: 11,
          evidence_after_frame_id: 12,
          controller: {
            provider: "gemini-account",
            model: "gemini-3-flash",
            latency_ms: 320,
          },
          verifier: {
            provider: "claude-account",
            model: "claude-opus-4-8",
            latency_ms: 940,
          },
        },
      },
      result: {
        verification: {
          verdict: "verified",
          summary: "Calculator now shows 42.",
          provider: "claude-account",
          model: "claude-opus-4-8",
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

  it("keeps a production-shaped direct click unverified and attaches its pre-action preview", () => {
    const snapshot = run({
      origin: "direct_mcp",
      events: [
        {
          sequence: 1,
          at: "2026-07-27T12:00:00Z",
          kind: "run.created",
          data: { origin: "direct_mcp" },
        },
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "action.pre_action_evidence_captured",
          data: {
            call_id: "direct-click",
            revision: 3,
            evidence_kind: "pre_action",
            before_frame_id: 17,
          },
        },
        {
          sequence: 3,
          at: "2026-07-27T12:00:02Z",
          kind: "action.attempted",
          data: {
            call_id: "direct-click",
            tool: "pikvm_click",
            arguments: { type: "click", x: 412, y: 286 },
          },
        },
        {
          sequence: 4,
          at: "2026-07-27T12:00:03Z",
          kind: "action.completed",
          data: {
            call_id: "direct-click",
            frame_id: 18,
            world_version: 18,
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      args: {
        __receipt: {
          evidence_revision: 3,
          evidence_kind: "pre_action",
          evidence_before_frame_id: 17,
        },
      },
      result: {
        status: "unverified",
      },
    });
  });

  it("shows exact direct typing as verified with its per-action caller", () => {
    const snapshot = run({
      origin: "direct_mcp",
      events: [
        {
          sequence: 1,
          at: "2026-07-27T12:00:00Z",
          kind: "action.attempted",
          data: {
            call_id: "direct-type",
            tool: "pikvm_type_text",
            arguments: { text: "quarterly earnings" },
            caller: {
              name: "claude-code",
              provider: "anthropic-oauth",
              model: "opus-4.8",
            },
          },
        },
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "action.completed",
          data: {
            call_id: "direct-type",
            effect_state: "verified",
            caller: {
              name: "claude-code",
              provider: "anthropic-oauth",
              model: "opus-4.8",
            },
            input_receipts: [
              {
                index: 0,
                type: "type_text",
                status: "verified_exact",
                verdict: "match",
                exact_sha256_match: true,
              },
            ],
          },
        },
      ],
    });
    const assistant = messagesForRun(snapshot).at(-1);
    const tool = Array.isArray(assistant?.content)
      ? assistant.content.find((part) => part.type === "tool-call")
      : undefined;

    expect(tool).toMatchObject({
      args: {
        __receipt: {
          caller: {
            name: "claude-code",
            provider: "anthropic-oauth",
            model: "opus-4.8",
          },
          input_receipts: [
            {
              status: "verified_exact",
              exact_readback_sha256_match: true,
            },
          ],
        },
      },
      result: {
        status: "completed",
        verification: {
          verdict: "verified",
            summary: "OCR read-back matched the delivery text exactly.",
        },
      },
    });
  });

  it("does not mark direct observation calls as unverified input", () => {
    const snapshot = run({
      origin: "direct_mcp",
      events: [
        {
          sequence: 1,
          at: "2026-07-27T12:00:00Z",
          kind: "action.attempted",
          data: {
            call_id: "direct-screen",
            tool: "pikvm_screenshot",
            arguments: { session_id: "session-1" },
          },
        },
        {
          sequence: 2,
          at: "2026-07-27T12:00:01Z",
          kind: "action.completed",
          data: {
            call_id: "direct-screen",
            effect_state: "not_applicable",
            frame_id: 22,
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
        status: "completed",
        frame_id: 22,
        verification: undefined,
      },
    });
  });

  it("exposes a pending dangerous action as inline approval choices", () => {
    const pending = run({
      status: "needs_approval",
      event_count: 2,
      event_cursor: 1,
      events: run().events.slice(0, 2),
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

  it("keeps internal planning prose out of the user-facing transcript", () => {
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

    expect(serialized).not.toContain("Use the smallest verifiable sequence.");
    expect(serialized).not.toContain("Open Calculator");
    expect(serialized).not.toContain("Enter the expression");
    expect(serialized).not.toContain("Model provider");
    expect(serialized).not.toContain("chain-of-thought");
    expect(serialized).not.toContain("success_criteria");
  });

  it("does not narrate a direct client trace as harness-owned work", () => {
    const assistant = messagesForRun(
      run({
        origin: "direct_mcp",
        status: "paused",
        plan: null,
        error: null,
      }),
    ).at(-1);
    const serialized = JSON.stringify(assistant?.content);

    expect(serialized).not.toContain("outer client chose");
    expect(serialized).toContain(
      "Direct gate paused. Resume it from the Computer view",
    );
    expect(serialized).not.toContain("Working through the requested task");
    expect(serialized).not.toContain("give a correction");
  });

  it("keeps orchestration timers and provider labels out of transcript prose", () => {
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

    const serialized = JSON.stringify(assistant?.content);
    expect(serialized).not.toContain("Checking the screen");
    expect(serialized).not.toContain("codex-account");
    expect(serialized).not.toContain("5s");
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
        run().events[1]!,
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
