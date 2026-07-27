import { afterEach, describe, expect, it, vi } from "vitest";
import { loadProviderCatalog } from "@/hooks/use-harness-workspace";

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
