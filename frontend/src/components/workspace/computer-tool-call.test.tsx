// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ThreadGroupPart } from "@/components/assistant-ui/thread";
import {
  ComputerActionReceipt,
  ComputerInputSequence,
  ComputerToolCall,
  ComputerToolGroup,
} from "./computer-tool-call";
import { ComputerToolEnvironmentProvider } from "./computer-tool-environment";

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
        <span>Earlier completed input</span>
        <span>Exact live input</span>
      </ComputerToolGroup>,
    );

    expect(screen.queryByText("Exact live input")).not.toBeNull();
    expect(screen.queryByText("Earlier completed input")).toBeNull();
    expect(
      screen
        .getByText("Exact live input")
        .closest('[data-slot="tool-group-content"]')
        ?.className,
    ).toContain(
      "[&>div>.computer-action-step:not(:last-child)]:hidden",
    );
    expect(
      screen
        .getByRole("button", { name: /2 computer actions/i })
        .getAttribute("aria-expanded"),
    ).toBe("true");
    expect(screen.getByText("Computer activity")).not.toBeNull();
    expect(screen.getByText("2 actions")).not.toBeNull();
    expect(screen.getByText("Input live")).not.toBeNull();
    expect(
      screen.queryByText("Current input stays open while the screen changes"),
    ).toBeNull();
  });

  it("lets a completed input group stay compact until inspected", async () => {
    const user = userEvent.setup();
    render(
      <ComputerToolGroup group={group({ type: "complete" })}>
        <span>Earlier completed input</span>
        <span>Exact completed input</span>
      </ComputerToolGroup>,
    );

    expect(screen.queryByText("Exact completed input")).toBeNull();
    expect(screen.queryByText("Earlier completed input")).toBeNull();

    await user.click(
      screen.getByRole("button", { name: /2 computer actions/i }),
    );

    expect(screen.queryByText("Exact completed input")).not.toBeNull();
    expect(screen.queryByText("Earlier completed input")).not.toBeNull();
  });

  it("focuses a review on the consequential input awaiting approval", () => {
    render(
      <ComputerToolGroup
        group={group({ type: "requires-action", reason: "interrupt" })}
      >
        <span>Earlier safe input</span>
        <span>Consequential input awaiting approval</span>
      </ComputerToolGroup>,
    );

    expect(
      screen.queryByText("Consequential input awaiting approval"),
    ).not.toBeNull();
    expect(screen.queryByText("Earlier safe input")).toBeNull();
  });
});

