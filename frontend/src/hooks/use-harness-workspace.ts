import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  AppendMessage,
  RespondToToolApprovalOptions,
} from "@assistant-ui/react";
import {
  clearStoredToken,
  harnessEventStream,
  harnessJson,
  readStoredToken,
  storeToken,
} from "@/lib/harness-api";
import type {
  LiveUpdateStatus,
  ProviderMap,
  RunSnapshot,
  RunStatus,
  RunSummary,
} from "@/types";

const ACTIVE_OR_PAUSED = new Set<RunStatus>([
  "created",
  "planning",
  "running",
  "executing",
  "verifying",
  "paused",
  "needs_approval",
]);

const STREAM_REFRESH_DEBOUNCE_MS = 75;
const STREAM_RETRY_BASE_MS = 500;
const STREAM_RETRY_MAX_MS = 5_000;
const LIVE_RECONCILE_MS = 15_000;
const DEGRADED_RECONCILE_MS = 1_500;

const messageText = (message: AppendMessage) => {
  const text = message.content
    .filter(
      (part): part is Extract<(typeof message.content)[number], { type: "text" }> =>
        part.type === "text",
    )
    .map((part) => part.text)
    .join("\n")
    .trim();
  if (!text) throw new Error("Enter a task for the computer.");
  return text;
};

