// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import {
  AssistantRuntimeProvider,
  ExportedMessageRepository,
  type ThreadMessage,
  type ThreadMessageLike,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { Thread } from "@/components/assistant-ui/thread";
import {
  DeferredComputerToolCall,
  DeferredWorkspaceToolGroup,
} from "@/components/workspace/deferred-computer-tools";
import { WorkspaceRuntimeBoundary } from "@/components/workspace/workspace-shell";

afterEach(cleanup);

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  };
  HTMLElement.prototype.scrollTo = vi.fn();
});

const messages: ThreadMessageLike[] = [
  {
    id: "user-1",
    role: "user",
    content: "What about now?",
  },
  {
    id: "assistant-1",
    role: "assistant",
    content: [],
    status: { type: "running" },
    metadata: { custom: {} },
  },
];

const handoffMessages: ThreadMessageLike[] = [
  {
    id: "user-1",
    role: "user",
    content: "What is on the screen?",
  },
  {
    id: "assistant-handoff",
    role: "assistant",
    content: "Let me take a look at the screen.",
    status: { type: "complete", reason: "stop" },
  },
  {
    id: "assistant-computer",
    role: "assistant",
    content: [],
    status: { type: "running" },
  },
];

const toolMessages: ThreadMessageLike[] = [
  {
    id: "user-tools",
    role: "user",
    content: "Find the current Python release.",
  },
  {
    id: "assistant-tools",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "search-1",
        toolName: "web.search_text",
        args: { query: "site:python.org latest Python release" },
        argsText: '{"query":"site:python.org latest Python release"}',
        result: { status: "completed" },
      },
      {
        type: "tool-call",
        toolCallId: "extract-1",
        toolName: "web.extract_content",
        args: { url: "https://www.python.org/downloads/" },
        argsText: '{"url":"https://www.python.org/downloads/"}',
        result: { status: "completed" },
      },
    ],
    status: { type: "complete", reason: "stop" },
  },
];

const runningToolMessages: ThreadMessageLike[] = [
  {
    id: "user-running-tool",
    role: "user",
    content: "Search Python.org.",
  },
  {
    id: "assistant-running-tool",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "search-running",
        toolName: "web.search_text",
        args: { query: "site:python.org latest Python release" },
        argsText: '{"query":"site:python.org latest Python release"}',
      },
    ],
    status: { type: "running" },
  },
];

const computerToolMessages: ThreadMessageLike[] = [
  {
    id: "user-computer-tools",
    role: "user",
    content: "Inspect two controls.",
  },
  {
    id: "assistant-computer-tools",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "computer-1",
        toolName: "pikvm_run_burst",
        args: {
          actions: [{ type: "click", x: 320, y: 240 }],
        },
        argsText: '{"actions":[{"type":"click","x":320,"y":240}]}',
        result: { status: "completed" },
      },
      {
        type: "tool-call",
        toolCallId: "computer-2",
        toolName: "pikvm_run_burst",
        args: {
          actions: [{ type: "click", x: 640, y: 480 }],
        },
        argsText: '{"actions":[{"type":"click","x":640,"y":480}]}',
        result: { status: "completed" },
      },
    ],
    status: { type: "complete", reason: "stop" },
  },
];

const approvalToolMessages: ThreadMessageLike[] = [
  {
    id: "user-approval-tool",
    role: "user",
    content: "Send the message.",
  },
  {
    id: "assistant-approval-tool",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "mail-approval",
        toolName: "mail.send",
        args: { to: "person@example.test", body: "Hello" },
        argsText: '{"to":"person@example.test","body":"Hello"}',
        approval: {
          id: "mail-approval",
          options: [
            {
              id: "approve",
              kind: "allow-once",
              label: "Allow once",
            },
            {
              id: "reject",
              kind: "reject-once",
              label: "Deny",
            },
          ],
        },
      },
    ],
    status: { type: "requires-action", reason: "tool-calls" },
  },
];

