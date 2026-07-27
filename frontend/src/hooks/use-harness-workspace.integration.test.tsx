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

  it("reduces live provider events without refetching the run per event", async () => {
    sessionStorage.setItem("pikvm-harness-token", "desktop-managed");
    const initialRun = {
      run_id: "live-run",
      task: "Type exact text",
      status: "running",
      mode: "computer",
      origin: "managed",
      caller: { interface: "chat_workspace", label: "chat-workspace" },
      created_at: "2026-07-27T12:00:00Z",
      updated_at: "2026-07-27T12:00:01Z",
      event_count: 1,
      event_cursor: 1,
      operator_guidance: [],
      events: [
        {
          sequence: 1,
          at: "2026-07-27T12:00:01Z",
          kind: "model.started",
          data: { role: "controller" },
        },
      ],
      events_truncated: false,
    };
    let detailReads = 0;
    const fetch = vi.fn(
      (path: string | URL | Request, init?: RequestInit) => {
        const url = String(path);
        if (url === "/api/runs") {
          return Promise.resolve(jsonResponse([initialRun]));
        }
        if (url === "/api/runs/live-run") {
          detailReads += 1;
          return Promise.resolve(jsonResponse(initialRun));
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
            jsonResponse({ status: "ok", computer_control: "enabled" }),
          );
        }
        if (url === "/api/runs/live-run/stream?after=1") {
          const encoder = new TextEncoder();
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                encoder.encode(
                  [
                    "event: run.event",
                    "id: 2",
                    `data: ${JSON.stringify({
                      sequence: 2,
                      at: "2026-07-27T12:00:02Z",
                      kind: "model.provider_request_sent",
                      data: {
                        role: "controller",
                        provider: "fast-controller",
                        model: "flash",
                        attempt: 1,
                      },
                    })}`,
                    "",
                    "event: run.event",
                    "id: 3",
                    `data: ${JSON.stringify({
                      sequence: 3,
                      at: "2026-07-27T12:00:03Z",
                      kind: "model.provider_validating",
                      data: {
                        role: "controller",
                        provider: "fast-controller",
                        model: "flash",
                        attempt: 1,
                      },
                    })}`,
                    "",
                    "",
                  ].join("\n"),
                ),
              );
              init?.signal?.addEventListener(
                "abort",
                () => controller.close(),
                { once: true },
              );
            },
          });
          return Promise.resolve(
            new Response(stream, {
              status: 200,
              headers: { "Content-Type": "text/event-stream" },
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetch);

    const { result } = renderHook(() => useHarnessWorkspace());

    await waitFor(() =>
      expect(result.current.selectedRun?.event_cursor).toBe(3),
    );
    expect(result.current.selectedRun?.active_activity).toMatchObject({
      kind: "model",
      phase: "validating",
      provider: "fast-controller",
      model: "flash",
    });
    expect(detailReads).toBe(1);
  });
});