describe("ComputerToolCall", () => {
  const baseProps = {
    type: "tool-call" as const,
    toolCallId: "call-1",
    toolName: "pikvm_run_burst",
    args: {
      actions: [{ type: "click", x: 638, y: 410, button: "left" }],
      __receipt: {
        latency_ms: 12,
        intent: "Open the selected control.",
        controller: { provider: "controller", model: "fast-model" },
      },
    },
    argsText:
      '{"actions":[{"type":"click","x":638,"y":410,"button":"left"}]}',
    result: {
      status: "completed",
      verification: { verdict: "verified" },
    },
    status: { type: "complete" as const },
    addResult: vi.fn(),
    resume: vi.fn(),
    respondToApproval: vi.fn(),
  };

  it("opens a checkpointed action and labels it before input is sent", () => {
    render(
      <ComputerToolCall
        {...baseProps}
        args={{
          actions: [
            {
              type: "type_text",
              text: "exactly one space",
            },
          ],
          __receipt: {
            phase: "checkpointed",
            intent: "Type the requested words exactly.",
          },
        }}
        result={undefined}
        status={{ type: "running" }}
      />,
    );

    expect(screen.getByText("Ready to send")).not.toBeNull();
    expect(screen.queryByText("Sending input")).toBeNull();
    expect(
      screen.getByLabelText("Exact computer input sequence"),
    ).not.toBeNull();
    expect(screen.getByLabelText("Exact text input").textContent).toContain(
      "exactly one space",
    );
  });

  it("renders a routine completed action as one compact inspectable row", async () => {
    const user = userEvent.setup();
    render(<ComputerToolCall {...baseProps} />);

    const trigger = screen.getByRole("button", {
      name: /Click at 638 × 410.*Verified/i,
    });
    expect(trigger.textContent).toContain("pikvm_run_burst");
    expect(trigger.textContent).toContain("<1s");
    expect(screen.queryByRole("button", { name: "Details" })).toBeNull();
    expect(
      screen.queryByLabelText("Exact computer input sequence"),
    ).toBeNull();

    await user.click(trigger);

    expect(screen.getByRole("button", { name: "Details" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Raw" })).not.toBeNull();
    expect(
      screen.queryByLabelText("Exact computer input sequence"),
    ).toBeNull();
  });

  it("keeps audit and raw MCP arguments behind separate disclosures", async () => {
    const user = userEvent.setup();
    render(<ComputerToolCall {...baseProps} />);

    await user.click(
      screen.getByRole("button", {
        name: /Click at 638 × 410.*Verified/i,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Details" }));

    expect(screen.getByLabelText("Action audit summary")).not.toBeNull();
    expect(screen.queryByText(baseProps.argsText)).toBeNull();

    await user.click(screen.getByRole("button", { name: "Raw" }));

    expect(screen.getByText(baseProps.argsText)).not.toBeNull();
  });

  it("shows a marked pre-action crop beside a completed click", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["target"], { type: "image/png" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:click-target"),
      revokeObjectURL: vi.fn(),
    });

    render(
      <ComputerToolEnvironmentProvider
        value={{
          token: "local-workspace-token",
          runId: "run-1",
          screenWidth: 1280,
          screenHeight: 720,
        }}
      >
        <ComputerToolCall
          {...baseProps}
          args={{
            ...baseProps.args,
            __receipt: {
              ...baseProps.args.__receipt,
              evidence_revision: 9,
            },
          }}
        />
      </ComputerToolEnvironmentProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByLabelText(
          "Pre-action preview around click at 638, 410",
        ),
      ).not.toBeNull(),
    );
    expect(screen.getByText("Click target")).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs/run-1/verification-images/9/click-target?x=638&y=410&screen_width=1280&screen_height=720",
      expect.objectContaining({
        headers: { Authorization: "Bearer local-workspace-token" },
        cache: "no-store",
      }),
    );
  });

  it("shows the same visual target for a guarded-direct click tool", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(["target"], { type: "image/png" }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:direct-click-target"),
      revokeObjectURL: vi.fn(),
    });

    render(
      <ComputerToolEnvironmentProvider
        value={{
          token: "local-workspace-token",
          runId: "direct-run",
          screenWidth: 1280,
          screenHeight: 720,
        }}
      >
        <ComputerToolCall
          {...baseProps}
          toolName="pikvm_click"
          args={{
            session_id: "redacted-session",
            x: 412,
            y: 286,
            button: "left",
            __receipt: {
              evidence_revision: 3,
              evidence_kind: "pre_action",
              evidence_before_frame_id: 17,
            },
          }}
          result={{ status: "unverified", frame_id: 18 }}
        />
      </ComputerToolEnvironmentProvider>,
    );

    await waitFor(() =>
      expect(
        screen.getByLabelText(
          "Pre-action preview around click at 412, 286",
        ),
      ).not.toBeNull(),
    );
    expect(screen.getByText("Click target")).not.toBeNull();
    expect(screen.getByText("Screen before input")).not.toBeNull();
    expect(
      screen.getByLabelText("Pre-action screen evidence"),
    ).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs/direct-run/verification-images/3/click-target?x=412&y=286&screen_width=1280&screen_height=720",
      expect.objectContaining({
        headers: { Authorization: "Bearer local-workspace-token" },
        cache: "no-store",
      }),
    );
  });

  it("keeps exact typing visible when a consequential action needs approval", () => {
    render(
      <ComputerToolCall
        {...baseProps}
        args={{
          actions: [{ type: "type_text", text: "Send the irreversible text" }],
        }}
        argsText='{"actions":[{"type":"type_text"}]}'
        result={undefined}
        status={{ type: "requires-action", reason: "interrupt" }}
        approval={{
          id: "approval-1",
          approved: undefined,
          options: [
            {
              id: "allow-once",
              kind: "allow-once",
              label: "Allow once",
              description: "This sends an external message.",
            },
          ],
        }}
      />,
    );

    expect(screen.getByLabelText("Exact text input").textContent).toBe(
      "Send the irreversible text",
    );
    expect(
      screen.getByText("Held before a consequential input"),
    ).not.toBeNull();
  });
});

