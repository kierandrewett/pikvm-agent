import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AppendMessage,
  RespondToToolApprovalOptions,
} from "@assistant-ui/react";
import {
  clearStoredToken,
  HarnessApiError,
  harnessEventStream,
  harnessJson,
  readStoredToken,
  storeToken,
} from "@/lib/harness-api";
import type {
  LiveUpdateStatus,
  ModelPreferences,
  ModelRole,
  ProviderCatalogEntry,
  ProviderConnectionInput,
  ProviderConnectionResult,
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
const MODEL_ROLES: ModelRole[] = ["reasoner", "controller", "verifier"];

export const reconcileIntervalMs = (status: LiveUpdateStatus) =>
  status === "retrying" || status === "offline"
    ? DEGRADED_RECONCILE_MS
    : LIVE_RECONCILE_MS;

const rejectsWorkspaceToken = (cause: unknown) =>
  cause instanceof HarnessApiError &&
  (cause.status === 401 || cause.status === 403);

export const loadProviderCatalog = async (accessToken: string) => {
  try {
    return await harnessJson<ProviderCatalogEntry[]>(
      accessToken,
      "/api/provider-catalog",
    );
  } catch (cause) {
    if (cause instanceof HarnessApiError && cause.status === 404) return [];
    throw cause;
  }
};

export const createRunPayload = (
  task: string,
  preferences: ModelPreferences,
) => ({
  task,
  auto_start: true,
  model_preferences:
    Object.keys(preferences).length > 0 ? preferences : null,
  source_client: "chat-workspace",
});

const messageText = (message: AppendMessage) => {
  const text = message.content
    .filter(
      (
        part,
      ): part is Extract<(typeof message.content)[number], { type: "text" }> =>
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
  const [providerCatalog, setProviderCatalog] = useState<
    ProviderCatalogEntry[]
  >([]);
  const [modelPreferences, setModelPreferences] =
    useState<ModelPreferences>({});
  const [connectingProvider, setConnectingProvider] = useState(false);
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
          model_route: run.model_route,
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
      const [nextRuns, nextProviders, nextCatalog, nextRun] = await Promise.all(
        [
          harnessJson<RunSummary[]>(accessToken, "/api/runs"),
          harnessJson<ProviderMap>(accessToken, "/api/providers"),
          loadProviderCatalog(accessToken),
          runId ? loadRun(accessToken, runId) : Promise.resolve(null),
        ],
      );
      if (!mounted.current) return;
      setRuns(nextRuns);
      setProviders(nextProviders);
      setProviderCatalog(nextCatalog);
      if (!runId) setSelectedRun(null);
      setModelPreferences((current) =>
        Object.fromEntries(
          MODEL_ROLES.flatMap((role) => {
            const provider = current[role];
            return provider &&
              nextProviders[provider] &&
              nextProviders[provider]?.ready !== false
              ? [[role, provider]]
              : [];
          }),
        ),
      );
      return nextRun;
    },
    [loadRun, selectedId, token],
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
        const [nextRuns, nextProviders, nextCatalog] = await Promise.all([
          harnessJson<RunSummary[]>(accessToken, "/api/runs"),
          harnessJson<ProviderMap>(accessToken, "/api/providers"),
          loadProviderCatalog(accessToken),
        ]);
        if (!mounted.current) return;
        setToken(accessToken);
        storeToken(accessToken);
        setRuns(nextRuns);
        setProviders(nextProviders);
        setProviderCatalog(nextCatalog);
        setConnected(true);
        const firstId = nextRuns[0]?.run_id ?? null;
        setSelectedId(firstId);
        if (firstId) await loadRun(accessToken, firstId);
      } catch (cause) {
        if (rejectsWorkspaceToken(cause)) clearStoredToken();
        if (!mounted.current) return;
        if (rejectsWorkspaceToken(cause)) setConnected(false);
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
    const interval = reconcileIntervalMs(liveUpdateStatus);
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
  }, [connected, loadRun, selectedId, streamRunId, token]);

  const selectRun = useCallback(
    async (runId: string) => {
      setSelectedId(runId);
      setSelectedRun(null);
      setError("");
      try {
        await loadRun(token, runId);
      } catch (cause) {
        setError(
          cause instanceof Error ? cause.message : "Could not open task.",
        );
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
            body: JSON.stringify(createRunPayload(task, modelPreferences)),
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
    [modelPreferences, refresh, selectedRun, token],
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
        )}/approvals/${encodeURIComponent(
          decision.approvalId,
        )}?background=true`,
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
    setProviderCatalog([]);
    setModelPreferences({});
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
  const routeLocked = Boolean(
    selectedRun && ACTIVE_OR_PAUSED.has(selectedRun.status),
  );

  const setModelPreference = useCallback(
    (role: ModelRole, provider: string) => {
      if (routeLocked) return;
      setModelPreferences((current) => {
        const next = { ...current };
        if (provider) next[role] = provider;
        else delete next[role];
        return next;
      });
    },
    [routeLocked],
  );

  const resetModelPreferences = useCallback(() => {
    if (!routeLocked) setModelPreferences({});
  }, [routeLocked]);

  const connectProvider = useCallback(
    async (
      input: ProviderConnectionInput,
    ): Promise<ProviderConnectionResult> => {
      setConnectingProvider(true);
      try {
        const result = await harnessJson<ProviderConnectionResult>(
          token,
          "/api/providers",
          {
            method: "POST",
            body: JSON.stringify(input),
          },
        );
        await refresh(token, selectedId);
        return result;
      } finally {
        if (mounted.current) setConnectingProvider(false);
      }
    },
    [refresh, selectedId, token],
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
      providerCatalog,
      connectingProvider,
      modelPreferences,
      routeLocked,
      error,
      liveUpdateStatus,
      lastLiveEventAt,
      isRunning,
      connect,
      disconnect,
      selectRun,
      newThread,
      setModelPreference,
      resetModelPreferences,
      connectProvider,
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
      providerCatalog,
      connectingProvider,
      modelPreferences,
      routeLocked,
      error,
      liveUpdateStatus,
      lastLiveEventAt,
      isRunning,
      connect,
      disconnect,
      selectRun,
      newThread,
      setModelPreference,
      resetModelPreferences,
      connectProvider,
      onNew,
      onCancel,
      continueRun,
      respondToApproval,
      refresh,
    ],
  );
}
