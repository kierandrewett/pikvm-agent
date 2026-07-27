// @vitest-environment jsdom

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useHarnessWorkspace } from "@/hooks/use-harness-workspace";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("useHarnessWorkspace authentication boundary", () => {
  it("keeps managed authentication when one persisted run cannot load", async () => {
    sessionStorage.setItem("pikvm-harness-token", "desktop-managed");
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string | URL | Request) => {
        const url = String(path);
        if (url === "/api/runs") {
          return Promise.resolve(
            jsonResponse([
              {
                run_id: "legacy-run",
                task: "Load an older run",
                status: "paused",
                origin: "managed",
                caller: {},
                created_at: "2026-07-27T12:00:00Z",
                updated_at: "2026-07-27T12:00:00Z",
                event_count: 1,
                event_cursor: 1,
              },
            ]),
          );
        }
        if (
          url === "/api/providers" ||
          url === "/api/provider-catalog" ||
          url === "/api/tools" ||
          url === "/api/tool-servers"
        ) {
          return Promise.resolve(
            jsonResponse(
              url === "/api/providers" || url === "/api/tool-servers"
                ? {}
                : [],
            ),
          );
        }
        if (url === "/api/health") {
          return Promise.resolve(
            jsonResponse({
              status: "ok",
              computer_control: "disabled",
            }),
          );
        }
        if (url === "/api/runs/legacy-run") {
          return Promise.resolve(
            jsonResponse(
              { detail: "Persisted run could not be decoded." },
              500,
            ),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { result } = renderHook(() => useHarnessWorkspace());

    await waitFor(() =>
      expect(result.current.error).toBe(
        "Persisted run could not be decoded.",
      ),
    );
    expect(result.current.connected).toBe(true);
    expect(result.current.computerControlEnabled).toBe(false);
    expect(sessionStorage.getItem("pikvm-harness-token")).toBe(
      "desktop-managed",
    );
  });
});
