// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

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
    expect(screen.getByText("Computer activity")).not.toBeNull();
    expect(screen.getByText("2 actions")).not.toBeNull();
    expect(screen.getByText("Input live")).not.toBeNull();
    expect(
      screen.getByText("Current input stays open while the screen changes"),
    ).not.toBeNull();
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

  it("keeps typed text and its exact read-back together", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "hello world" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "verified_exact",
            verdict: "match",
            observed_text: "hello world",
            observed_text_redacted: false,
            typed_characters: 11,
            intended_characters: 11,
            correction_count: 1,
            delivery_retries: 0,
            used_fast_path: false,
            summary: "Typed and verified.",
            edit_distance: 0,
            focus_evidence: "read_back_verified",
            intended_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            acknowledged_prefix_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            observed_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            exact_sha256_match: true,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Exact read-back");
    expect(readBack.textContent).toContain("hello world");
    expect(readBack.textContent).toContain("11 / 11 chars");
    expect(readBack.textContent).toContain("0 edits");
    expect(readBack.textContent).toContain("1 correction");
    expect(readBack.textContent).toContain("Exact SHA-256");
    expect(readBack.textContent).toContain("b94d27b9934d");
  });

  it("shows focus loss without treating transport as success", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "hello world" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "failed_focus_lost",
            verdict: "mismatch",
            observed_text: "",
            observed_text_redacted: false,
            typed_characters: 5,
            intended_characters: 11,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            edit_distance: 11,
            focus_evidence: "focus_lost",
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Focus lost");
    expect(readBack.textContent).toContain("5 / 11 chars");
    expect(readBack.textContent).not.toContain("Read-back matches");
  });

  it("does not present a normalized OCR match as exact", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "one space" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "verified_safe_normalized",
            verdict: "match",
            observed_text: "one  space",
            observed_text_redacted: false,
            typed_characters: 9,
            intended_characters: 9,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            edit_distance: 0,
            focus_evidence: "read_back_verified",
            intended_sha256: "a".repeat(64),
            acknowledged_prefix_sha256: "a".repeat(64),
            observed_sha256: "b".repeat(64),
            exact_sha256_match: false,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Normalized only");
    expect(readBack.textContent).toContain("aaaaaaaaaaaa ≠ bbbbbbbbbbbb");
    expect(readBack.textContent).not.toContain("Exact read-back");
  });

  it("never renders retained read-back for a secret input", () => {
    render(
      <ComputerInputSequence
        actions={[
          {
            type: "type_text",
            text: "••••••••",
            secret: true,
            redacted: true,
          },
        ]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "delivered_unverified",
            verdict: "unverified",
            observed_text: "must not render",
            observed_text_redacted: true,
            typed_characters: 14,
            intended_characters: 14,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            focus_evidence: "read_back_not_retained",
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain(
      "No read-back text retained for secret input",
    );
    expect(readBack.textContent).not.toContain("must not render");
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
      screen.getByLabelText("Pointer target 1012, 642 on 1920 × 1080 screen"),
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
    expect(receipt.textContent).toContain("Trace");
    expect(receipt.textContent).toContain(
      "Office lab · 1920×1080 · frame 41 → 42",
    );
    expect(receipt.textContent).not.toContain("world 9 → 10");
    expect(receipt.textContent).not.toContain("control 3");
    expect(receipt.textContent).toContain("Input");
    expect(receipt.textContent).toContain(
      "Committed · 2 inputs · 84 chars · attempt 2 · 742 ms",
    );
    expect(receipt.textContent).toContain("Result");
    expect(receipt.textContent).toContain(
      "The Save dialog closed and the document remained open.",
    );
    expect(receipt.textContent).not.toContain("run:action:4:abc123");
  });

  it("keeps a verified managed action to four essential audit rows", () => {
    render(
      <ComputerActionReceipt
        args={{
          actions: [{ type: "click", x: 640, y: 360, button: "left" }],
          based_on_frame_id: 1,
          based_on_world_version: 1,
          based_on_control_epoch: 1,
          __receipt: {
            intent: "Activate the managed smoke canvas.",
            expected_evidence: ["The canvas visibly reports completion."],
            attempt: 1,
            latency_ms: 1,
            idempotency_key: "hidden-action-key",
            controller: {
              provider: "managed-smoke",
              model: "deterministic-smoke-v1",
            },
            verifier: {
              provider: "managed-smoke",
              model: "deterministic-smoke-v1",
            },
          },
        }}
        result={{
          status: "completed",
          frame_id: 2,
          world_version: 2,
          verification: {
            verdict: "verified",
            summary: "The managed smoke task is visibly complete.",
          },
        }}
        status={{ type: "complete" }}
        environment={{ machineName: "Managed smoke canvas" }}
        actionCount={1}
        characterCount={0}
        showVisualEvidence={false}
      />,
    );

    const audit = screen.getByLabelText("Action audit summary");
    expect(audit.children).toHaveLength(4);
    expect(audit.textContent).toContain("GoalActivate the managed smoke canvas.");
    expect(audit.textContent).toContain(
      "InputCommitted · Click at 640 × 360 · 1 ms",
    );
    expect(audit.textContent).toContain(
      "TraceManaged smoke canvas · frame 1 → 2",
    );
    expect(audit.textContent).toContain(
      "Modelsdeterministic-smoke-v1 · selected + checked",
    );
    expect(audit.textContent).not.toContain("The canvas visibly reports");
    expect(audit.textContent).not.toContain("The managed smoke task is");
    expect(audit.textContent).not.toContain("hidden-action-key");
    expect(audit.textContent).not.toContain("world 1 → 2");
    expect(audit.textContent).not.toContain("control 1");
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
    expect(receipt.textContent).toContain("Held for approval · 1 input");
    expect(receipt.textContent).toContain("world 9");
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
    expect(receipt.textContent).toContain("In progress");
    expect(receipt.textContent).toContain("In progress · 1 input · 32 chars");
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
    expect(receipt.textContent).toContain(
      "Transport failed · details are retained in diagnostics.",
    );
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

  it("shows the model handoff and authenticated before-after evidence", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["image"], { type: "image/png" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:action-evidence"),
      revokeObjectURL: vi.fn(),
    });

    render(
      <ComputerActionReceipt
        args={{
          based_on_frame_id: 41,
          __receipt: {
            evidence_revision: 7,
            controller: {
              provider: "gemini-account",
              model: "gemini-3-flash",
              latency_ms: 320,
            },
            verifier: {
              provider: "claude-account",
              model: "claude-opus-4-8",
              latency_ms: 940,
            },
          },
        }}
        result={{
          status: "completed",
          frame_id: 42,
          verification: {
            verdict: "verified",
            summary: "The intended control changed.",
          },
        }}
        status={{ type: "complete" }}
        environment={{
          token: "local-workspace-token",
          runId: "run-1",
          machineName: "Office lab",
        }}
        actionCount={1}
        characterCount={0}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Models");
    expect(receipt.textContent).toContain(
      "gemini-3-flash → claude-opus-4-8",
    );
    expect(receipt.textContent).not.toContain("local-workspace-token");

    await waitFor(() =>
      expect(
        screen.getByAltText(
          "Before and after screen evidence, frame 41 → frame 42",
        ),
      ).not.toBeNull(),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs/run-1/verification-images/7",
      expect.objectContaining({
        headers: { Authorization: "Bearer local-workspace-token" },
        cache: "no-store",
      }),
    );
  });

  it("can keep the screen image outside the forensic receipt", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ComputerActionReceipt
        args={{
          based_on_frame_id: 41,
          __receipt: {
            evidence_revision: 7,
          },
        }}
        result={{
          status: "completed",
          frame_id: 42,
        }}
        status={{ type: "complete" }}
        environment={{
          token: "local-workspace-token",
          runId: "run-1",
        }}
        actionCount={1}
        characterCount={0}
        showVisualEvidence={false}
      />,
    );

    expect(screen.queryByLabelText("Before and after screen evidence")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
