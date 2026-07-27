// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { ComputerSheet } from "@/components/workspace/computer-sheet";
import type { RunSnapshot } from "@/types";

afterEach(cleanup);

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
  };
});

describe("ComputerSheet", () => {
  it("shows configured connection state instead of an endless frame skeleton before a run", () => {
    render(
      <ComputerSheet
        open
        onOpenChange={vi.fn()}
        token="workspace-token"
        run={null}
        connectionEnabled
        connectionMcpName="PiKVM lab"
        connectionMachineName="Windows acceptance VM"
        onPause={vi.fn()}
        onContinue={vi.fn()}
      />,
    );

    expect(
      screen.getByText("PiKVM lab is configured"),
    ).toBeVisible();
    expect(
      screen.getByText("Windows acceptance VM · no active session"),
    ).toBeVisible();
    expect(
      screen.getByText(/live screen appears here when a task starts/i),
    ).toBeVisible();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("explains chat-only mode when computer control is disabled", () => {
    render(
      <ComputerSheet
        open
        onOpenChange={vi.fn()}
        token="workspace-token"
        run={null}
        connectionEnabled={false}
        onPause={vi.fn()}
        onContinue={vi.fn()}
      />,
    );

    expect(screen.getByText("No managed computer configured")).toBeVisible();
    expect(
      screen.getByText(/chat and research tools remain available/i),
    ).toBeVisible();
  });

  it("does not show a loading frame for a chat turn with no computer session", () => {
    render(
      <ComputerSheet
        open
        onOpenChange={vi.fn()}
        token="workspace-token"
        run={
          {
            run_id: "chat-only-run",
            task: "Hello",
            status: "completed",
            origin: "managed",
            created_at: "2026-07-27T12:00:00Z",
            updated_at: "2026-07-27T12:00:01Z",
            event_count: 1,
            event_cursor: 1,
            operator_guidance: [],
            events: [],
            events_truncated: false,
          } satisfies RunSnapshot
        }
        connectionEnabled
        onPause={vi.fn()}
        onContinue={vi.fn()}
      />,
    );

    expect(
      screen.getByText("Managed PiKVM MCP is configured"),
    ).toBeVisible();
    expect(screen.queryByText("completed")).toBeNull();
  });
});
