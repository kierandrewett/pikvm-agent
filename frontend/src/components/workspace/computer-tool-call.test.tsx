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
    expect(screen.getByText("Exact typed payload")).not.toBeNull();
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
        environment={{ screenWidth: 1920, screenHeight: 1080 }}
      />,
    );

    expect(
      screen.getByLabelText(
        "Exact pointer input: left button · x 1012 · y 642",
      ),
    ).not.toBeNull();
    expect(screen.getByText("x 1012").tagName).toBe("CODE");
    expect(
      screen.getByLabelText(
        "Pointer target 1012, 642 on 1920 × 1080 screen",
      ),
    ).not.toBeNull();
    expect(screen.getByText(/52.7% across · 59.4% down/)).not.toBeNull();
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

  it("describes step-based scroll input without inventing pixels", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "scroll", direction: "down", amount: 4 }]}
      />,
    );

    expect(screen.getByText("Scroll input")).not.toBeNull();
    expect(screen.getByText("Scroll down 4 steps")).not.toBeNull();
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
          __receipt: {
            attempt: 2,
            latency_ms: 742,
            idempotency_key: "run:action:4:abc123",
          },
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
        environment={{
          machineName: "Office lab",
          currentFrameId: 42,
          screenWidth: 1920,
          screenHeight: 1080,
        }}
        actionCount={2}
        characterCount={84}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Office lab");
    expect(receipt.textContent).toContain("1920×1080");
    expect(receipt.textContent).toContain("Read from");
    expect(receipt.textContent).toContain("Frame 41");
    expect(receipt.textContent).toContain("world 9 · control 3");
    expect(receipt.textContent).toContain("Input boundary");
    expect(receipt.textContent).toContain("Committed");
    expect(receipt.textContent).toContain("84 exact characters");
    expect(receipt.textContent).toContain("Observed after");
    expect(receipt.textContent).toContain("Frame 42 · verified");
    expect(receipt.textContent).toContain(
      "The Save dialog closed and the document remained open.",
    );
    expect(receipt.textContent).toContain("attempt 2");
    expect(receipt.textContent).toContain("742 ms transport");
    expect(receipt.textContent).toContain("run:action:4:abc123");
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
    expect(receipt.textContent).toContain("Awaiting screen");
  });

  it("shows a live bounded input without claiming it reached the computer", () => {
    render(
      <ComputerActionReceipt
        args={{
          based_on_world_version: 14,
          __receipt: {
            attempt: 1,
            idempotency_key: "run:action:7:def456",
          },
        }}
        result={undefined}
        status={{ type: "running" }}
        environment={{ machineName: "Office lab", currentFrameId: 88 }}
        actionCount={1}
        characterCount={32}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Sending input");
    expect(receipt.textContent).toContain("In progress");
    expect(receipt.textContent).toContain(
      "The harness is sending this bounded input",
    );
    expect(receipt.textContent).not.toContain("Committed");
    expect(receipt.textContent).not.toContain("Verified");
  });

  it("keeps raw transport errors out of the receipt", () => {
    render(
      <ComputerActionReceipt
        args={{ based_on_world_version: 14 }}
        result={{
          status: "failed",
          error:
            "connection failed at vm.internal.invalid with credential=secret",
        }}
        status={{ type: "complete" }}
        environment={{ machineName: "Office lab" }}
        actionCount={1}
        characterCount={0}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Failed");
    expect(receipt.textContent).toContain("details are in diagnostics");
    expect(receipt.textContent).not.toContain("vm.internal.invalid");
    expect(receipt.textContent).not.toContain("credential=secret");
  });

  it("distinguishes captured output from independently verified output", () => {
    render(
      <ComputerActionReceipt
        args={{ based_on_world_version: 14 }}
        result={{
          status: "unverified",
          frame_id: 89,
          world_version: 15,
        }}
        status={{ type: "complete" }}
        environment={{ machineName: "Office lab", currentFrameId: 89 }}
        actionCount={1}
        characterCount={0}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Not verified");
    expect(receipt.textContent).not.toContain("Frame 89 · verified");
  });
});