export function useHarnessWorkspace() {
  const [token, setToken] = useState("");
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(false);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<RunSnapshot | null>(null);
  const [providers, setProviders] = useState<ProviderMap>({});
  const [selectedProvider, setSelectedProvider] = useState("");
  const [error, setError] = useState("");
  const [liveUpdateStatus, setLiveUpdateStatus] =
    useState<LiveUpdateStatus>("idle");
  const [lastLiveEventAt, setLastLiveEventAt] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const loadRun = useCallback(async (accessToken: string, runId: string) => {
    const run = await harnessJson<RunSnapshot>(
      accessToken,
      `/api/runs/${encodeURIComponent(runId)}`,
    );
    if (mounted.current) {
      setSelectedRun(run);
      setRuns((current) => {
        const index = current.findIndex((item) => item.run_id === run.run_id);
        const summary: RunSummary = {
          run_id: run.run_id,
          task: run.task,
          status: run.status,
          origin: run.origin,
          model_provider: run.model_provider,
          caller: run.caller,
          session_id: run.session_id,
          created_at: run.created_at,
          updated_at: run.updated_at,
          event_count: run.event_count,
          event_cursor: run.event_cursor,
          error: run.error,
        };
        if (index < 0) return [summary, ...current];
        const next = [...current];
        next[index] = summary;
        return next;
      });
    }
    return run;
  }, []);

  const refresh = useCallback(
    async (accessToken = token, runId = selectedId) => {
      if (!accessToken) return;
      const [nextRuns, nextProviders, nextRun] = await Promise.all([
        harnessJson<RunSummary[]>(accessToken, "/api/runs"),
        harnessJson<ProviderMap>(accessToken, "/api/providers"),
        runId ? loadRun(accessToken, runId) : Promise.resolve(null),
      ]);
      if (!mounted.current) return;
      setRuns(nextRuns);
      setProviders(nextProviders);
      if (!runId) setSelectedRun(null);
      if (
        selectedProvider &&
        (!nextProviders[selectedProvider] ||
          nextProviders[selectedProvider]?.ready === false)
      ) {
        setSelectedProvider("");
      }
      return nextRun;
    },
    [loadRun, selectedId, selectedProvider, token],
  );

  const connect = useCallback(
    async (candidate: string) => {
      const accessToken = candidate.trim();
      if (!accessToken) {
        setError("Paste the one-time local workspace token.");
        return;
      }
      setLoading(true);
      setError("");
      try {
        const [nextRuns, nextProviders] = await Promise.all([
          harnessJson<RunSummary[]>(accessToken, "/api/runs"),
          harnessJson<ProviderMap>(accessToken, "/api/providers"),
        ]);
        if (!mounted.current) return;
        setToken(accessToken);
        storeToken(accessToken);
        setRuns(nextRuns);
        setProviders(nextProviders);
        setConnected(true);
        const firstId = nextRuns[0]?.run_id ?? null;
        setSelectedId(firstId);
        if (firstId) await loadRun(accessToken, firstId);
      } catch (cause) {
        clearStoredToken();
        if (!mounted.current) return;
        setConnected(false);
        setError(cause instanceof Error ? cause.message : "Connection failed.");
      } finally {
        if (mounted.current) setLoading(false);
      }
    },
    [loadRun],
  );

  useEffect(() => {
    const stored = readStoredToken();
    if (stored) void connect(stored);
  }, [connect]);

  useEffect(() => {
    if (!connected || !token) return;
    const interval =
      liveUpdateStatus === "live"
        ? LIVE_RECONCILE_MS
        : DEGRADED_RECONCILE_MS;
    const timer = window.setInterval(() => {
      void refresh().catch((cause) => {
        if (cause instanceof Error && mounted.current) setError(cause.message);
      });
    }, interval);
    return () => window.clearInterval(timer);
  }, [connected, liveUpdateStatus, refresh, token]);

  const streamRunId =
    selectedRun?.run_id === selectedId ? selectedRun.run_id : null;

  useEffect(() => {
    if (!connected || !token || !selectedId || streamRunId !== selectedId) {
      if (mounted.current) {
        setLiveUpdateStatus(connected ? "idle" : "offline");
        setLastLiveEventAt(null);
      }
      return;
    }

    let active = true;
    let cursor = selectedRun?.event_cursor ?? 0;
    let failures = 0;
    let refreshTimer: number | undefined;
    const streamController = new AbortController();
    const delayController = new AbortController();

    const scheduleSnapshotRefresh = () => {
      if (refreshTimer != null) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        void loadRun(token, selectedId).catch((cause) => {
          if (active && mounted.current && cause instanceof Error) {
            setError(cause.message);
          }
        });
      }, STREAM_REFRESH_DEBOUNCE_MS);
    };

    const waitForRetry = (delay: number) =>
      new Promise<void>((resolve) => {
        let timer: number | undefined;
        const finish = () => {
          if (timer != null) window.clearTimeout(timer);
          delayController.signal.removeEventListener("abort", finish);
          resolve();
        };
        timer = window.setTimeout(finish, delay);
        delayController.signal.addEventListener("abort", finish, {
          once: true,
        });
      });

    const listen = async () => {
      while (active) {
        if (mounted.current) {
          setLiveUpdateStatus(
            failures === 0
              ? "connecting"
              : failures >= 3
                ? "offline"
                : "retrying",
          );
        }
        try {
          await harnessEventStream(
            token,
            `/api/runs/${encodeURIComponent(selectedId)}/stream?after=${cursor}`,
            {
              signal: streamController.signal,
              onMessage: (message) => {
                if (!active || !mounted.current) return;
                const nextCursor = Number(message.id);
                if (Number.isSafeInteger(nextCursor) && nextCursor > cursor) {
                  cursor = nextCursor;
                } else if (
                  message.data &&
                  typeof message.data === "object" &&
                  "cursor" in message.data
                ) {
                  const announced = Number(message.data.cursor);
                  if (Number.isSafeInteger(announced) && announced > cursor) {
                    cursor = announced;
                  }
                }
                failures = 0;
                setLiveUpdateStatus("live");
                setLastLiveEventAt(new Date().toISOString());
                if (
                  message.event === "run.event" ||
                  message.event === "run.state"
                ) {
                  scheduleSnapshotRefresh();
                }
              },
            },
          );
          if (!active) return;
          throw new Error("Live update stream ended.");
        } catch (cause) {
          if (!active || streamController.signal.aborted) return;
          failures += 1;
          if (mounted.current) {
            setLiveUpdateStatus(failures >= 3 ? "offline" : "retrying");
          }
          const delay = Math.min(
            STREAM_RETRY_MAX_MS,
            STREAM_RETRY_BASE_MS * 2 ** Math.min(failures - 1, 4),
          );
          await waitForRetry(delay);
        }
      }
    };

    void listen();
    return () => {
      active = false;
      streamController.abort();
      delayController.abort();
      if (refreshTimer != null) window.clearTimeout(refreshTimer);
    };
  }, [
    connected,
    loadRun,
    selectedId,
    streamRunId,
    token,
  ]);

  const selectRun = useCallback(
    async (runId: string) => {
      setSelectedId(runId);
      setSelectedRun(null);
      setError("");
      try {
        await loadRun(token, runId);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "Could not open task.");
      }
    },
    [loadRun, token],
  );

  const newThread = useCallback(() => {
    setSelectedId(null);
    setSelectedRun(null);
    setError("");
  }, []);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const task = messageText(message);
      setError("");
      try {
        let run: RunSnapshot;
        if (selectedRun && ACTIVE_OR_PAUSED.has(selectedRun.status)) {
          run = await harnessJson<RunSnapshot>(
            token,
            `/api/runs/${encodeURIComponent(selectedRun.run_id)}/steer`,
            {
              method: "POST",
              body: JSON.stringify({ instruction: task, auto_resume: true }),
            },
          );
        } else {
          run = await harnessJson<RunSnapshot>(token, "/api/runs", {
            method: "POST",
            body: JSON.stringify({
              task,
              auto_start: true,
              model_provider: selectedProvider || null,
              source_client: "chat-workspace",
            }),
          });
          setSelectedId(run.run_id);
        }
        setSelectedRun(run);
        await refresh(token, run.run_id);
      } catch (cause) {
        const message =
          cause instanceof Error ? cause.message : "Could not send the task.";
        setError(message);
        throw cause;
      }
    },
    [refresh, selectedProvider, selectedRun, token],
  );

  const onCancel = useCallback(async () => {
    if (!selectedRun) return;
    const run = await harnessJson<RunSnapshot>(
      token,
      `/api/runs/${encodeURIComponent(selectedRun.run_id)}/pause`,
      {
        method: "POST",
        body: JSON.stringify({ reason: "paused from chat workspace" }),
      },
    );
    setSelectedRun(run);
  }, [selectedRun, token]);

  const continueRun = useCallback(async () => {
    if (!selectedRun) return;
    const run = await harnessJson<RunSnapshot>(
      token,
      `/api/runs/${encodeURIComponent(selectedRun.run_id)}/continue`,
      { method: "POST" },
    );
    setSelectedRun(run);
  }, [selectedRun, token]);

  const respondToApproval = useCallback(
    async (decision: RespondToToolApprovalOptions) => {
      if (!selectedRun) return;
      const run = await harnessJson<RunSnapshot>(
        token,
        `/api/runs/${encodeURIComponent(
          selectedRun.run_id,
        )}/approvals/${encodeURIComponent(decision.approvalId)}`,
        {
          method: "POST",
          headers: {
            "X-PiKVM-Approval-Intent": decision.approvalId,
          },
          body: JSON.stringify({
            type: decision.approved ? "approve" : "reject",
            reason: decision.reason || "",
          }),
        },
      );
      setSelectedRun(run);
    },
    [selectedRun, token],
  );

  const disconnect = useCallback(() => {
    clearStoredToken();
    setToken("");
    setConnected(false);
    setRuns([]);
    setProviders({});
    setSelectedId(null);
    setSelectedRun(null);
    setError("");
    setLiveUpdateStatus("offline");
    setLastLiveEventAt(null);
  }, []);

  const isRunning = Boolean(
    selectedRun &&
      ["planning", "running", "executing", "verifying"].includes(
        selectedRun.status,
      ),
  );

  return useMemo(
    () => ({
      token,
      connected,
      loading,
      runs,
      selectedId,
      selectedRun,
      providers,
      selectedProvider,
      error,
      liveUpdateStatus,
      lastLiveEventAt,
      isRunning,
      connect,
      disconnect,
      selectRun,
      newThread,
      setSelectedProvider,
      onNew,
      onCancel,
      continueRun,
      respondToApproval,
      refresh,
    }),
    [
      token,
      connected,
      loading,
      runs,
      selectedId,
      selectedRun,
      providers,
      selectedProvider,
      error,
      liveUpdateStatus,
      lastLiveEventAt,
      isRunning,
      connect,
      disconnect,
      selectRun,
      newThread,
      onNew,
      onCancel,
      continueRun,
      respondToApproval,
      refresh,
    ],
  );
}