const completedApprovalToolMessages: ThreadMessageLike[] = [
  approvalToolMessages[0]!,
  {
    id: "assistant-approval-tool",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "mail-approval",
        toolName: "mail.send",
        args: { to: "person@example.test", body: "Hello" },
        argsText: '{"to":"person@example.test","body":"Hello"}',
        result: { status: "completed" },
      },
    ],
    status: { type: "complete", reason: "stop" },
  },
];

const failedToolMessages: ThreadMessageLike[] = [
  {
    id: "user-failed-tool",
    role: "user",
    content: "Search Python.org.",
  },
  {
    id: "assistant-failed-tool",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "search-failed",
        toolName: "web.search_text",
        args: { query: "site:python.org latest Python release" },
        argsText: '{"query":"site:python.org latest Python release"}',
        result: { status: "failed", error: "Search unavailable." },
        isError: true,
      },
    ],
    status: { type: "complete", reason: "stop" },
  },
];

const refusedToolMessages: ThreadMessageLike[] = [
  {
    id: "user-refused-tool",
    role: "user",
    content: "Send the message.",
  },
  {
    id: "assistant-refused-tool",
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "mail-refused",
        toolName: "mail.send",
        args: { to: "person@example.test", body: "Hello" },
        argsText: '{"to":"person@example.test","body":"Hello"}',
        result: { status: "refused", reason: "Denied by the operator." },
      },
    ],
    status: { type: "complete", reason: "stop" },
  },
];

function RunningThread() {
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning: true,
    onNew: async () => undefined,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          role: "reasoner",
        }}
      />
    </AssistantRuntimeProvider>
  );
}

function RunningHandoffThread() {
  const runtime = useExternalStoreRuntime({
    messages: handoffMessages,
    convertMessage: (message) => message,
    isRunning: true,
    onNew: async () => undefined,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          role: "reasoner",
        }}
      />
    </AssistantRuntimeProvider>
  );
}

function ToolThread({
  messages,
  isRunning = false,
  computerAware = false,
}: {
  messages: ThreadMessageLike[];
  isRunning?: boolean;
  computerAware?: boolean;
}) {
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isRunning,
    onNew: async () => undefined,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread
        components={
          computerAware
            ? {
                ToolFallback: DeferredComputerToolCall,
                ToolGroup: DeferredWorkspaceToolGroup,
              }
            : undefined
        }
      />
    </AssistantRuntimeProvider>
  );
}

function SwitchingThread({ selected }: { selected: boolean }) {
  const selectedMessages = selected ? messages : [];
  return (
    <WorkspaceRuntimeBoundary
      runtimeKey={selected ? "run-1" : "new-task"}
      adapter={{
        messages: selectedMessages,
        convertMessage: (message) => message,
        isRunning: selected,
        onNew: async () => undefined,
      }}
    >
      <Thread
        working={selected}
        activity={
          selected
            ? {
                kind: "model",
                started_at: "2026-07-27T12:00:00Z",
                role: "verifier",
              }
            : undefined
        }
      />
    </WorkspaceRuntimeBoundary>
  );
}

function BranchingThread() {
  const repository = ExportedMessageRepository.fromBranchableArray(
    [
      {
        message: {
          id: "user-old",
          role: "user",
          content: "Old request.",
        },
        parentId: null,
      },
      {
        message: {
          id: "assistant-old",
          role: "assistant",
          content: "Old transient reply.",
        },
        parentId: "user-old",
      },
      {
        message: {
          id: "user-current",
          role: "user",
          content: "What is on the screen?",
        },
        parentId: null,
      },
      {
        message: {
          id: "assistant-current",
          role: "assistant",
          content: "Current reply.",
        },
        parentId: "user-current",
      },
    ],
    { headId: "assistant-current" },
  );
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning: false,
    onNew: async () => undefined,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  );
}

