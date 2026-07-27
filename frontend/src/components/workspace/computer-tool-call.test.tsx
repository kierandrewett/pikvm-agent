// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import type { ThreadGroupPart } from "@/components/assistant-ui/thread";
import {
  ComputerActionReceipt,
  ComputerInputSequence,
  ComputerToolGroup,
} from "./computer-tool-call";

const group = (status: ThreadGroupPart["status"], count = 2): ThreadGroupPart =>
  ({
    type: "group-tool",
    indices: Array.from({ length: count }, (_, index) => index),
    status,
  }) as ThreadGroupPart;

afterEach(cleanup);

describe("ComputerToolGroup", () => {
  it("does not wrap a single action in a redundant group control", () => {
    render(
      <ComputerToolGroup
        group={group({ type: "requires-action", reason: "interrupt" }, 1)}
      >
        <span>Exact approval</span>
      </ComputerToolGroup>,
    );

    expect(screen.queryByText("Exact approval")).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: /1 computer action/i }),
    ).toBeNull();
  });

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

describe("ComputerInputSequence", () => {
  it("keeps long text and consequential keys independently inspectable", () => {
    render(
      <ComputerInputSequence
        actions={[
          {
            type: "type_text",
            text: "Quarterly figures are attached for your review.",
          },
          { type: "key", keys: ["CTRL", "ENTER"] },
        ]}
      />,
    );

    expect(screen.getByLabelText("Exact text input").textContent).toBe(
      "Quarterly figures are attached for your review.",
    );
    expect(screen.getByText("Typed payload")).not.toBeNull();
    expect(screen.getByText("47 chars · 1 line")).not.toBeNull();
    expect(
      screen.getByLabelText("Exact key input: CTRL plus ENTER"),
    ).not.toBeNull();
    expect(screen.getByText("CTRL").tagName).toBe("KBD");
    expect(screen.getByText("ENTER").tagName).toBe("KBD");
  });

  it("shows exact pointer coordinates and button", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "click", x: 1012, y: 642, button: "left" }]}
      />,
    );

    expect(
      screen.getByLabelText(
        "Exact pointer input: left button · x 1012 · y 642",
      ),
    ).not.toBeNull();
    expect(screen.getByText("x 1012").tagName).toBe("CODE");
  });

  it("treats keypress actions as keyboard input", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "keypress", keys: ["CTRL", "P"] }]}
      />,
    );

    expect(screen.getByText("Keyboard input")).not.toBeNull();
    expect(
      screen.getByLabelText("Exact key input: CTRL plus P"),
    ).not.toBeNull();
  });
});

describe("ComputerActionReceipt", () => {
  it("shows a source-to-verification receipt for a completed input", () => {
    render(
      <ComputerActionReceipt
        args={{
          based_on_frame_id: 41,
          based_on_world_version: 9,
          based_on_control_epoch: 3,
        }}
        result={{
          status: "completed",
          frame_id: 42,
          world_version: 10,
          verification: {
            verdict: "verified",
            summary: "The Save dialog closed and the document remained open.",
          },
        }}
        status={{ type: "complete" }}
        environment={{ machineName: "Office lab", currentFrameId: 42 }}
        actionCount={2}
        characterCount={84}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Office lab");
    expect(receipt.textContent).toContain("current frame 42");
    expect(receipt.textContent).toContain("Source screen");
    expect(receipt.textContent).toContain("Frame 41");
    expect(receipt.textContent).toContain("world 9 · control 3");
    expect(receipt.textContent).toContain("2 inputs");
    expect(receipt.textContent).toContain("84 exact characters");
    expect(receipt.textContent).toContain("Committed");
    expect(receipt.textContent).toContain("Verified");
    expect(receipt.textContent).toContain(
      "The Save dialog closed and the document remained open.",
    );
  });

  it("makes a held action visibly distinct from a committed action", () => {
    render(
      <ComputerActionReceipt
        args={{ based_on_world_version: 9 }}
        result={undefined}
        status={{ type: "requires-action", reason: "interrupt" }}
        environment={{ machineName: "Office lab" }}
        actionCount={1}
        characterCount={0}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Held for approval");
    expect(receipt.textContent).toContain(
      "The consequential input has not been sent",
    );
    expect(receipt.textContent).toContain("Pending");
  });
});
