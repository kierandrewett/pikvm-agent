// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  activityPresentation,
  RunActivity,
} from "@/components/workspace/run-activity";

afterEach(cleanup);

describe("RunActivity", () => {
  it("presents the current stage without leaking a timer or provider label", () => {
    render(
      <RunActivity
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          role: "controller",
          provider: "claude-account",
          model: "opus",
        }}
      />,
    );

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Using the computer");
    expect(status).not.toHaveTextContent("opus");
    expect(status).not.toHaveTextContent("claude-account");
    expect(status).not.toHaveTextContent(/\d+s/);
    expect(status).toHaveAttribute(
      "title",
      "Using the computer — opus via claude-account",
    );
  });

  it("uses a quiet fallback when a provider did not report its model", () => {
    expect(
      activityPresentation({
        kind: "model",
        started_at: "2026-07-27T12:00:00Z",
        role: "verifier",
        provider: "claude-account",
      }),
    ).toMatchObject({
      label: "Checking the result",
      model: "",
    });
  });

  it("does not duplicate a running tool call", () => {
    const { container } = render(
      <RunActivity
        working
        activity={{
          kind: "tool",
          started_at: "2026-07-27T12:00:00Z",
          tool: "pikvm_screenshot",
        }}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