describe("ComputerInputSequence", () => {
  it("shows a bounded spreadsheet action as one readable grid receipt", () => {
    render(
      <ComputerInputSequence
        actions={[
          {
            type: "spreadsheet_grid",
            rows: [
              ["Q1", "124.8"],
              ["Q2", "132.1"],
            ],
          },
        ]}
        inputReceipts={[
          {
            index: 0,
            type: "spreadsheet_grid",
            status: "delivered_unverified",
            verdict: "unverified",
            proof_state: "issued_only",
            observed_text: "",
            observed_text_redacted: false,
            requested_cells: 4,
            issued_cells: 4,
            requested_characters: 14,
            issued_characters: 14,
            emitted_characters: 14,
            emitted_exactly_once: true,
            used_fast_path: false,
            focus_evidence: "read_back_unavailable",
          },
        ]}
      />,
    );

    expect(screen.getByText("Spreadsheet data")).not.toBeNull();
    expect(screen.getByText("Enter 2 × 2 spreadsheet grid")).not.toBeNull();
    const grid = screen.getByLabelText(
      "Spreadsheet grid input: 2 rows by 2 columns",
    );
    expect(grid.textContent).toContain("Q1");
    expect(grid.textContent).toContain("124.8");
    expect(grid.textContent).toContain("Q2");
    expect(grid.textContent).toContain("132.1");
    expect(screen.getByText("4 / 4 cells issued")).not.toBeNull();
    expect(screen.getByText("Final workbook verification pending")).not.toBeNull();
  });

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
    expect(screen.getByText("Requested payload")).not.toBeNull();
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
            proof_state: "exact_visual_readback",
            observed_text: "hello world",
            observed_text_redacted: false,
            issued_characters: 11,
            requested_characters: 11,
            observed_characters: 11,
            correction_count: 1,
            delivery_retries: 0,
            emitted_characters: 11,
            emitted_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            emitted_exactly_once: true,
            used_fast_path: false,
            summary: "Typed and verified.",
            edit_distance: 0,
            focus_evidence: "read_back_verified",
            requested_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            issued_prefix_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            readback_sha256:
              "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            readback_frame_sha256:
              "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            exact_readback_sha256_match: true,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Exact visual read-back");
    expect(readBack.textContent).toContain("hello world");
    expect(readBack.textContent).toContain("11 / 11 issued");
    expect(readBack.textContent).toContain("11 read back");
    expect(readBack.textContent).toContain("0 edits");
    expect(readBack.textContent).toContain("1 correction");
    expect(readBack.textContent).toContain("at-most-once emission");
    expect(readBack.textContent).toContain("Payload/OCR SHA-256");
    expect(readBack.textContent).toContain("b94d27b9934d");
    expect(readBack.textContent).toContain("frame cccccccccccc");
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
            proof_state: "issued_only",
            observed_text: "",
            observed_text_redacted: false,
            issued_characters: 5,
            requested_characters: 11,
            observed_characters: 0,
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
    expect(readBack.textContent).toContain("5 / 11 issued");
    expect(readBack.textContent).not.toContain("Read-back matches");
  });

  it("does not present a normalized OCR match as exact", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "One space" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "verified_safe_normalized",
            verdict: "match",
            proof_state: "normalized_readback",
            observed_text: "one space",
            observed_text_redacted: false,
            issued_characters: 9,
            requested_characters: 9,
            observed_characters: 9,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            edit_distance: 0,
            focus_evidence: "read_back_verified",
            requested_sha256: "a".repeat(64),
            issued_prefix_sha256: "a".repeat(64),
            readback_sha256: "b".repeat(64),
            exact_readback_sha256_match: false,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Normalized only");
    expect(readBack.textContent).toContain(
      "Delivery aaaaaaaaaaaa ≠ OCR bbbbbbbbbbbb",
    );
    expect(readBack.textContent).not.toContain("Exact read-back");
  });

  it("makes an observed double space a blocking mismatch", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "one space" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "unverified_whitespace",
            verdict: "unverified",
            proof_state: "ambiguous_readback",
            observed_text: "one  space",
            observed_text_redacted: false,
            issued_characters: 9,
            requested_characters: 9,
            delivery_characters: 9,
            delivery_transformed: false,
            observed_characters: 10,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            edit_distance: 1,
            focus_evidence: "read_back_unverified",
            delivery_sha256: "a".repeat(64),
            issued_prefix_sha256: "a".repeat(64),
            readback_sha256: "b".repeat(64),
            exact_readback_sha256_match: false,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Whitespace differs");
    expect(readBack.textContent).toContain("one  space");
    expect(readBack.textContent).toContain(
      "Delivery aaaaaaaaaaaa ≠ OCR bbbbbbbbbbbb",
    );
  });

  it("shows sender completion and a partial read-back as different facts", () => {
    render(
      <ComputerInputSequence
        actions={[{ type: "type_text", text: "Get-Process observer*" }]}
        inputReceipts={[
          {
            index: 0,
            type: "type_text",
            status: "unverified_truncated",
            verdict: "unverified",
            proof_state: "partial_readback",
            observed_text: "Get-Process",
            observed_text_redacted: false,
            issued_characters: 21,
            requested_characters: 21,
            observed_characters: 11,
            correction_count: 0,
            delivery_retries: 0,
            used_fast_path: false,
            focus_evidence: "read_back_unverified",
            requested_sha256: "a".repeat(64),
            issued_prefix_sha256: "a".repeat(64),
            readback_sha256: "b".repeat(64),
            exact_readback_sha256_match: false,
          },
        ]}
      />,
    );

    const readBack = screen.getByLabelText("Typing read-back for action 1");
    expect(readBack.textContent).toContain("Partial OCR read-back");
    expect(readBack.textContent).toContain("21 / 21 issued");
    expect(readBack.textContent).toContain("11 read back");
    expect(readBack.textContent).not.toContain("Exact OCR read-back");
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
            proof_state: "not_retained",
            observed_text: "must not render",
            observed_text_redacted: true,
            issued_characters: 14,
            requested_characters: 14,
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
        toolName="pikvm_run_burst"
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
          image_sha256:
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
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
    expect(receipt.textContent).toContain("MCP toolpikvm_run_burst");
    expect(receipt.textContent).toContain("Office lab");
    expect(receipt.textContent).toContain("Trace");
    expect(receipt.textContent).toContain(
      "Office lab · 1920×1080 · frame 41 → 42 · pixels 0123456789ab",
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

  it("shows the exact outer caller for a direct MCP action", () => {
    render(
      <ComputerActionReceipt
        args={{
          __receipt: {
            caller: {
              name: "claude-code",
              provider: "anthropic-oauth",
              model: "opus-4.8",
            },
          },
        }}
        result={{ status: "unverified" }}
        status={{ type: "complete" }}
        environment={{ machineName: "Office lab" }}
        actionCount={1}
        characterCount={0}
      />,
    );

    const receipt = screen.getByLabelText("Computer action receipt");
    expect(receipt.textContent).toContain("Caller");
    expect(receipt.textContent).toContain(
      "claude-code · opus-4.8 via anthropic-oauth",
    );
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
