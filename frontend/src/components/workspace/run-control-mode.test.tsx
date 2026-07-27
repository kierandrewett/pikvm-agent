// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  canComposeIntoRun,
  DirectRunBanner,
  RunControlModeBadge,
  usesManagedControlLoop,
} from "@/components/workspace/run-control-mode";

afterEach(cleanup);

describe("RunControlModeBadge", () => {
  it("labels a new or managed task as harness-owned", () => {
    const { rerender } = render(<RunControlModeBadge />);

    expect(screen.getByText("Managed harness")).toHaveAttribute(
      "title",
      expect.stringContaining("plans, acts, verifies"),
    );

    rerender(<RunControlModeBadge origin="managed" />);
    expect(screen.getByText("Managed harness")).toBeInTheDocument();
  });

  it("does not present a direct client trace as managed", () => {
    render(<RunControlModeBadge origin="direct_mcp" />);

    expect(screen.getByText("Guarded direct")).toHaveAttribute(
      "title",
      expect.stringContaining("outer coding client"),
    );
    expect(screen.queryByText("Managed harness")).toBeNull();
  });
});

describe("DirectRunBanner", () => {
  it("states who controls a direct run and how to return to managed mode", () => {
    render(<DirectRunBanner />);

    expect(
      screen.getByText(/Claude, Codex, Gemini, or OpenCode is choosing/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Start a new task for harness-managed execution/i),
    ).toBeInTheDocument();
  });
});

describe("canComposeIntoRun", () => {
  it("keeps active direct traces read-only in the chat", () => {
    expect(canComposeIntoRun(false, undefined)).toBe(false);
    expect(canComposeIntoRun(true, undefined)).toBe(true);
    expect(canComposeIntoRun(true, "managed")).toBe(true);
    expect(canComposeIntoRun(true, "direct_mcp")).toBe(false);
  });

  it("keeps managed model routing out of direct traces", () => {
    expect(usesManagedControlLoop(undefined)).toBe(true);
    expect(usesManagedControlLoop("managed")).toBe(true);
    expect(usesManagedControlLoop("direct_mcp")).toBe(false);
  });
});
