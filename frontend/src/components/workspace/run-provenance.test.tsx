// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RunProvenance } from "./run-provenance";

afterEach(cleanup);

describe("RunProvenance", () => {
  it("keeps the outer client visible without expanding diagnostics", () => {
    render(
      <RunProvenance
        caller={{ interface: "managed_mcp", label: "claude-cli" }}
      />,
    );

    const provenance = screen.getByText("via claude-cli");
    expect(provenance.getAttribute("title")).toBe(
      "Task submitted through managed_mcp by claude-cli",
    );
  });

  it("stays absent when the run has no caller identity", () => {
    const { container } = render(<RunProvenance caller={{}} />);

    expect(container.childElementCount).toBe(0);
  });
});
