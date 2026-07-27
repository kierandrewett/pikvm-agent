import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createRunPayload,
  loadHarnessHealth,
  loadProviderCatalog,
  reconcileIntervalMs,
} from "@/hooks/use-harness-workspace";

afterEach(() => vi.unstubAllGlobals());

describe("loadProviderCatalog", () => {
  it("loads the authenticated secret-free catalog", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            kind: "codex_cli",
            support_tier: "stable",
            implementation_contract: "first_party",
            interface: "Codex exec",
            pixel_input: "Native image attachment",
            structured_output: "Strict JSON Schema",
            auth: [
              {
                mode: "saved_cli_login",
                credential_owner: "provider_cli",
              },
            ],
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetch);

    const catalog = await loadProviderCatalog("workspace-token");

    expect(catalog).toHaveLength(1);
    expect(catalog[0]?.kind).toBe("codex_cli");
    const [, request] = fetch.mock.calls[0] as [string, RequestInit];
    expect(new Headers(request.headers).get("Authorization")).toBe(
      "Bearer workspace-token",
    );
  });

  it("keeps the workspace usable during a rolling upgrade from an older server", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Not Found" }), {
          status: 404,
          statusText: "Not Found",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(loadProviderCatalog("workspace-token")).resolves.toEqual([]);
  });
});

describe("loadHarnessHealth", () => {
  it("distinguishes a chat-only server from a connected computer", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "ok",
            computer_control: "disabled",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(loadHarnessHealth("workspace-token")).resolves.toBe(false);
  });

  it("keeps older harness servers computer-capable by default", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(loadHarnessHealth("workspace-token")).resolves.toBe(true);
  });
});

describe("createRunPayload", () => {
  it("keeps automatic routing explicit without using the legacy all-role pin", () => {
    expect(createRunPayload("Open the report", {})).toEqual({
      task: "Open the report",
      mode: "assistant",
      auto_start: true,
      model_preferences: null,
      source_client: "chat-workspace",
    });
    expect(createRunPayload("Open the report", {})).not.toHaveProperty(
      "model_provider",
    );
  });

  it("sends independent role primaries", () => {
    expect(
      createRunPayload("Build the workbook", {
        reasoner: "strong-reasoner",
        controller: "fast-controller",
        verifier: "strong-reasoner",
      }),
    ).toMatchObject({
      model_preferences: {
        reasoner: "strong-reasoner",
        controller: "fast-controller",
        verifier: "strong-reasoner",
      },
    });
  });
});

describe("reconcileIntervalMs", () => {
  it("does not hammer the local API while a new workspace is idle", () => {
    expect(reconcileIntervalMs("idle")).toBe(15_000);
    expect(reconcileIntervalMs("live")).toBe(15_000);
    expect(reconcileIntervalMs("connecting")).toBe(15_000);
  });

  it("reconciles aggressively only while live updates are degraded", () => {
    expect(reconcileIntervalMs("retrying")).toBe(1_500);
    expect(reconcileIntervalMs("offline")).toBe(1_500);
  });
});