describe("Thread progress", () => {
  it("renders model progress even when the assistant has no content yet", () => {
    render(<RunningThread />);

    expect(screen.getByRole("status")).toHaveTextContent("Planning the task");
  });

  it("renders active progress only on the latest assistant message", () => {
    render(<RunningHandoffThread />);

    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("Planning the task");
  });

  it("virtualizes old assistant messages without virtualizing the current one", () => {
    render(<RunningHandoffThread />);

    const assistantMessages = document.querySelectorAll(
      "[data-slot='aui_assistant-message-root']",
    );
    expect(assistantMessages).toHaveLength(2);
    expect(assistantMessages[0]).toHaveClass("[content-visibility:auto]");
    expect(assistantMessages[1]).not.toHaveClass("[content-visibility:auto]");
  });

  it("rebinds the assistant runtime when a new task receives its run id", () => {
    const view = render(<SwitchingThread selected={false} />);

    view.rerender(<SwitchingThread selected />);

    expect(screen.getByText("What about now?")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Checking the result");
  });

  it("does not present transient assistant reconciliation as reply versions", () => {
    render(<BranchingThread />);

    expect(screen.getByText("Current reply.")).toBeVisible();
    expect(
      document.querySelector("[data-slot='aui-branch-picker-root']"),
    ).toBeNull();
  });

  it("names compacted tool calls without opening the group", () => {
    render(<ToolThread messages={toolMessages} />);

    const trigger = screen.getByRole("button", {
      name: "web.search_text then web.extract_content, 2 tool calls, completed",
    });
    expect(trigger).toHaveTextContent("web.search_text → web.extract_content");
    expect(trigger).toHaveTextContent("Done");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("uses the computer activity group only for PiKVM action tools", async () => {
    const view = render(
      <ToolThread messages={computerToolMessages} computerAware />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /2 computer actions/i }),
      ).toHaveTextContent("Computer activity");
    });

    view.rerender(<ToolThread messages={toolMessages} computerAware />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", {
          name: "web.search_text then web.extract_content, 2 tool calls, completed",
        }),
      ).toBeVisible();
    });
    expect(
      screen.queryByRole("button", { name: /computer actions/i }),
    ).toBeNull();
  });

  it("shows the active tool and running state in the collapsed summary", () => {
    render(<ToolThread messages={runningToolMessages} isRunning />);

    const trigger = screen.getByRole("button", {
      name: "web.search_text, 1 tool call, running",
    });
    expect(trigger).toHaveTextContent("web.search_text");
    expect(trigger).toHaveTextContent("Running");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("opens approval-required tool groups and labels the review state", () => {
    render(<ToolThread messages={approvalToolMessages} isRunning />);

    const trigger = screen.getByRole("button", {
      name: "mail.send, 1 tool call, review required",
    });
    expect(trigger).toHaveTextContent("Review");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Allow once")).toBeVisible();
    const currentMessage = document.querySelector(
      "[data-slot='aui_assistant-message-root']",
    );
    expect(currentMessage).not.toBeNull();
    expect(currentMessage).not.toHaveClass("[content-visibility:auto]");
  });

  it("collapses the tool group after an approval-required turn resolves", async () => {
    const view = render(
      <ToolThread messages={approvalToolMessages} isRunning />,
    );
    expect(
      screen.getByRole("button", {
        name: "mail.send, 1 tool call, review required",
      }),
    ).toHaveAttribute("aria-expanded", "true");

    view.rerender(<ToolThread messages={completedApprovalToolMessages} />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", {
          name: "mail.send, 1 tool call, completed",
        }),
      ).toHaveAttribute("aria-expanded", "false");
    });
  });

  it("shows a failed state on a collapsed tool group", () => {
    render(<ToolThread messages={failedToolMessages} />);

    const trigger = screen.getByRole("button", {
      name: "web.search_text, 1 tool call, failed",
    });
    expect(trigger).toHaveTextContent("Failed");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("shows a refused state instead of presenting denial as done", () => {
    render(<ToolThread messages={refusedToolMessages} />);

    const trigger = screen.getByRole("button", {
      name: "mail.send, 1 tool call, refused",
    });
    expect(trigger).toHaveTextContent("Refused");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });
});
