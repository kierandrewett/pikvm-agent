import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AppendMessage,
  RespondToToolApprovalOptions,
} from "@assistant-ui/react";
import {
  clearPendingCreate,
  clearStoredRunId,
  clearStoredToken,
  HarnessApiError,
  harnessEventStream,
  harnessJson,
  pendingCreateRequestId,
  readStoredRunId,
  readStoredToken,
  storeRunId,
  storeToken,
} from "@/lib/harness-api";
import {
  eventNeedsSnapshotReconciliation,
  isHarnessEvent,
  preferNewestRunRevision,
  reduceRunEvent,
  reduceRunState,
} from "@/lib/live-run-reducer";
import type {
  AssistantTool,
  AssistantToolServerMap,
  ComputerConnection,
  HarnessHealth,
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

const STREAM_RETRY_BASE_MS = 500;
const STREAM_RETRY_MAX_MS = 5_000;
const LIVE_RECONCILE_MS = 15_000;
const DEGRADED_RECONCILE_MS = 1_500;
const MODEL_ROLES: ModelRole[] = ["reasoner", "controller", "verifier"];

export const reconcileIntervalMs = (
  status: LiveUpdateStatus,
  awaitingExternalRun = false,
) =>
  awaitingExternalRun || status === "retrying" || status === "offline"
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

export const loadAssistantTools = async (accessToken: string) => {
  try {
    return await harnessJson<AssistantTool[]>(accessToken, "/api/tools");
  } catch (cause) {
    if (cause instanceof HarnessApiError && cause.status === 404) return [];
    throw cause;
  }
};

export const loadAssistantToolServers = async (accessToken: string) => {
  try {
    return await harnessJson<AssistantToolServerMap>(
      accessToken,
      "/api/tool-servers",
    );
  } catch (cause) {
    if (cause instanceof HarnessApiError && cause.status === 404) return {};
    throw cause;
  }
};

export const loadHarnessHealth = async (accessToken: string) => {
  const health = await harnessJson<HarnessHealth>(accessToken, "/api/health");
  return health.computer_control !== "disabled";
};

export const loadComputerConnection = async (
  accessToken: string,
): Promise<ComputerConnection | null> => {
  try {
    const connection = await harnessJson<{
      enabled: boolean;
      mcp_server_name: string;
      machine_name: string;
    }>(accessToken, "/api/computer-connection");
    return {
      enabled: connection.enabled,
      mcpServerName: connection.mcp_server_name,
      machineName: connection.machine_name,
    };
  } catch (cause) {
    if (cause instanceof HarnessApiError && cause.status === 404) return null;
    throw cause;
  }
};

const defaultComputerConnection = (enabled: boolean): ComputerConnection => ({
  enabled,
  mcpServerName: "Managed PiKVM MCP",
  machineName: enabled ? "Managed computer" : "No computer",
});

export const createRunPayload = (
  task: string,
  preferences: ModelPreferences,
  clientRequestId: string,
) => ({
  task,
  mode: "assistant",
  auto_start: true,
  model_preferences:
    Object.keys(preferences).length > 0 ? preferences : null,
  source_client: "chat-workspace",
  client_request_id: clientRequestId,
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
  if (!text) throw new Error("Enter a message.");
  return text;
};

const summaryForRun = (run: RunSnapshot): RunSummary => ({
  run_id: run.run_id,
  task: run.task,
  status: run.status,
  mode: run.mode,
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
});

export function useHarnessWorkspace() {
  const [token, setToken] = useState("");
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(false);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<RunSnapshot | null>(null);
  const [providers, setProviders] = useState<ProviderMap>({});
  const [tools, setTools] = useState<AssistantTool[]>([]);
  const [toolServers, setToolServers] = useState<AssistantToolServerMap>({});
  const [computerConnection, setComputerConnection] =
    useState<ComputerConnection>(() => defaultComputerConnection(true));
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
  const selectedIdRef = useRef<string | null>(null);
  const autoFollowExternalRunRef = useRef(true);

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
      if (selectedIdRef.current === runId) {
        setSelectedRun((current) => preferNewestRunRevision(current, run));
      }
      setRuns((current) => {
        const index = current.findIndex((item) => item.run_id === run.run_id);
        const summary = summaryForRun(run);
        if (index < 0) return [summary, ...current];
        const next = [...current];
        next[index] = preferNewestRunRevision(next[index], summary);
        return next;
      });
    }
    return run;
  }, []);

  const refresh = useCallback(
    async (accessToken = token, runId = selectedId) => {
      if (!accessToken) return;
      const [
        nextRuns,
        nextProviders,
        nextCatalog,
        nextTools,
        nextToolServers,
        nextRun,
      ] = await Promise.all(
        [
          harnessJson<RunSummary[]>(accessToken, "/api/runs"),
          harnessJson<ProviderMap>(accessToken, "/api/providers"),
          loadProviderCatalog(accessToken),
          loadAssistantTools(accessToken),
          loadAssistantToolServers(accessToken),
          runId ? loadRun(accessToken, runId) : Promise.resolve(null),
        ],
      );
      if (!mounted.current) return;
      setRuns((current) =>
        nextRuns.map((run) =>
          preferNewestRunRevision(
            current.find((candidate) => candidate.run_id === run.run_id),
            run,
          ),
        ),
      );
      setProviders(nextProviders);
      setProviderCatalog(nextCatalog);
      setTools(nextTools);
      setToolServers(nextToolServers);
      const delegatedRun =
        !runId &&
        selectedIdRef.current === null &&
        autoFollowExternalRunRef.current
          ? nextRuns.find(
              (run) =>
                run.origin === "managed" &&
                run.caller?.interface === "managed_mcp",
            )
          : undefined;
      if (delegatedRun) {
        autoFollowExternalRunRef.current = false;
        selectedIdRef.current = delegatedRun.run_id;
        storeRunId(delegatedRun.run_id);
        setSelectedId(delegatedRun.run_id);
        await loadRun(accessToken, delegatedRun.run_id);
      } else if (!runId) {
        setSelectedRun(null);
      }
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
        const [
          nextRuns,
          nextProviders,
          nextCatalog,
          nextTools,
          nextToolServers,
          nextComputerControlEnabled,
          nextComputerConnection,
        ] = await Promise.all([
          harnessJson<RunSummary[]>(accessToken, "/api/runs"),
          harnessJson<ProviderMap>(accessToken, "/api/providers"),
          loadProviderCatalog(accessToken),
          loadAssistantTools(accessToken),
          loadAssistantToolServers(accessToken),
          loadHarnessHealth(accessToken),
          loadComputerConnection(accessToken),
        ]);
        if (!mounted.current) return;
        setToken(accessToken);
        storeToken(accessToken);
        setRuns(nextRuns);
        setProviders(nextProviders);
        setProviderCatalog(nextCatalog);
        setTools(nextTools);
        setToolServers(nextToolServers);
        setComputerConnection(
          nextComputerConnection ??
            defaultComputerConnection(nextComputerControlEnabled),
        );
        setConnected(true);
        for (const run of nextRuns) {
          const requestId = run.caller?.request_id;
          if (typeof requestId === "string") {
            clearPendingCreate(requestId);
          }
        }
        const storedRunId = readStoredRunId();
        const initialId = storedRunId || nextRuns[0]?.run_id || null;
        autoFollowExternalRunRef.current = initialId === null;
        selectedIdRef.current = initialId;
        setSelectedId(initialId);
        if (initialId) {
          storeRunId(initialId);
          try {
            await loadRun(accessToken, initialId);
          } catch (cause) {
            if (mounted.current) {
              setError(
                cause instanceof Error
                  ? cause.message
                  : "Could not restore the selected task.",
              );
            }
          }
        }
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
    const interval = reconcileIntervalMs(
      liveUpdateStatus,
      selectedId === null && autoFollowExternalRunRef.current,
    );
    const timer = window.setInterval(() => {
      void refresh().catch((cause) => {
        if (cause instanceof Error && mounted.current) setError(cause.message);
      });
    }, interval);
    return () => window.clearInterval(timer);
  }, [connected, liveUpdateStatus, refresh, selectedId, token]);

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
    let liveRun = selectedRun;
    let failures = 0;
    let reconciliation: Promise<RunSnapshot | null> | null = null;
    const streamController = new AbortController();
    const delayController = new AbortController();

    const publishLiveRun = (run: RunSnapshot) => {
      liveRun = run;
      cursor = run.event_cursor;
      setSelectedRun(run);
      setRuns((current) => {
        const summary = summaryForRun(run);
        const index = current.findIndex((item) => item.run_id === run.run_id);
        if (index < 0) return [summary, ...current];
        const next = [...current];
        next[index] = summary;
        return next;
      });
    };

    const reconcileSnapshot = () => {
      if (reconciliation) return reconciliation;
      reconciliation = loadRun(token, selectedId)
        .then((run) => {
          if (active && run) {
            liveRun = preferNewestRunRevision(liveRun, run);
            cursor = liveRun.event_cursor;
          }
          return run;
        })
        .catch((cause) => {
          if (active && mounted.current && cause instanceof Error) {
            setError(cause.message);
          }
          return null;
        })
        .finally(() => {
          reconciliation = null;
        });
      return reconciliation;
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
                if (message.event === "run.event") {
                  if (!liveRun || !isHarnessEvent(message.data)) {
                    void reconcileSnapshot();
                    return;
                  }
                  const reduction = reduceRunEvent(liveRun, message.data);
                  if (reduction.gap) {
                    void reconcileSnapshot();
                    return;
                  }
                  if (reduction.changed) {
                    publishLiveRun(reduction.run);
                    if (eventNeedsSnapshotReconciliation(message.data)) {
                      void reconcileSnapshot();
                    }
                  }
                } else if (message.event === "run.state" && liveRun) {
                  publishLiveRun(reduceRunState(liveRun, message.data));
                }
                failures = 0;
                setLiveUpdateStatus("live");
                setLastLiveEventAt(new Date().toISOString());
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
          if (active) await reconcileSnapshot();
        }
      }
    };

    void listen();
    return () => {
      active = false;
      streamController.abort();
      delayController.abort();
    };
  }, [connected, loadRun, selectedId, streamRunId, token]);

  const selectRun = useCallback(
    async (runId: string) => {
      autoFollowExternalRunRef.current = false;
      selectedIdRef.current = runId;
      storeRunId(runId);
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
    autoFollowExternalRunRef.current = false;
    selectedIdRef.current = null;
    clearStoredRunId();
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
        const currentRun =
          selectedRun != null &&
          selectedIdRef.current === selectedRun.run_id
            ? selectedRun
            : null;
        if (selectedIdRef.current !== null && currentRun === null) {
          throw new Error(
            "The selected task could not be restored. No replacement task was started. Retry loading it or start a new task explicitly.",
          );
        }
        const reusableAssistant =
          currentRun != null &&
          (currentRun.conversation?.length ?? 0) > 0 &&
          !["aborted", "failed", "rejected"].includes(currentRun.status);
        if (
          currentRun &&
          (ACTIVE_OR_PAUSED.has(currentRun.status) || reusableAssistant)
        ) {
          run = await harnessJson<RunSnapshot>(
            token,
            `/api/runs/${encodeURIComponent(currentRun.run_id)}/steer`,
            {
              method: "POST",
              body: JSON.stringify({ instruction: task, auto_resume: true }),
            },
          );
        } else {
          const clientRequestId = pendingCreateRequestId(task);
          run = await harnessJson<RunSnapshot>(token, "/api/runs", {
            method: "POST",
            body: JSON.stringify(
              createRunPayload(task, modelPreferences, clientRequestId),
            ),
          });
          clearPendingCreate(clientRequestId);
          autoFollowExternalRunRef.current = false;
          selectedIdRef.current = run.run_id;
          storeRunId(run.run_id);
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
    clearStoredRunId();
    setToken("");
    setConnected(false);
    setRuns([]);
    setProviders({});
    setProviderCatalog([]);
    setTools([]);
    setToolServers({});
    setComputerConnection(defaultComputerConnection(true));
    setModelPreferences({});
    selectedIdRef.current = null;
    setSelectedId(null);
    setSelectedRun(null);
    setError("");
    setLiveUpdateStatus("offline");
    setLastLiveEventAt(null);
    autoFollowExternalRunRef.current = true;
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
      tools,
      toolServers,
      computerControlEnabled: computerConnection.enabled,
      computerConnection,
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
      tools,
      toolServers,
      computerConnection,
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
