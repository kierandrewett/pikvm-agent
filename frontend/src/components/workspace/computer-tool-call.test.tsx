// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { ThreadGroupPart } from "@/components/assistant-ui/thread";
import { ComputerToolGroup } from "./computer-tool-call";

const group = (
  status: ThreadGroupPart["status"],
): ThreadGroupPart =>
  ({
    type: "group-tool",
    indices: [0, 1],
    status,
  }) as ThreadGroupPart;

afterEach(cleanup);

describe("ComputerToolGroup", () => {
  it("keeps active computer inputs expanded during live event updates", () => {
    render(
      <ComputerToolGroup group={group({ type: "running" })}>
        <span>Exact live input</span>
      </ComputerToolGroup>,
    );

    expect(screen.queryByText("Exact live input")).not.toBeNull();
    expect(
      screen
        .getByRole("button", { name: /2 computer actions/i })
        .getAttribute("aria-expanded"),
    ).toBe("true");
  });

  it("lets a completed input group stay compact until inspected", async () => {
    const user = userEvent.setup();
    render(
      <ComputerToolGroup group={group({ type: "complete" })}>
        <span>Exact completed input</span>
      </ComputerToolGroup>,
    );

    expect(screen.queryByText("Exact completed input")).toBeNull();

    await user.click(
      screen.getByRole("button", { name: /2 computer actions/i }),
    );

    expect(screen.queryByText("Exact completed input")).not.toBeNull();
  });
});
