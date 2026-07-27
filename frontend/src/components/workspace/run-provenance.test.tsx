// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RunProvenance } from "./run-provenance";

afterEach(cleanup);

describe("RunProvenance", () => {
  it("keeps the outer client and its declared route visible", () => {
    render(
      <RunProvenance
        caller={{
          interface: "direct_mcp",
          label: "claude-cli",
          provider: "anthropic-oauth",
          model: "opus-4.8",
        }}
      />,
    );

    const provenance = screen.getByText(
      "via claude-cli · anthropic-oauth · opus-4.8",
    );
    expect(provenance.closest("[data-slot='badge']")?.getAttribute("title")).toBe(
      "Task submitted through direct_mcp by claude-cli using anthropic-oauth · opus-4.8",
    );
  });

  it("uses the protocol client name when no launcher label exists", () => {
    render(
      <RunProvenance
        caller={{ name: "codex-protocol-client", model: "gpt-5.5" }}
      />,
    );

    expect(
      screen.getByText("via codex-protocol-client · gpt-5.5"),
    ).not.toBeNull();
  });

  it("stays absent when the run has no caller identity", () => {
    const { container } = render(<RunProvenance caller={{}} />);

    expect(container.childElementCount).toBe(0);
  });
});
