// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ToolFallbackApproval,
  ToolFallbackAttribution,
} from "@/components/assistant-ui/tool-fallback";

afterEach(cleanup);

const approval = {
  id: "approval-mail-1",
  options: [
    {
      id: "approve",
      kind: "allow-once" as const,
      label: "Allow once",
      description:
        "side effect: This external tool may send a message.",
      confirm: {
        title: "Allow mail.send?",
        description:
          "This external tool may send a message.",
      },
    },
    {
      id: "reject",
      kind: "reject-once" as const,
      label: "Deny",
    },
  ],
};

describe("ToolFallbackApproval", () => {
  it("requires a second exact confirmation before a consequential tool", () => {
    const respond = vi.fn();
    render(
      <ToolFallbackApproval
        approval={approval}
        respondToApproval={respond}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Allow once" }));

    expect(respond).not.toHaveBeenCalled();
    expect(screen.getByText("Allow mail.send?")).toBeVisible();
    expect(
      screen.getByText("This external tool may send a message."),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(respond).toHaveBeenCalledOnce();
    expect(respond).toHaveBeenCalledWith({ optionId: "approve" });
  });

  it("keeps denial immediate and distinct from approval", () => {
    const respond = vi.fn();
    render(
      <ToolFallbackApproval
        approval={approval}
        respondToApproval={respond}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Deny & stop" }));

    expect(respond).toHaveBeenCalledOnce();
    expect(respond).toHaveBeenCalledWith({ optionId: "reject" });
    expect(screen.queryByText("Allow mail.send?")).toBeNull();
  });
});

describe("ToolFallbackAttribution", () => {
  it("shows the model that selected a capability without turning it into telemetry", () => {
    render(
      <ToolFallbackAttribution
        selectedBy={{
          provider: "claude-account",
          model: "claude-opus-4-8",
          latencyMs: 5188,
        }}
      />,
    );

    expect(screen.getByText("Selected by")).toBeVisible();
    expect(screen.getByText("claude-opus-4-8")).toBeVisible();
    expect(screen.getByText("claude-account")).toBeVisible();
    expect(screen.getByText("· 5.1s")).toBeVisible();
  });
});
