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
  it("presents the current stage and active model without a noisy timer", () => {
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
    expect(status).toHaveTextContent("Choosing the next action");
    expect(status).toHaveTextContent("opus");
    expect(status).not.toHaveTextContent("claude-account");
    expect(status).not.toHaveTextContent(/\d+s/);
    expect(status).toHaveAttribute(
      "title",
      "Choosing the next action — opus via claude-account",
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
      route: "claude-account",
    });
  });

  it("shows schema validation and failover as distinct live phases", () => {
    const { rerender } = render(
      <RunActivity
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          phase: "validating",
          role: "controller",
          provider: "fast-provider",
          model: "flash",
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking the model response",
    );
    rerender(
      <RunActivity
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          phase: "failover",
          role: "controller",
          provider: "strong-provider",
        }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Switching models");
    expect(screen.getByRole("status")).toHaveTextContent("strong-provider");
  });

  it("says what the model is doing, not that a request is in flight", () => {
    render(
      <RunActivity
        working
        activity={{
          kind: "model",
          started_at: "2026-07-27T12:00:00Z",
          phase: "request_sent",
          role: "reasoner",
          provider: "claude-account",
          model: "opus",
        }}
      />,
    );

    const status = screen.getByRole("status");
    // "Waiting for a response" is true of every request and says nothing about
    // the work; the role does.
    expect(status).toHaveTextContent("Planning the task");
    expect(status).not.toHaveTextContent("Waiting for a response");
    expect(status).toHaveTextContent("opus");
    // The elapsed counter is held back so quick turns never flash a timer.
    expect(status).not.toHaveTextContent(/\d+s/);
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
