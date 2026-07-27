// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComputerConnectionButton } from "@/components/workspace/computer-connection-button";

afterEach(cleanup);

describe("ComputerConnectionButton", () => {
  it("names the managed MCP target and its truthful configured state", async () => {
    const onOpen = vi.fn();
    render(
      <ComputerConnectionButton
        enabled
        mcpServerName="Managed PiKVM MCP"
        machineName="Windows acceptance VM"
        onOpen={onOpen}
      />,
    );

    const button = screen.getByRole("button", {
      name: /open managed computer.*windows acceptance vm.*configured/i,
    });
    expect(button).toHaveTextContent("Windows acceptance VM");
    expect(button).toHaveTextContent("configured");
    expect(button).toHaveAttribute(
      "title",
      expect.stringContaining("Target reachability is checked"),
    );
    expect(screen.getByText("configured")).not.toHaveClass("hidden");

    await userEvent.click(button);
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("does not present a placeholder alias as a real machine identity", () => {
    render(
      <ComputerConnectionButton
        enabled
        mcpServerName="Managed PiKVM MCP"
        machineName="Unlabelled target"
        onOpen={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: /open managed computer/i }),
    ).toHaveTextContent("Managed computer");
    expect(screen.queryByText("Unlabelled target")).toBeNull();
  });

  it("makes chat-only mode explicit when no computer is configured", () => {
    render(
      <ComputerConnectionButton
        enabled={false}
        mcpServerName="Managed PiKVM MCP"
        onOpen={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", {
      name: /open computer connection details.*no managed computer/i,
    });
    expect(button).toHaveTextContent("Chat only");
    expect(button).toHaveTextContent("no computer");
  });
});
