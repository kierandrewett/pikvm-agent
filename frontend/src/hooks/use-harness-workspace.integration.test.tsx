// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
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
  it("starts a fresh run when New task and send happen in the same render", async () => {
    sessionStorage.setItem("pikvm-harness-token", "desktop-managed");
    const existingRun = {
      run_id: "existing-run",
      task: "what is on the screen",
      status: "planning",
      mode: "assistant",
      origin: "managed",
      caller: { interface: "managed_mcp", label: "chat-workspace" },
      created_at: "2026-07-28T08:00:00Z",
      updated_at: "2026-07-28T08:00:01Z",
      event_count: 0,
      event_cursor: 0,
      operator_guidance: [],
      conversation: [
        {
          message_id: "existing-user",
          role: "user",
          content: "what is on the screen",
          created_at: "2026-07-28T08:00:00Z",
          event_cursor: 0,
        },
        {
          message_id: "existing-assistant",
          role: "assistant",
          content: "The screen shows Calculator.",
          created_at: "2026-07-28T08:00:01Z",
          event_cursor: 0,
        },
      ],
      events: [],
      events_truncated: false,
    };
    const freshRun = {
      ...existingRun,
      run_id: "fresh-run",
      task: "Find the latest stable Python release",
      status: "planning",
      conversation: [],
    };
    const requests: Array<{ url: string; method: string }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string | URL | Request, init?: RequestInit) => {
        const url = String(path);
        const method = init?.method ?? "GET";
        requests.push({ url, method });
        if (url === "/api/runs" && method === "GET") {
          return Promise.resolve(jsonResponse([existingRun]));
        }
        if (url === "/api/runs" && method === "POST") {
          return Promise.resolve(jsonResponse(freshRun));
        }
        if (url === "/api/runs/existing-run") {
          return Promise.resolve(jsonResponse(existingRun));
        }
        if (url === "/api/runs/fresh-run") {
          return Promise.resolve(jsonResponse(freshRun));
        }
        if (url.includes("/stream?")) return new Promise<Response>(() => {});
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
        if (url === "/api/computer-connection") {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              mcp_server_name: "Managed PiKVM MCP",
              machine_name: "Disposable Windows VM",
            }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const { result } = renderHook(() => useHarnessWorkspace());
    await waitFor(() =>
      expect(result.current.selectedRun?.run_id).toBe("existing-run"),
    );

    const sendFromExistingRender = result.current.onNew;
    act(() => result.current.newThread());
    await act(async () => {
      await sendFromExistingRender({
        role: "user",
        metadata: { custom: {} },
        createdAt: new Date("2026-07-28T09:00:00Z"),
        parentId: null,
        sourceId: null,
        runConfig: undefined,
        content: [
          {
            type: "text",
            text: "Find the latest stable Python release",
          },
        ],
      });
    });

    expect(
      requests.some(
        ({ url, method }) =>
          url === "/api/runs/existing-run/steer" && method === "POST",
      ),
    ).toBe(false);
    expect(
      requests.some(
        ({ url, method }) => url === "/api/runs" && method === "POST",
      ),
    ).toBe(true);
    expect(result.current.selectedId).toBe("fresh-run");
  });

  it("automatically follows the first externally delegated managed task", async () => {
    sessionStorage.setItem("pikvm-harness-token", "desktop-managed");
    const externalRun = {
      run_id: "external-managed-run",
      task: "Open Calculator and report the result",
      status: "running",
      mode: "computer",
      origin: "managed",
      caller: { interface: "managed_mcp", label: "claude-cli" },
      created_at: "2026-07-28T09:00:00Z",
      updated_at: "2026-07-28T09:00:01Z",
      event_count: 1,
      event_cursor: 1,
      operator_guidance: [],
      conversation: [],
      events: [
        {
          sequence: 1,
          at: "2026-07-28T09:00:01Z",
          kind: "model.started",
          data: { role: "reasoner" },
        },
      ],
      events_truncated: false,
    };
    let delegated = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string | URL | Request) => {
        const url = String(path);
        if (url === "/api/runs") {
          return Promise.resolve(
            jsonResponse(delegated ? [externalRun] : []),
          );
        }
        if (url === "/api/runs/external-managed-run") {
          return Promise.resolve(jsonResponse(externalRun));
        }
        if (url.includes("/stream?")) return new Promise<Response>(() => {});
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
        if (url === "/api/computer-connection") {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              mcp_server_name: "Managed PiKVM MCP",
              machine_name: "Disposable Windows VM",
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { result } = renderHook(() => useHarnessWorkspace());

    await waitFor(() => expect(result.current.connected).toBe(true));
    expect(result.current.selectedId).toBeNull();

    delegated = true;
    await act(async () => {
      await result.current.refresh();
    });

    await waitFor(() =>
      expect(result.current.selectedId).toBe("external-managed-run"),
    );
    expect(result.current.selectedRun?.run_id).toBe(
      "external-managed-run",
    );
  });

  it("does not let a stale task detail replace the newer selection", async () => {
    sessionStorage.setItem("pikvm-harness-token", "desktop-managed");
    const snapshot = (runId: string, task: string) => ({
      run_id: runId,
      task,
      status: "completed",
      mode: "assistant",
      origin: "managed",
      caller: { interface: "chat_workspace", label: "chat-workspace" },
      created_at: "2026-07-27T12:00:00Z",
      updated_at: "2026-07-27T12:00:01Z",
      event_count: 0,
      event_cursor: 0,
      operator_guidance: [],
      conversation: [],
      events: [],
      events_truncated: false,
    });
    const firstRun = snapshot("first-run", "First task");
    const secondRun = snapshot("second-run", "Second task");
    let resolveFirst!: (response: Response) => void;
    let resolveSecond!: (response: Response) => void;
    const firstDetail = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const secondDetail = new Promise<Response>((resolve) => {
      resolveSecond = resolve;
    });

    vi.stubGlobal(
      "fetch",
      vi.fn((path: string | URL | Request) => {
        const url = String(path);
        if (url === "/api/runs") {
          return Promise.resolve(jsonResponse([firstRun, secondRun]));
        }
        if (url === "/api/runs/first-run") return firstDetail;
        if (url === "/api/runs/second-run") return secondDetail;
        if (url.includes("/stream?")) return new Promise<Response>(() => {});
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
        if (url === "/api/computer-connection") {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              mcp_server_name: "PiKVM lab",
              machine_name: "Windows acceptance VM",
            }),
          );
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const { result } = renderHook(() => useHarnessWorkspace());

    await waitFor(() => expect(result.current.selectedId).toBe("first-run"));
    let selecting!: Promise<void>;
    act(() => {
      selecting = result.current.selectRun("second-run");
    });
    await act(async () => {
      resolveSecond(jsonResponse(secondRun));
      await selecting;
    });
    expect(result.current.selectedRun?.run_id).toBe("second-run");

    await act(async () => {
      resolveFirst(jsonResponse(firstRun));
      await Promise.resolve();
    });

    expect(result.current.selectedId).toBe("second-run");
    expect(result.current.selectedRun?.run_id).toBe("second-run");
  });

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
        if (url === "/api/computer-connection") {
          return Promise.resolve(
            jsonResponse({
              enabled: false,
              mcp_server_name: "Managed PiKVM MCP",
              machine_name: "No computer",
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
    expect(result.current.computerConnection).toEqual({
      enabled: false,
      mcpServerName: "Managed PiKVM MCP",
      machineName: "No computer",
    });
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
        if (url === "/api/computer-connection") {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              mcp_server_name: "PiKVM lab",
              machine_name: "Windows acceptance VM",
            }),
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
    expect(result.current.computerConnection).toEqual({
      enabled: true,
      mcpServerName: "PiKVM lab",
      machineName: "Windows acceptance VM",
    });
    expect(detailReads).toBe(1);
  });
});
