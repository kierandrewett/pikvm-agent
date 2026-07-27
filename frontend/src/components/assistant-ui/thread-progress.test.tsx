// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import {
  AssistantRuntimeProvider,
  ExportedMessageRepository,
  type ThreadMessage,
  type ThreadMessageLike,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { Thread } from "@/components/assistant-ui/thread";
import { WorkspaceRuntimeBoundary } from "@/components/workspace/workspace-shell";

afterEach(cleanup);

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  };
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

  it("rebinds the assistant runtime when a new task receives its run id", () => {
    const view = render(<SwitchingThread selected={false} />);

    view.rerender(<SwitchingThread selected />);

    expect(screen.getByText("What about now?")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking the result",
    );
  });

  it("does not present transient assistant reconciliation as reply versions", () => {
    render(<BranchingThread />);

    expect(screen.getByText("Current reply.")).toBeVisible();
    expect(
      document.querySelector("[data-slot='aui-branch-picker-root']"),
    ).toBeNull();
  });
});
