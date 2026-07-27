// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  canComposeIntoRun,
  directCallerSummary,
  DirectRunBanner,
  RunControlModeBadge,
  usesManagedControlLoop,
} from "@/components/workspace/run-control-mode";

afterEach(cleanup);

describe("RunControlModeBadge", () => {
  it("labels a new or managed task as harness-owned", () => {
    const { rerender } = render(<RunControlModeBadge />);

    expect(screen.getByText("Managed")).toHaveAttribute(
      "title",
      expect.stringContaining("plans, acts, verifies"),
    );

    rerender(<RunControlModeBadge origin="managed" />);
    expect(screen.getByText("Managed")).toBeInTheDocument();
  });

  it("does not present a direct client trace as managed", () => {
    render(<RunControlModeBadge origin="direct_mcp" />);

    expect(screen.getByText("Guarded direct")).toHaveAttribute(
      "title",
      expect.stringContaining("outer coding client"),
    );
    expect(screen.queryByText("Managed")).toBeNull();
  });
});

describe("DirectRunBanner", () => {
  it("states the actual outer caller and how to return to managed mode", () => {
    render(
      <DirectRunBanner
        caller={{
          label: "claude-cli",
          provider: "anthropic-oauth",
          model: "opus",
        }}
        onStartManaged={() => undefined}
      />,
    );

    expect(screen.getByText("claude-cli")).toBeInTheDocument();
    expect(screen.getByText(/anthropic-oauth · opus/)).toBeInTheDocument();
    expect(screen.getByText("New managed task")).toBeInTheDocument();
  });

  it("uses a truthful generic identity when the caller is unknown", () => {
    render(<DirectRunBanner />);

    expect(screen.getByText("External MCP client")).toBeInTheDocument();
  });
});

describe("directCallerSummary", () => {
  it("supports protocol-client name as well as generated launcher label", () => {
    expect(
      directCallerSummary({
        name: "codex-protocol-client",
        provider: "openai-oauth",
        model: "gpt-5",
      }),
    ).toEqual({
      identity: "codex-protocol-client",
      route: "openai-oauth · gpt-5",
    });
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
