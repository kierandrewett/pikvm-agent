import { afterEach, describe, expect, it, vi } from "vitest";
import { harnessEventStream } from "@/lib/harness-api";

const encoder = new TextEncoder();

const streamResponse = (...chunks: string[]) =>
  new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );

afterEach(() => {
  vi.restoreAllMocks();
});

describe("harnessEventStream", () => {
  it("parses authenticated SSE messages split across network chunks", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      streamResponse(
        "event: stream.ready\n",
        'data: {"cursor":12,"status":"running"}\n\nid: 13\n',
        "event: run.event\n",
        'data: {"sequence":13,"kind":"action.attempted"}\n\n',
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const messages: Array<Record<string, unknown>> = [];

    await harnessEventStream(
      "workspace-secret",
      "/api/runs/run-1/stream?after=12",
      {
        onMessage: (message) => messages.push(message),
      },
    );

    expect(messages).toEqual([
      {
        event: "stream.ready",
        data: { cursor: 12, status: "running" },
      },
      {
        event: "run.event",
        id: "13",
        data: { sequence: 13, kind: "action.attempted" },
      },
    ]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/runs/run-1/stream?after=12",
      expect.objectContaining({
        cache: "no-store",
        headers: expect.any(Headers),
      }),
    );
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer workspace-secret");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(fetchMock.mock.calls[0]?.[0]).not.toContain("workspace-secret");
  });

  it("supports CRLF and multi-line SSE data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        streamResponse(
          "id: 21\r\nevent: run.state\r\n",
          "data: {\r\ndata: \"status\": \"verifying\"\r\ndata: }\r\n\r\n",
        ),
      ),
    );
    const messages: Array<Record<string, unknown>> = [];

    await harnessEventStream("token", "/stream", {
      onMessage: (message) => messages.push(message),
    });

    expect(messages).toEqual([
      {
        event: "run.state",
        id: "21",
        data: { status: "verifying" },
      },
    ]);
  });

  it("fails safely when the authenticated stream is rejected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "expired workspace token" }), {
          status: 401,
          statusText: "Unauthorized",
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(
      harnessEventStream("expired", "/stream", { onMessage: () => undefined }),
    ).rejects.toMatchObject({
      name: "HarnessApiError",
      status: 401,
      message: "expired workspace token",
    });
  });
});
