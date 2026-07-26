(() => {
  "use strict";

  const TOKEN_KEY = "pikvm.harness.session-token";
  const MAX_VISIBLE_EVENTS = 500;
  const PERFORMANCE_REFRESH_MS = 4_000;
  const TERMINAL_STATUSES = new Set([
    "completed",
    "blocked",
    "rejected",
    "aborted",
    "failed",
  ]);
  const ACTIVE_STATUSES = new Set([
    "planning",
    "running",
    "paused",
    "needs_approval",
  ]);

  const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    runs: [],
    selectedId: null,
    selectedRun: null,
    selectedPerformance: null,
    performanceLoadedAt: 0,
    providers: {},
    controlMode: "interactive",
    apiStatus: "connecting",
    streamStatus: "idle",
    streamLastSignalAt: null,
    streamReconnectAttempt: 0,
    streamReconnectTotal: 0,
    cursor: 0,
    streamController: null,
    refreshTimer: null,
    frameTimer: null,
    pollTimer: null,
    ageTimer: null,
    frameObjectUrl: null,
    displayedFrameId: null,
    frameLoadedAt: null,
    frameMode: "checkpoint",
    liveCapable: false,
    verificationObjectUrl: null,
    verificationKey: null,
    verificationError: null,
    taskDialogMode: "create",
    filters: new Set([
      "model",
      "computer",
      "action",
      "approval",
      "verification",
      "run",
    ]),
  };

  const elements = Object.fromEntries(
    [
      "app-shell",
      "auth-gate",
      "auth-form",
      "token-input",
      "auth-error",
      "connection-status",
      "activity-status",
      "activity-label",
      "provider-button",
      "provider-summary",
      "provider-popover",
      "provider-list",
      "close-providers",
      "new-task-button",
      "rail-new-task",
      "run-list",
      "run-empty",
      "run-count",
      "machine-target",
      "machine-session",
      "frame-freshness",
      "refresh-frame",
      "fit-frame",
      "live-screen",
      "machine-frame",
      "screen-empty",
      "screen-busy",
      "fact-machine",
      "fact-target",
      "fact-layer",
      "fact-frame",
      "fact-world",
      "fact-epoch",
      "fact-size",
      "transaction-subtitle",
      "control-assurance",
      "control-assurance-title",
      "control-assurance-detail",
      "efficiency-strip",
      "metric-wall",
      "metric-model",
      "metric-progress",
      "metric-recovery",
      "metric-budget",
      "transaction-content",
      "steer-button",
      "pause-button",
      "pause-label",
      "continue-button",
      "continue-label",
      "takeover-button",
      "event-timeline",
      "timeline-empty",
      "event-count",
      "timeline-filter-button",
      "timeline-filters",
      "approval-shelf",
      "approval-reason",
      "approval-id",
      "approval-risk",
      "approval-freshness",
      "approval-request",
      "approval-note",
      "approve-button",
      "reject-button",
      "abort-button",
      "toast-region",
      "new-task-dialog",
      "new-task-form",
      "task-dialog-eyebrow",
      "task-dialog-title",
      "task-input-label",
      "task-input",
      "auto-start-input",
      "auto-start-title",
      "auto-start-help",
      "task-error",
      "create-task-submit",
      "abort-dialog",
      "abort-form",
      "abort-reason",
      "abort-error",
      "abort-submit",
      "session-button",
      "session-dialog",
      "disconnect-button",
    ].map((id) => [id, document.getElementById(id)])
  );

  function bearerHeaders(extra = {}) {
    return {
      Authorization: `Bearer ${state.token}`,
      ...extra,
    };
  }

  async function apiFetch(path, options = {}) {
    const headers = bearerHeaders(options.headers || {});
    const response = await fetch(path, {
      ...options,
      headers,
      cache: "no-store",
    });
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        detail = payload.detail || detail;
      } catch {}
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  async function apiJson(path, options = {}) {
    const headers = {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    };
    const response = await apiFetch(path, { ...options, headers });
    return response.json();
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.previousDisabled = String(button.disabled);
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (label) {
        button.dataset.previousAriaLabel =
          button.getAttribute("aria-label") || "";
        button.setAttribute("aria-label", label);
      }
    } else {
      button.disabled = button.dataset.previousDisabled === "true";
      delete button.dataset.previousDisabled;
      button.removeAttribute("aria-busy");
      if ("previousAriaLabel" in button.dataset) {
        const previous = button.dataset.previousAriaLabel;
        if (previous) button.setAttribute("aria-label", previous);
        else button.removeAttribute("aria-label");
        delete button.dataset.previousAriaLabel;
      }
    }
  }

  function showError(element, message) {
    element.textContent = message;
    element.hidden = false;
  }

  function clearError(element) {
    element.textContent = "";
    element.hidden = true;
  }

  function toast(message, tone = "neutral") {
    const item = document.createElement("div");
    item.className = `toast ${tone}`;
    item.textContent = message;
    elements["toast-region"].append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function setConnection(label, tone) {
    const chip = elements["connection-status"];
    chip.className = `status-chip ${tone}`;
    chip.replaceChildren();
    const dot = document.createElement("span");
    dot.className = `signal-dot ${tone}`;
    dot.setAttribute("aria-hidden", "true");
    chip.append(dot, document.createTextNode(label));
  }

  function setApiStatus(status) {
    state.apiStatus = status;
    renderConnectionStatus();
  }

  function setStreamStatus(status, signalReceived = false) {
    state.streamStatus = status;
    if (signalReceived) state.streamLastSignalAt = Date.now();
    renderConnectionStatus();
  }

  function renderConnectionStatus() {
    if (state.apiStatus === "connecting") {
      setConnection("Connecting", "neutral");
      return;
    }
    if (state.apiStatus === "disconnected") {
      setConnection("Disconnected", "danger");
      return;
    }
    if (state.apiStatus === "interrupted") {
      setConnection("Harness unavailable", "danger");
      return;
    }
    if (state.streamStatus === "reconnecting") {
      setConnection(
        `Reconnecting · attempt ${state.streamReconnectAttempt}`,
        "warning"
      );
      return;
    }
    if (state.streamStatus === "connecting") {
      setConnection("Events connecting", "neutral");
      return;
    }
    if (state.streamStatus === "live") {
      const age = state.streamLastSignalAt
        ? Math.max(0, Math.floor((Date.now() - state.streamLastSignalAt) / 1000))
        : 0;
      setConnection(
        age < 2 ? "Events live" : `Events live · ${age}s signal`,
        age > 15 ? "warning" : "success"
      );
      return;
    }
    setConnection(
      state.controlMode === "external_benchmark"
        ? "Live benchmark connected"
        : "Harness connected",
      "success"
    );
  }

  function openConsole() {
    elements["auth-gate"].hidden = true;
    elements["app-shell"].removeAttribute("aria-hidden");
  }

  function closeConsole() {
    elements["auth-gate"].hidden = false;
    elements["app-shell"].setAttribute("aria-hidden", "true");
    elements["token-input"].value = "";
    window.setTimeout(() => elements["token-input"].focus(), 0);
  }

  async function connect() {
    setApiStatus("connecting");
    const [runs, providers, health] = await Promise.all([
      apiJson("/api/runs?limit=100"),
      apiJson("/api/providers"),
      apiJson("/api/health"),
    ]);
    state.runs = runs;
    state.providers = providers;
    state.controlMode = health.control_mode || "interactive";
    const externallyDriven = state.controlMode === "external_benchmark";
    elements["new-task-button"].hidden = externallyDriven;
    elements["rail-new-task"].hidden = externallyDriven;
    openConsole();
    setApiStatus("connected");
    renderProviders();
    renderRuns();
    if (!state.selectedId && state.runs.length) {
      await selectRun(state.runs[0].run_id);
    } else if (!state.runs.length) {
      renderNoSelection();
    }
    startPolling();
  }

  function disconnect() {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    stopStreaming();
    stopFrameLoop();
    stopPolling();
    releaseFrame();
    releaseVerificationImage();
    state.runs = [];
    state.selectedId = null;
    state.selectedRun = null;
    state.selectedPerformance = null;
    state.performanceLoadedAt = 0;
    state.controlMode = "interactive";
    renderRuns();
    renderNoSelection();
    if (elements["session-dialog"].open) elements["session-dialog"].close();
    closeConsole();
    setApiStatus("disconnected");
  }

  async function authenticate(event) {
    event.preventDefault();
    clearError(elements["auth-error"]);
    const token = elements["token-input"].value.trim();
    const minimumLength = elements["token-input"].minLength;
    if (token.length < minimumLength) {
      showError(
        elements["auth-error"],
        `The harness token must contain at least ${minimumLength} characters.`
      );
      return;
    }
    state.token = token;
    setBusy(event.submitter, true, "Connecting…");
    try {
      await connect();
      sessionStorage.setItem(TOKEN_KEY, token);
      elements["token-input"].value = "";
    } catch (error) {
      state.token = "";
      sessionStorage.removeItem(TOKEN_KEY);
      showError(
        elements["auth-error"],
        error.status === 401
          ? "The harness rejected this token."
          : `Could not reach the harness: ${error.message}`
      );
      closeConsole();
    } finally {
      setBusy(event.submitter, false);
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = window.setInterval(refreshOverview, 4000);
    state.ageTimer = window.setInterval(() => {
      renderFrameAge();
      renderActivityAge();
      renderConnectionStatus();
      renderEfficiency();
    }, 1000);
  }

  function stopPolling() {
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    if (state.ageTimer) window.clearInterval(state.ageTimer);
    state.pollTimer = null;
    state.ageTimer = null;
  }

  async function refreshOverview() {
    if (!state.token) return;
    try {
      const [runs, providers] = await Promise.all([
        apiJson("/api/runs?limit=100"),
        apiJson("/api/providers"),
      ]);
      state.runs = runs;
      state.providers = providers;
      setApiStatus("connected");
      renderRuns();
      renderProviders();
    } catch (error) {
      setApiStatus("interrupted");
      if (error.status === 401) disconnect();
    }
  }

  function statusMeta(status) {
    const table = {
      planning: ["Planning", "success"],
      running: ["Running", "success"],
      paused: ["Paused", "neutral"],
      needs_approval: ["Approval", "warning"],
      completed: ["Completed", "success"],
      blocked: ["Blocked", "danger"],
      rejected: ["Rejected", "danger"],
      aborted: ["Aborted", "danger"],
      failed: ["Failed", "danger"],
    };
    return table[status] || [status || "Unknown", "neutral"];
  }

  function runStatusMeta(run) {
    const acceptance =
      run.artifact_acceptance?.state || run.artifact_acceptance_state;
    if (acceptance === "pending" || acceptance === "capturing") {
      return ["Validating file", "warning"];
    }
    if (acceptance === "passed") return ["Accepted", "success"];
    if (acceptance === "failed") return ["Artifact failed", "danger"];
    return statusMeta(run.status);
  }

  function formatTime(value, includeDate = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat(undefined, {
      ...(includeDate ? { month: "short", day: "numeric" } : {}),
      hour: "2-digit",
      minute: "2-digit",
      second: includeDate ? undefined : "2-digit",
    }).format(date);
  }

  function truncate(value, maximum = 58) {
    const text = String(value || "");
    return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
  }

  function runSource(run) {
    const caller = run.caller || {};
    const name = caller.label || caller.name || "";
    return run.origin === "direct_mcp" ? `Direct ${name || "MCP"}` : name;
  }

  function renderRuns() {
    const list = elements["run-list"];
    list.replaceChildren();
    elements["run-count"].textContent = `${state.runs.length} ${
      state.runs.length === 1 ? "recorded" : "recorded"
    }`;
    elements["run-empty"].hidden = state.runs.length > 0;
    list.hidden = state.runs.length === 0;

    for (const run of state.runs) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "run-item";
      button.dataset.runId = run.run_id;
      button.setAttribute(
        "aria-current",
        String(run.run_id === state.selectedId)
      );

      const title = document.createElement("strong");
      title.textContent = run.task;
      title.title = run.task;
      const titleRow = document.createElement("span");
      titleRow.className = "run-item-title";
      titleRow.append(title);
      const source = runSource(run);
      if (source) {
        const origin = document.createElement("span");
        origin.className = "run-origin";
        origin.textContent = source;
        titleRow.append(origin);
      }

      const meta = document.createElement("span");
      meta.className = "run-item-meta";
      const status = document.createElement("span");
      status.className = "run-item-state";
      const [label, tone] = runStatusMeta(run);
      const dot = document.createElement("span");
      dot.className = `signal-dot ${tone}`;
      dot.setAttribute("aria-hidden", "true");
      const statusLabel = document.createElement("span");
      statusLabel.textContent = label;
      status.append(dot, statusLabel);
      const time = document.createElement("time");
      time.dateTime = run.updated_at || "";
      time.textContent = formatTime(run.updated_at);
      meta.append(status, time);
      button.append(titleRow, meta);
      button.addEventListener("click", () => selectRun(run.run_id));
      list.append(button);
    }
  }

  async function selectRun(runId) {
    if (!runId) return;
    stopStreaming();
    stopFrameLoop();
    state.selectedId = runId;
    state.selectedRun = state.runs.find((run) => run.run_id === runId) || null;
    state.selectedPerformance = null;
    state.performanceLoadedAt = 0;
    state.cursor = 0;
    releaseVerificationImage();
    renderRuns();
    renderSelectedRun();
    try {
      await refreshSelected(runId, true);
      startEventStream(runId);
    } catch (error) {
      toast(`Could not open run: ${error.message}`, "error");
    }
  }

  function scheduleSelectedRefresh(runId) {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(() => {
      state.refreshTimer = null;
      refreshSelected(runId).catch((error) => {
        toast(`Run refresh failed: ${error.message}`, "error");
      });
    }, 90);
  }

  async function refreshSelected(runId = state.selectedId, forceFrame = false) {
    if (!runId || runId !== state.selectedId) return;
    const refreshPerformance =
      !state.performanceLoadedAt ||
      Date.now() - state.performanceLoadedAt >= PERFORMANCE_REFRESH_MS;
    const [run, performance] = await Promise.all([
      apiJson(`/api/runs/${encodeURIComponent(runId)}`),
      refreshPerformance
        ? apiJson(
            `/api/runs/${encodeURIComponent(runId)}/performance`
          ).catch(() => null)
        : Promise.resolve(state.selectedPerformance),
    ]);
    if (runId !== state.selectedId) return;
    if (refreshPerformance) state.performanceLoadedAt = Date.now();
    run.events = trimVisibleEvents(run.events || []);
    state.selectedRun = run;
    state.selectedPerformance = performance;
    const index = state.runs.findIndex((candidate) => candidate.run_id === runId);
    if (index >= 0) state.runs[index] = run;
    else state.runs.unshift(run);
    state.cursor = Math.max(
      state.cursor,
      run.event_cursor || 0,
      ...(run.events || []).map((event) => event.sequence || 0),
      0
    );
    renderRuns();
    renderSelectedRun();
    const verificationRefresh = refreshVerificationImage(run);
    const frameId = run.observation?.frame_id;
    const frameRefresh =
      forceFrame || (frameId != null && frameId !== state.displayedFrameId)
        ? refreshFrame(runId, frameId)
        : Promise.resolve();
    await Promise.all([verificationRefresh, frameRefresh]);
  }

  function trimVisibleEvents(events) {
    return events.length > MAX_VISIBLE_EVENTS
      ? events.slice(-MAX_VISIBLE_EVENTS)
      : events;
  }

  function reconnectDelay(attempt) {
    const exponent = Math.min(5, Math.max(0, attempt - 1));
    return Math.min(10_000, 500 * 2 ** exponent);
  }

  function stopStreaming() {
    if (state.streamController) state.streamController.abort();
    state.streamController = null;
    state.streamStatus = "idle";
    state.streamLastSignalAt = null;
    state.streamReconnectAttempt = 0;
    renderConnectionStatus();
  }

  async function startEventStream(runId) {
    stopStreaming();
    const controller = new AbortController();
    state.streamController = controller;
    setStreamStatus("connecting");

    while (
      !controller.signal.aborted &&
      state.token &&
      state.selectedId === runId
    ) {
      try {
        const response = await apiFetch(
          `/api/runs/${encodeURIComponent(runId)}/stream?after=${state.cursor}`,
          {
            headers: { Accept: "text/event-stream" },
            signal: controller.signal,
          }
        );
        if (!(response.body instanceof ReadableStream)) {
          throw new Error("event stream is unavailable in this browser");
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const result = await reader.read();
          if (result.done) break;
          buffer += decoder.decode(result.value, { stream: true });
          const chunks = buffer.split(/\r?\n\r?\n/);
          buffer = chunks.pop() || "";
          for (const chunk of chunks) consumeStreamBlock(runId, chunk);
        }
        if (!controller.signal.aborted) {
          throw new Error("event stream closed");
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error.status === 401) {
          disconnect();
          return;
        }
        state.streamReconnectAttempt += 1;
        state.streamReconnectTotal += 1;
        setStreamStatus("reconnecting");
        await wait(
          reconnectDelay(state.streamReconnectAttempt),
          controller.signal
        );
        if (!controller.signal.aborted) setStreamStatus("connecting");
      }
    }
  }

  function consumeStreamBlock(runId, block) {
    let eventName = "message";
    let id = null;
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("id:")) id = Number(line.slice(3).trim());
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    let payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }
    state.streamReconnectAttempt = 0;
    setStreamStatus("live", true);
    if (
      eventName === "stream.ready" ||
      eventName === "stream.heartbeat"
    ) {
      return;
    }
    if (eventName === "run.event") {
      if (id) state.cursor = Math.max(state.cursor, id);
      if (state.selectedRun && state.selectedRun.run_id === runId) {
        const events = state.selectedRun.events || [];
        if (!events.some((event) => event.sequence === payload.sequence)) {
          events.push(payload);
          state.selectedRun.events = trimVisibleEvents(events);
          state.selectedRun.event_count =
            Number(state.selectedRun.event_count || 0) + 1;
          state.selectedRun.event_cursor = Math.max(
            Number(state.selectedRun.event_cursor || 0),
            Number(payload.sequence || 0)
          );
          state.selectedRun.events_truncated =
            state.selectedRun.event_count >
            state.selectedRun.events.length;
          renderTimeline();
        }
      }
      scheduleSelectedRefresh(runId);
    } else if (eventName === "run.state") {
      if (state.selectedRun && state.selectedRun.run_id === runId) {
        state.selectedRun.status = payload.status;
        state.selectedRun.active_activity = payload.active_activity || null;
      }
      scheduleSelectedRefresh(runId);
    }
  }

  function wait(milliseconds, signal) {
    return new Promise((resolve) => {
      const timer = window.setTimeout(resolve, milliseconds);
      signal.addEventListener(
        "abort",
        () => {
          window.clearTimeout(timer);
          resolve();
        },
        { once: true }
      );
    });
  }

  function releaseFrame() {
    if (state.frameObjectUrl) URL.revokeObjectURL(state.frameObjectUrl);
    state.frameObjectUrl = null;
    state.displayedFrameId = null;
    state.frameLoadedAt = null;
    state.frameMode = "checkpoint";
    state.liveCapable = false;
    elements["machine-frame"].removeAttribute("src");
  }

  function releaseVerificationImage() {
    if (state.verificationObjectUrl) {
      URL.revokeObjectURL(state.verificationObjectUrl);
    }
    state.verificationObjectUrl = null;
    state.verificationKey = null;
    state.verificationError = null;
  }

  function verificationImageKey(run) {
    if (!run?.verification_image_available) return null;
    return `${run.run_id}:${run.verification_image_revision || 0}`;
  }

  async function refreshVerificationImage(run) {
    const key = verificationImageKey(run);
    if (!key) {
      if (state.verificationKey || state.verificationObjectUrl) {
        releaseVerificationImage();
        renderTransaction();
      }
      return;
    }
    if (key === state.verificationKey && state.verificationObjectUrl) return;

    const runId = run.run_id;
    state.verificationKey = key;
    state.verificationError = null;
    renderTransaction();
    try {
      const response = await apiFetch(
        `/api/runs/${encodeURIComponent(runId)}/verification-image`,
        { headers: { Accept: "image/*" } }
      );
      const blob = await response.blob();
      if (
        runId !== state.selectedId ||
        key !== verificationImageKey(state.selectedRun)
      ) {
        return;
      }
      const objectUrl = URL.createObjectURL(blob);
      if (state.verificationObjectUrl) {
        URL.revokeObjectURL(state.verificationObjectUrl);
      }
      state.verificationObjectUrl = objectUrl;
      state.verificationError = null;
      renderTransaction();
    } catch (error) {
      if (
        runId !== state.selectedId ||
        key !== verificationImageKey(state.selectedRun)
      ) {
        return;
      }
      state.verificationError =
        error.status === 404
          ? "The labelled evidence image is no longer available."
          : `Evidence unavailable: ${error.message}`;
      renderTransaction();
      if (error.status !== 404) {
        toast(`Evidence unavailable: ${error.message}`, "error");
      }
    }
  }

  async function refreshFrame(
    runId = state.selectedId,
    expectedFrameId = state.selectedRun?.observation?.frame_id
  ) {
    if (!runId || runId !== state.selectedId) return;
    elements["screen-busy"].hidden = false;
    try {
      const response = await apiFetch(
        `/api/runs/${encodeURIComponent(runId)}/frame`,
        { headers: { Accept: "image/*" } }
      );
      const blob = await response.blob();
      if (runId !== state.selectedId) return;
      const objectUrl = URL.createObjectURL(blob);
      if (state.frameObjectUrl) URL.revokeObjectURL(state.frameObjectUrl);
      state.frameObjectUrl = objectUrl;
      state.displayedFrameId = expectedFrameId ?? null;
      state.frameLoadedAt = Date.now();
      state.frameMode =
        response.headers.get("X-PiKVM-Frame-Mode") || "checkpoint";
      state.liveCapable =
        state.frameMode === "live" ||
        response.headers.get("X-PiKVM-Live-Capable") === "true";
      elements["machine-frame"].src = objectUrl;
      elements["machine-frame"].hidden = false;
      elements["screen-empty"].hidden = true;
      renderFrameAge();
    } catch (error) {
      if (error.status !== 404) toast(`Frame unavailable: ${error.message}`, "error");
      if (!state.frameObjectUrl) {
        elements["machine-frame"].hidden = true;
        elements["screen-empty"].hidden = false;
      }
    } finally {
      elements["screen-busy"].hidden = true;
      scheduleFrameLoop();
    }
  }

  function stopFrameLoop() {
    if (state.frameTimer) window.clearTimeout(state.frameTimer);
    state.frameTimer = null;
  }

  function scheduleFrameLoop() {
    stopFrameLoop();
    if (
      !state.liveCapable ||
      !state.token ||
      !state.selectedId
    ) {
      return;
    }
    state.frameTimer = window.setTimeout(async () => {
      state.frameTimer = null;
      if (document.visibilityState === "visible") {
        await refreshFrame(
          state.selectedId,
          state.selectedRun?.observation?.frame_id
        );
      } else {
        scheduleFrameLoop();
      }
    }, state.frameMode === "live" ? 650 : 2000);
  }

  function renderFrameAge() {
    const label = elements["frame-freshness"];
    if (!state.frameLoadedAt || state.displayedFrameId == null) {
      label.textContent = "No frame";
      label.classList.remove("stale");
      return;
    }
    const seconds = Math.max(0, Math.round((Date.now() - state.frameLoadedAt) / 1000));
    if (state.frameMode === "live") {
      label.textContent =
        seconds < 2 ? "Live snapshot" : `Live snapshot ${seconds}s ago`;
      label.classList.toggle("stale", seconds > 3);
    } else {
      label.textContent =
        seconds < 2
          ? "Checkpoint just received"
          : `Checkpoint received ${seconds}s ago`;
      label.classList.toggle("stale", seconds > 10);
    }
  }

  function activeActivity(run) {
    if (!run || !["planning", "running"].includes(run.status)) return null;
    const activity = run.active_activity;
    if (activity?.kind === "model") {
      return {
        label: `${titleCase(activity.role || "model")} · ${
          activity.provider || "provider"
        } call in flight`,
        startedAt: activity.started_at,
      };
    }
    if (activity?.kind === "tool") {
      return {
        label: `${activity.tool || "MCP tool"} in flight`,
        startedAt: activity.started_at,
      };
    }
    const events = Array.isArray(run.events) ? run.events : [];
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index];
      if (event.kind === "model.provider_started") {
        const data = event.data || {};
        const closed = events.slice(index + 1).some((candidate) => {
          if (
            ![
              "model.provider_completed",
              "model.provider_failed",
              "model.provider_skipped",
            ].includes(candidate.kind)
          ) {
            return false;
          }
          const candidateData = candidate.data || {};
          return (
            candidateData.provider === data.provider &&
            candidateData.role === data.role &&
            Number(candidateData.attempt || 0) === Number(data.attempt || 0)
          );
        });
        if (!closed) {
          return {
            label: `${titleCase(data.role || "model")} · ${
              data.provider || "provider"
            } call in flight`,
            startedAt: event.at,
          };
        }
      }
      if (event.kind === "action.attempted") {
        const closed = events.slice(index + 1).some((candidate) =>
          [
            "action.completed",
            "action.failed",
            "action.refused_stale",
            "action.refused_by_operator",
            "action.stale_world_refreshed",
            "action.stale_world_retry_checkpointed",
            "action.transport_uncertain",
            "action.completed_unverified",
            "action.recoverable_failure",
            "approval.required",
            "target.identity_changed",
          ].includes(candidate.kind)
        );
        if (!closed) {
          return {
            label: `${event.data?.tool || "MCP tool"} in flight`,
            startedAt: event.at,
          };
        }
      }
    }
    return {
      label:
        run.status === "planning"
          ? "Planning in progress"
          : "Control loop active",
      startedAt: run.updated_at,
    };
  }

  function elapsedLabel(startedAt) {
    const started = Date.parse(startedAt || "");
    if (!Number.isFinite(started)) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  function renderActivityAge() {
    const activity = activeActivity(state.selectedRun);
    elements["activity-status"].hidden = !activity;
    if (!activity) {
      elements["activity-label"].textContent = "Idle";
      return;
    }
    const elapsed = elapsedLabel(activity.startedAt);
    elements["activity-label"].textContent = `${activity.label}${
      elapsed ? ` · ${elapsed}` : ""
    }`;
  }

  function compactDuration(milliseconds) {
    const value = Math.max(0, Number(milliseconds || 0));
    if (value < 1_000) return `${Math.round(value)}ms`;
    if (value < 60_000) {
      return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)}s`;
    }
    const minutes = Math.floor(value / 60_000);
    const seconds = Math.floor((value % 60_000) / 1_000);
    return `${minutes}m ${seconds}s`;
  }

  function setEfficiencyMetric(id, value, detail) {
    const element = elements[id];
    element.textContent = value;
    element.title = detail || value;
  }

  function compactCost(microusd) {
    const dollars = Math.max(0, Number(microusd || 0)) / 1_000_000;
    if (dollars === 0) return "$0";
    if (dollars < 0.01) return `$${dollars.toFixed(6)}`;
    if (dollars < 1) return `$${dollars.toFixed(4)}`;
    return `$${dollars.toFixed(2)}`;
  }

  function renderBudgetMetric(run) {
    if (!run) {
      setEfficiencyMetric("metric-budget", "—", "No run budget available");
      return;
    }
    if (run.origin === "direct_mcp") {
      setEfficiencyMetric(
        "metric-budget",
        "external",
        "The managed harness cannot meter an external MCP client's model usage"
      );
      return;
    }
    const budget = run.model_budget || {};
    const attempts = Number(budget.provider_attempts || 0);
    const attemptLimit = budget.provider_attempt_limit;
    const costCap = budget.max_cost_microusd;
    const committed = Number(budget.committed_cost_microusd || 0);
    const outstanding = Number(budget.outstanding_cost_microusd || 0);
    const attemptLabel =
      attemptLimit == null ? `${attempts}` : `${attempts}/${attemptLimit}`;
    if (costCap != null) {
      setEfficiencyMetric(
        "metric-budget",
        `${attemptLabel} · ${compactCost(committed + outstanding)}/${compactCost(
          costCap
        )}`,
        `${attemptLabel} provider attempts · ${compactCost(
          committed
        )} settled · ${compactCost(outstanding)} reserved · explicit price table ${
          budget.pricing_version || "unlabelled"
        }`
      );
      return;
    }
    setEfficiencyMetric(
      "metric-budget",
      `${attemptLabel} attempts`,
      `${attemptLabel} provider attempts · no metered cost cap configured`
    );
  }

  function renderEfficiency() {
    const run = state.selectedRun;
    const performance = state.selectedPerformance;
    renderBudgetMetric(run);
    if (!run || !performance) {
      for (const id of [
        "metric-wall",
        "metric-model",
        "metric-progress",
        "metric-recovery",
      ]) {
        setEfficiencyMetric(id, "—", "No run metrics available");
      }
      return;
    }
    let wallClockMs = Number(performance.wall_clock_ms || 0);
    if (!TERMINAL_STATUSES.has(run.status)) {
      const created = Date.parse(run.created_at || "");
      if (Number.isFinite(created)) {
        wallClockMs = Math.max(wallClockMs, Date.now() - created);
      }
    }
    const modelCalls = (performance.model_lanes || []).reduce(
      (total, lane) => total + Number(lane.latency?.samples || 0),
      0
    );
    const recoveryCount =
      Number(performance.provider_fallbacks || 0) +
      Number(performance.provider_schema_repairs || 0) +
      Number(performance.provider_safety_downgrades || 0);
    const faultCount =
      Number(performance.provider_failures || 0) +
      Number(performance.action_recoverable_failures || 0) +
      Number(performance.action_stale_retries || 0);
    setEfficiencyMetric(
      "metric-wall",
      compactDuration(wallClockMs),
      `Run wall time · ${compactDuration(wallClockMs)}`
    );
    setEfficiencyMetric(
      "metric-model",
      `${compactDuration(performance.model_active_ms)} · ${modelCalls}`,
      `${modelCalls} completed model call${
        modelCalls === 1 ? "" : "s"
      } · ${compactDuration(performance.model_active_ms)} summed active time`
    );
    setEfficiencyMetric(
      "metric-progress",
      `${performance.progress_actions_completed || 0} / ${
        performance.actions_completed || 0
      }`,
      `${performance.progress_actions_completed || 0} progress-bearing · ${
        performance.observation_only_actions_completed || 0
      } observation-only completed actions`
    );
    setEfficiencyMetric(
      "metric-recovery",
      `${performance.autonomous_resumes || 0} auto · ${faultCount} fault`,
      `${performance.autonomous_resumes || 0} harness-owned automatic continuation · ${
        performance.autonomy_stops || 0
      } autonomy stop · ${recoveryCount} recovery · ${
        performance.provider_fallbacks || 0
      } fallback · ${
        performance.provider_schema_repairs || 0
      } schema repair · ${
        performance.provider_safety_downgrades || 0
      } safety downgrade · ${faultCount} failure or stale retry`
    );
  }

  function renderNoSelection() {
    state.selectedRun = null;
    state.selectedPerformance = null;
    state.performanceLoadedAt = 0;
    elements["machine-target"].textContent = "Unverified target";
    elements["machine-session"].textContent = "No session selected";
    elements["transaction-subtitle"].textContent =
      "Select a run to inspect its control loop";
    elements["control-assurance"].className = "control-assurance neutral";
    elements["control-assurance-title"].textContent =
      "No control mode selected";
    elements["control-assurance-detail"].textContent =
      "Choose a run to see who chooses actions and what independently checks them.";
    elements["fact-machine"].textContent = "—";
    elements["fact-target"].textContent = "—";
    elements["fact-layer"].textContent = "—";
    elements["fact-frame"].textContent = "—";
    elements["fact-world"].textContent = "—";
    elements["fact-epoch"].textContent = "—";
    elements["fact-size"].textContent = "—";
    elements["continue-button"].hidden = true;
    elements["steer-button"].hidden = true;
    elements["pause-button"].hidden = true;
    elements["takeover-button"].hidden = true;
    elements["abort-button"].disabled = true;
    elements["refresh-frame"].disabled = true;
    elements["fit-frame"].disabled = true;
    elements["approval-shelf"].hidden = true;
    releaseFrame();
    releaseVerificationImage();
    elements["machine-frame"].hidden = true;
    elements["screen-empty"].hidden = false;
    elements["transaction-content"].replaceChildren(
      buildEmptyTransaction()
    );
    renderActivityAge();
    renderEfficiency();
    renderTimeline();
  }

  function renderSelectedRun() {
    const run = state.selectedRun;
    if (!run) {
      renderNoSelection();
      return;
    }
    const observation = run.observation || {};
    const machine = observation.machine || {};
    const [statusLabel] = statusMeta(run.status);
    const direct = run.origin === "direct_mcp";
    const caller = run.caller || {};
    const source = runSource(run);
    elements["machine-target"].textContent = machine.fingerprint
      ? `${machine.fingerprint} · ${
          machine.desktop_layer || "layer not declared"
        }`
      : "Unverified target";
    elements["machine-session"].textContent = run.session_id
      ? `${machine.alias || "Unlabelled target"} · ${
          source ? `${source} · ` : ""
        }${truncate(
          run.session_id,
          24
        )} · ${statusLabel}`
      : `${machine.alias || "Unlabelled target"} · ${
          source ? `${source} · ` : ""
        }${statusLabel}`;
    elements["transaction-subtitle"].textContent = run.error
      ? run.error
      : truncate(run.task, 100);
    elements["control-assurance"].className = `control-assurance ${
      direct ? "direct" : "managed"
    }`;
    elements["control-assurance-title"].textContent = direct
      ? "Direct MCP control"
      : "Harness-managed control";
    elements["control-assurance-detail"].textContent = direct
      ? "The external client chooses tool calls. The daemon enforces policy and freshness. No independent model verifier is running."
      : `${
          source ? `Requested by ${source}. ` : ""
        }The harness owns planning, bounded action slices, independent verification, and meaningful pauses.`;
    elements["steer-button"].hidden =
      state.controlMode === "external_benchmark" ||
      direct ||
      !["planning", "running", "paused", "blocked"].includes(run.status);
    elements["fact-machine"].textContent = machine.alias || "Unlabelled target";
    elements["fact-target"].textContent = machine.fingerprint || "Unverified";
    elements["fact-layer"].textContent =
      machine.desktop_layer || "Layer not declared";
    elements["fact-frame"].textContent = observation.frame_id ?? "—";
    elements["fact-world"].textContent = observation.world_version ?? "—";
    elements["fact-epoch"].textContent = observation.control_epoch ?? "—";
    elements["fact-size"].textContent =
      observation.width && observation.height
        ? `${observation.width}×${observation.height}`
        : "—";
    elements["refresh-frame"].disabled = !run.observation;
    elements["fit-frame"].disabled = !state.frameObjectUrl;
    if (state.controlMode === "external_benchmark") {
      elements["continue-button"].hidden = true;
      elements["pause-button"].hidden = true;
    } else if (direct) {
      elements["continue-button"].hidden = run.status !== "paused";
      elements["continue-button"].disabled = false;
      elements["pause-button"].hidden = run.status !== "running";
      elements["pause-label"].textContent = "Pause MCP";
      elements["continue-label"].textContent = "Resume MCP";
      elements["pause-button"].title =
        "Block future direct MCP actions. Use Stop to interrupt the current computer session.";
    } else {
      elements["continue-button"].hidden = ![
        "paused",
        "planning",
        "running",
      ].includes(run.status);
      elements["continue-button"].disabled = run.status === "running";
      elements["pause-button"].hidden = !["planning", "running"].includes(
        run.status
      );
      elements["pause-label"].textContent = "Pause";
      elements["continue-label"].textContent = "Continue";
      elements["pause-button"].removeAttribute("title");
    }
    elements["takeover-button"].hidden = run.status !== "needs_approval";
    elements["abort-button"].disabled =
      !ACTIVE_STATUSES.has(run.status) || TERMINAL_STATUSES.has(run.status);
    renderActivityAge();
    renderEfficiency();
    renderTransaction();
    renderTimeline();
    renderApproval();
  }

  function buildEmptyTransaction() {
    const empty = document.createElement("div");
    empty.className = "transaction-empty";
    const title = document.createElement("strong");
    title.textContent = "Nothing is hidden behind the transcript.";
    const text = document.createElement("p");
    text.textContent =
      "Plan, provider, exact non-secret MCP call, freshness, and verifier evidence will appear here as one transaction.";
    empty.append(title, text);
    return empty;
  }

  function appendText(parent, tag, text, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  }

  function makeList(items, ordered = false, className = "") {
    const list = document.createElement(ordered ? "ol" : "ul");
    if (className) list.className = className;
    for (const item of items || []) appendText(list, "li", item);
    return list;
  }

  function safeStringify(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function latestEvent(run, predicate) {
    return [...(run.events || [])].reverse().find(predicate);
  }

  function renderTransaction() {
    const run = state.selectedRun;
    const container = elements["transaction-content"];
    container.replaceChildren();
    if (!run) {
      container.append(buildEmptyTransaction());
      return;
    }
    const observation = run.observation;

    const grid = document.createElement("div");
    grid.className = "transaction-grid";

    const planBlock = document.createElement("section");
    planBlock.className = "transaction-block";
    const direct = run.origin === "direct_mcp";
    const caller = run.caller || {};
    const source = runSource(run);
    if (direct) {
      appendText(planBlock, "h3", "Control source");
      appendText(
        planBlock,
        "strong",
        `${caller.label || caller.name || "External MCP client"}${
          caller.model ? ` · ${caller.model}` : ""
        }`
      );
      appendText(
        planBlock,
        "p",
        "The external client chooses each action. The harness preflights policy, records the call before HID, and retains operator authority."
      );
    } else {
      appendText(planBlock, "h3", source ? `Plan · ${source}` : "Plan");
    }
    if (!direct && run.plan) {
      appendText(planBlock, "strong", run.plan.summary);
      planBlock.append(makeList(run.plan.steps, true));
      if (run.plan.constraints?.length) {
        appendText(
          planBlock,
          "p",
          `${run.plan.constraints.length} explicit constraint${
            run.plan.constraints.length === 1 ? "" : "s"
          }`
        );
      }
    } else if (!direct) {
      appendText(
        planBlock,
        "p",
        run.status === "planning"
          ? "The reasoner is producing observable completion criteria."
          : "No active plan is checkpointed."
      );
    }
    if (!direct && run.operator_guidance?.length) {
      appendText(planBlock, "h4", "Operator guidance");
      appendText(
        planBlock,
        "p",
        run.operator_guidance[run.operator_guidance.length - 1]
      );
    }

    const actionBlock = document.createElement("section");
    actionBlock.className = "transaction-block";
    appendText(actionBlock, "h3", "Intent & MCP");
    const action = run.pending_action;
    const attempt = latestEvent(run, (event) => event.kind === "action.attempted");
    const activity = run.active_activity;
    const currentTool =
      activity?.kind === "tool"
        ? {
            tool: activity.tool,
            arguments: activity.arguments,
          }
        : null;
    const controller = latestEvent(
      run,
      (event) =>
        event.kind === "model.completed" && event.data?.role === "controller"
    );
    appendText(
      actionBlock,
      "strong",
      direct
        ? attempt?.data?.tool ||
          currentTool?.tool ||
          "Waiting for a direct MCP call"
        : action?.intent ||
            run.last_controller?.intent ||
            controller?.data?.intent ||
            "Waiting for the controller"
    );
    const provider = direct
      ? caller.provider || caller.name
      : controller?.data?.provider;
    const model = direct ? caller.model : controller?.data?.model;
    appendText(
      actionBlock,
      "p",
      provider
        ? `${
            direct ? "Declared by MCP launcher · " : ""
          }${provider}${model ? ` · ${model}` : ""}`
        : direct
          ? "The MCP client did not declare a provider or model."
          : "No controller provider has acted yet."
    );
    if (attempt?.data?.arguments || currentTool?.arguments || action?.actions) {
      const code = appendText(
        actionBlock,
        "code",
        safeStringify(
          attempt?.data?.arguments ||
            currentTool?.arguments || {
              actions: action.actions,
              based_on_world_version: action.based_on_world_version,
              based_on_control_epoch: action.based_on_control_epoch,
              idempotency_key: action.idempotency_key,
            }
        ),
        "transaction-code"
      );
      code.setAttribute("aria-label", "Exact visible MCP arguments");
    }
    const callId = attempt?.data?.call_id;
    const outcome = latestEvent(
      run,
      (event) =>
        ["action.completed", "action.failed", "action.refused_by_operator"].includes(
          event.kind
        ) && (!callId || event.data?.call_id === callId)
    );
    if (outcome) {
      const latency =
        outcome.data?.latency_ms == null
          ? ""
          : ` in ${outcome.data.latency_ms} ms`;
      const summary =
        outcome.kind === "action.completed"
          ? `Completed${latency} · ${titleCase(outcome.data?.status || "recorded")}`
          : outcome.kind === "action.failed"
            ? `Failed${latency} · ${outcome.data?.error || "tool failure"}`
            : `Refused before execution · ${
                outcome.data?.reason || "operator control gate"
              }`;
      appendText(actionBlock, "p", `Tool outcome · ${summary}`);
    }

    const verifyBlock = document.createElement("section");
    verifyBlock.className = "transaction-block";
    appendText(verifyBlock, "h3", direct ? "Daemon evidence" : "Verification");
    if (direct && observation) {
      appendText(
        verifyBlock,
        "strong",
        observation.status === run.status
          ? `${titleCase(run.status)} · latest tool result`
          : `${titleCase(run.status)} · daemon result ${titleCase(
              observation.status
            )}`
      );
      appendText(
        verifyBlock,
        "p",
        "This is daemon-owned freshness and policy evidence. No separate model verifier is implied for direct control."
      );
    } else if (run.last_verification) {
      appendText(
        verifyBlock,
        "strong",
        `${titleCase(run.last_verification.verdict)} · ${
          run.last_verification.summary
        }`
      );
      if (run.last_verification.evidence?.length) {
        verifyBlock.append(
          makeList(run.last_verification.evidence, false, "evidence-list")
        );
      }
    } else {
      appendText(
        verifyBlock,
        "p",
        action
          ? "The action is checkpointed; independent evidence is still pending."
          : "The verifier has not evaluated a machine transition yet."
      );
    }
    if (observation) {
      appendText(
        verifyBlock,
        "code",
        `frame ${observation.frame_id ?? "—"} · world ${
          observation.world_version ?? "—"
        } · epoch ${observation.control_epoch ?? "—"}`,
        "transaction-code"
      );
    }
    if (!direct && run.verification_image_available) {
      const figure = document.createElement("figure");
      figure.className = "evidence-comparison";
      const caption = document.createElement("figcaption");
      appendText(caption, "strong", "Before → after evidence");
      appendText(
        caption,
        "span",
        "Labelled verifier view · full-resolution checkpoint"
      );
      if (
        state.verificationObjectUrl &&
        state.verificationKey === verificationImageKey(run)
      ) {
        const image = document.createElement("img");
        image.src = state.verificationObjectUrl;
        image.alt =
          "Labelled comparison of the machine screen before and after the latest action";
        figure.append(caption, image);
      } else {
        appendText(
          figure,
          "div",
          state.verificationError || "Loading labelled comparison…",
          "evidence-placeholder"
        );
        figure.prepend(caption);
      }
      verifyBlock.append(figure);
    }
    const acceptance = run.artifact_acceptance;
    if (acceptance) {
      const proof = document.createElement("div");
      proof.className = `artifact-acceptance ${acceptance.state}`;
      appendText(proof, "strong", "Saved artifact acceptance");
      const progress =
        acceptance.state === "pending"
          ? "Waiting for the managed task to finish."
          : acceptance.state === "capturing"
            ? "Reading the exact guest file through the host verifier."
            : acceptance.state === "passed"
              ? `Host-verified file · ${acceptance.checks_passed}/${acceptance.checks_total} checks passed · ${acceptance.byte_count} bytes`
              : `Host verification failed · ${
                  acceptance.error_class || "artifact evidence did not pass"
                }`;
      appendText(proof, "span", progress);
      if (acceptance.sha256) {
        appendText(
          proof,
          "code",
          `sha256:${acceptance.sha256.slice(0, 12)}`,
          "transaction-code"
        );
      }
      verifyBlock.append(proof);
    }
    const media = run.media_transaction;
    if (media) {
      const issue = media.cleanup_reason || media.failure_reason;
      appendText(verifyBlock, "div", `Read-only media transfer · ${titleCase(media.state)} · ${media.media_name} · sha256:${media.image_sha256.slice(0, 12)} · epoch ${media.control_epoch} · lease ${formatTime(media.lease_expires_at, true)} · ${(media.files || []).length} Exact guest-file receipts${issue ? ` · ${media.cleanup_reason ? "Cleanup required" : "Refused before upload"}: ${issue}` : ""}`, `media-transfer ${media.state}`);
    }
    const raw = document.createElement("details");
    raw.className = "raw-disclosure";
    appendText(raw, "summary", "Inspect run checkpoint");
    appendText(raw, "pre", safeStringify(run));
    verifyBlock.append(raw);

    grid.append(planBlock, actionBlock, verifyBlock);
    container.append(grid);
  }

  function eventCategory(kind) {
    const prefix = String(kind || "").split(".")[0];
    if (prefix === "model" || prefix === "controller") return "model";
    if (prefix === "computer") return "computer";
    if (prefix === "action") return "action";
    if (prefix === "approval") return "approval";
    if (prefix === "verification" || prefix === "artifact") {
      return "verification";
    }
    return "run";
  }

  function eventRole(event) {
    const category = eventCategory(event.kind);
    if (category === "model") {
      const role = event.data?.role;
      if (role === "reasoner") return "R";
      if (role === "controller") return "C";
      if (role === "verifier") return "V";
      return "AI";
    }
    return {
      computer: "M",
      action: "H",
      approval: "P",
      verification: "V",
      run: "•",
    }[category];
  }

  function eventSummary(event) {
    const data = event.data || {};
    const summaries = {
      "run.created": ["Run created", "Durable checkpoint initialized"],
      "computer.opened": [
        "Computer session opened",
        `Frame ${data.frame_id ?? "—"} · world ${data.world_version ?? "—"}`,
      ],
      "computer.open_failed": ["Computer open failed", data.error || ""],
      "action.checkpointed": [
        data.intent || "Action checkpointed",
        `Action ${data.index ?? "—"} · saved before HID`,
      ],
      "action.attempted": [
        data.tool || "MCP action attempted",
        data.source === "direct_mcp"
          ? `Recorded before execution · ${
              data.caller?.name || "external MCP client"
            }`
          : `Attempt ${data.attempt ?? "—"} · idempotent transaction`,
      ],
      "action.completed": [
        data.tool || "MCP tool completed",
        `Completed in ${data.latency_ms ?? "—"} ms · ${titleCase(
          data.status || "recorded"
        )} · frame ${data.frame_id ?? "—"} · world ${
          data.world_version ?? "—"
        }`,
      ],
      "action.transport_uncertain": [
        "Transport result uncertain",
        "Exact idempotency key retained for retry",
      ],
      "action.refused_stale": [
        "Stale action refused",
        data.status || "Freshness changed",
      ],
      "action.refused_by_operator": [
        "Direct MCP call refused",
        data.reason || "Operator control gate is active",
      ],
      "action.failed": [
        data.tool || "Direct MCP call failed",
        data.error || "",
      ],
      "approval.required": [
        "Human approval required",
        data.risk || "Consequential action held",
      ],
      "approval.resolving": [
        `Approval ${data.decision || "decision"}`,
        truncate(data.approval_id, 24),
      ],
      "approval.not_approved": [
        "Action not approved",
        data.decision || "Operator intervened",
      ],
      "approval.approved": [
        "Action approved once",
        truncate(data.approval_id, 24),
      ],
      "verification.uncertain": [
        "Verification uncertain",
        data.summary || "",
      ],
      "verification.failed": ["Verification failed", data.summary || ""],
      "artifact.pending": [
        "Saved artifact expected",
        data.label || "Host verification will follow model completion",
      ],
      "artifact.capturing": [
        "Capturing exact artifact",
        "Reading guest file bytes through the observer boundary",
      ],
      "artifact.passed": [
        "Host artifact acceptance passed",
        `${data.checks_passed ?? "—"}/${data.checks_total ?? "—"} semantic checks`,
      ],
      "artifact.failed": [
        "Host artifact acceptance failed",
        data.error_class || "Saved file evidence did not pass",
      ],
      "run.completed": ["Task complete", data.summary || "Verifier proved success"],
      "run.paused": ["Run paused", data.reason || "Waiting for operator"],
      "run.steered": [
        "Operator redirected the run",
        data.instruction || "A fresh plan is required",
      ],
      "run.autonomous_resume": [
        "Harness continued automatically",
        data.reason || "Internal action slice completed",
      ],
      "run.autonomy_stopped": [
        "Automatic continuation stopped",
        `Harness-owned limit ${data.limit ?? "—"} · operator review required`,
      ],
      "run.resumed": [
        "Direct MCP actions resumed",
        "Future calls may pass the operator gate",
      ],
      "run.abort_requested": [
        "Emergency stop requested",
        data.reason || "Waiting for daemon acknowledgement",
      ],
      "run.aborted": ["Run aborted", data.reason || "Stopped by operator"],
      "run.budget_exhausted": [
        "Action budget exhausted",
        "Run blocked before further HID",
      ],
      "model.budget_exhausted": [
        "Model budget exhausted",
        data.reason || "No further provider or HID action was allowed",
      ],
      "model.budget_reserved": [
        "Model attempt reserved",
        `${data.provider || "unknown"} · attempt ${
          data.provider_attempts ?? "—"
        }/${data.provider_attempt_limit ?? "—"}`,
      ],
      "model.budget_settled": [
        "Model cost settled",
        `${data.provider || "unknown"} · ${compactCost(
          data.actual_cost_microusd
        )}`,
      ],
      "model.budget_settlement_failed": [
        "Model cost settlement failed",
        data.reason || "Usage could not be verified",
      ],
    };
    if (summaries[event.kind]) return summaries[event.kind];
    if (event.kind === "model.started") {
      return [
        `${titleCase(data.role || "model")} started`,
        (data.candidates || []).join(" → ") || "Provider route selected",
      ];
    }
    if (event.kind === "model.completed") {
      let result = data.outcome || data.verdict || data.plan?.summary || "Schema valid";
      return [
        `${titleCase(data.role || "model")} completed`,
        `${data.provider || "provider"}${data.model ? ` · ${data.model}` : ""} · ${result}`,
      ];
    }
    if (event.kind === "model.failed") {
      return [`${titleCase(data.role || "model")} failed`, data.error || ""];
    }
    if (event.kind === "model.provider_started") {
      return [
        `${titleCase(data.role || "model")} provider started`,
        `${data.provider || "unknown"} · route ${
          Number(data.route_index ?? 0) + 1
        } · attempt ${data.attempt || 1}${
          data.repair ? " · repair" : ""
        }`,
      ];
    }
    if (event.kind === "model.provider_schema_repair") {
      return [
        "Schema repair",
        `${data.provider || "unknown"} returned locally invalid output · ${
          data.validation_errors || 0
        } validation issue${data.validation_errors === 1 ? "" : "s"}`,
      ];
    }
    if (event.kind === "model.provider_schema_safety_downgrade") {
      return [
        "Unsafe commit separated",
        `${data.provider || "unknown"} preserved ${
          data.preserved_actions || 0
        } safe draft action${data.preserved_actions === 1 ? "" : "s"} and dropped ${
          data.dropped_actions || 0
        } active follow-up${data.dropped_actions === 1 ? "" : "s"}`,
      ];
    }
    if (event.kind === "controller.pointer_noop_rejected") {
      return [
        "Pointer no-op rejected",
        "The controller proposed movement without task progress; no HID was sent.",
      ];
    }
    if (event.kind === "model.provider_failed") {
      return [
        "Provider failed",
        `${data.provider || "unknown"} · ${
          data.error_type || data.error || "provider-error"
        }`,
      ];
    }
    if (event.kind === "model.provider_skipped") {
      return [
        "Provider skipped",
        `${data.provider || "unknown"} · ${
          data.reason === "cooldown"
            ? `cooling down after ${data.error || "provider-error"}`
            : data.error || "not ready"
        }`,
      ];
    }
    if (event.kind === "model.provider_completed") {
      return [
        "Provider selected",
        `${data.provider || "unknown"}${
          data.model ? ` · ${data.model}` : ""
        } · ${data.latency_ms ?? "—"} ms`,
      ];
    }
    return [
      titleCase(String(event.kind || "event").replaceAll(".", " ")),
      data.reason || data.error || "",
    ];
  }

  function titleCase(value) {
    return String(value || "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function providerKindLabel(kind) {
    const labels = {
      anthropic_api: "Anthropic API",
      claude_cli: "Claude CLI",
      codex_cli: "Codex CLI",
      gemini_cli: "Gemini CLI",
      gemini_api: "Gemini API",
      vertex_gemini: "Vertex AI Gemini API",
      openai_compatible: "OpenAI-compatible API",
      openai_responses: "OpenAI Responses API",
      azure_openai_responses: "Azure OpenAI Responses API",
      subprocess_json: "Subprocess JSON",
    };
    return labels[kind] || titleCase(kind || "provider");
  }

  function providerAuthLabel(health) {
    const source = health.credential_source
      ? `${health.credential_source} · `
      : "";
    const labels = {
      api_key_env: "API key environment",
      bearer_env: "Bearer token environment",
      bearer_command: "CLI bearer token",
      external_or_none: "External or bridge-owned",
      saved_cli_login: "Saved CLI login",
    };
    return `${source}${
      labels[health.auth_mode] || titleCase(health.auth_mode || "auth unknown")
    }`;
  }

  function providerConformanceLabel(h) {
    const status = h.conformance_status || "not-run";
    const a = +h.conformance_calls_attempted || 0;
    if (!a) return `Blind conformance · ${titleCase(status)}`;
    const e = +h.conformance_exact || 0;
    const s = +h.conformance_schema_valid || 0;
    const m = +h.conformance_median_latency_ms;
    return `Blind conformance · ${titleCase(status)} · ${e}/${a} exact · ${s}/${a} schema${
      Number.isFinite(m) ? ` · ${Math.round(m)} ms median` : ""
    }${
      h.conformance_created_at
        ? ` · ${formatTime(h.conformance_created_at, true)}`
        : ""
    }`;
  }

  function renderTimeline() {
    const timeline = elements["event-timeline"];
    timeline.replaceChildren();
    const allEvents = state.selectedRun?.events || [];
    const totalEvents = Number(
      state.selectedRun?.event_count ?? allEvents.length
    );
    const events = allEvents.filter((event) =>
      state.filters.has(eventCategory(event.kind))
    );
    elements["event-count"].textContent =
      totalEvents > allEvents.length
        ? `${totalEvents} events · latest ${allEvents.length}`
        : `${totalEvents} ${totalEvents === 1 ? "event" : "events"}`;
    elements["timeline-empty"].hidden = events.length > 0;
    timeline.hidden = events.length === 0;

    events.forEach((event, index) => {
      const category = eventCategory(event.kind);
      const item = document.createElement("li");
      const failed =
        event.kind.includes("failed") ||
        event.kind.includes("uncertain") ||
        event.kind.includes("refused");
      item.className = `event-entry ${category}${failed ? " failed" : ""}`;

      const role = document.createElement("span");
      role.className = "event-role";
      role.textContent = eventRole(event);
      role.title = category;

      const details = document.createElement("details");
      details.className = "event-details";
      if (index === events.length - 1) details.open = true;
      const summary = document.createElement("summary");
      const summaryCopy = document.createElement("span");
      summaryCopy.className = "event-summary";
      const [title, subtitle] = eventSummary(event);
      appendText(summaryCopy, "strong", title);
      appendText(summaryCopy, "span", subtitle || event.kind);
      const time = document.createElement("time");
      time.dateTime = event.at || "";
      time.textContent = formatTime(event.at);
      summary.append(summaryCopy, time);
      const raw = appendText(details, "pre", safeStringify(event), "event-raw");
      raw.setAttribute("aria-label", `Raw event ${event.sequence}`);
      details.prepend(summary);
      item.append(role, details);
      timeline.append(item);
    });

    if (events.length && state.cursor === 0) {
      state.cursor = Math.max(...events.map((event) => event.sequence || 0), 0);
    }
  }

  function renderApproval() {
    const run = state.selectedRun;
    const request = run?.pending_approval;
    const visible = run?.status === "needs_approval" && request?.approval_id;
    elements["approval-shelf"].hidden = !visible;
    if (!visible) {
      elements["approval-note"].value = "";
      return;
    }
    elements["approval-reason"].textContent =
      request.reason || "A consequential action is waiting for your decision.";
    elements["approval-id"].textContent = `approval ${request.approval_id}`;
    elements["approval-risk"].textContent = request.risk
      ? `Risk: ${titleCase(request.risk)}`
      : "Risk not classified";
    const machine = request.machine || run.observation?.machine || {};
    elements["approval-freshness"].textContent = `frame ${
      request.frame_id ?? "—"
    } · world ${request.world_version ?? "—"} · epoch ${
      request.control_epoch ?? run.observation?.control_epoch ?? "—"
    } · ${machine.alias || "unlabelled target"} · ${
      machine.fingerprint || "unverified target"
    } · ${machine.desktop_layer || "layer not declared"}`;
    elements["approval-request"].textContent = safeStringify(request);
  }

  function renderProviders() {
    const list = elements["provider-list"];
    list.replaceChildren();
    const providers = Object.entries(state.providers);
    let healthy = 0;
    for (const [name, health] of providers) {
      const row = document.createElement("div");
      row.className = "provider-row";
      const identity = document.createElement("div");
      appendText(identity, "strong", name);
      appendText(
        identity,
        "span",
        `${providerKindLabel(health.kind)} · ${providerAuthLabel(
          health
        )} · ${titleCase(
          health.credential || "credential unknown"
        )} · ${titleCase(health.billing_mode || "unclassified")} billing`
      );
      appendText(
        identity,
        "span",
        `${health.interface || "Unknown"} · ${
          health.pixel_input || "Unknown"
        } · ${health.structured_output || "Unknown"}`
      );
      appendText(identity, "span", `${health.support_tier} · ${health.credential_owner}. Tier ≠ live-tested.`);
      const routes = Array.isArray(health.routes) ? health.routes : [];
      appendText(
        identity,
        "span",
        routes.length
          ? routes
              .map(
                (route) =>
                  `${titleCase(route.role || "role")} #${
                    route.position || "?"
                  }`
              )
              .join(" · ")
          : "No role route",
        "provider-routes"
      );
      appendText(
        identity,
        "span",
        `${health.configured_model || "Unavailable"} configured · ${
          health.last_model || "no response"
        } last`
      );
      const conformanceStatus = health.conformance_status || "not-run";
      appendText(
        identity,
        "span",
        providerConformanceLabel(health),
        `provider-conformance conformance-${conformanceStatus}`
      );
      const stats = document.createElement("div");
      stats.className = "provider-stats";
      const failures = Number(health.consecutive_failures || 0);
      const calls = Number(health.calls || 0);
      const successes = Number(health.successes || 0);
      const skipped = Number(health.skipped || 0);
      const ready = health.ready !== false;
      const cooldownUntil = Date.parse(health.cooldown_until || "");
      const coolingDown =
        Number.isFinite(cooldownUntil) && cooldownUntil > Date.now();
      const tone = !ready
        ? "failed"
        : coolingDown || failures
          ? "degraded"
          : "healthy";
      if (tone === "healthy") healthy += 1;
      appendText(
        stats,
        "span",
        !ready
          ? `Not ready · ${health.readiness_error || "prerequisite missing"}`
          : coolingDown
            ? `Cooling down · ${health.last_error_class || "provider-error"}`
            : failures
              ? `${failures} recent failure${failures === 1 ? "" : "s"}`
              : successes
                ? "Operational"
                : "Prerequisites present · unproven",
        tone
      );
      appendText(
        stats,
        "code",
        `${calls} calls · ${
          health.last_latency_ms != null ? `${health.last_latency_ms} ms` : "no latency"
        }${skipped ? ` · ${skipped} skipped` : ""}`
      );
      if (health.last_error || health.readiness_error) {
        row.title = health.last_error || health.readiness_error;
      }
      row.append(identity, stats);
      list.append(row);
    }
    if (!providers.length) {
      appendText(list, "div", "No model providers are configured.", "rail-empty");
    }
    elements["provider-summary"].textContent = providers.length
      ? `${healthy}/${providers.length} eligible now`
      : "No providers";
  }

  function openTaskDialog(mode) {
    const steering = mode === "steer";
    state.taskDialogMode = mode;
    elements["new-task-form"].reset();
    elements["auto-start-input"].checked = true;
    const copy = steering
      ? [
          "Live operator steering",
          "Guide this run",
          "Instruction",
          "Describe the correction or constraint. The harness will replan.",
          "Resume immediately",
          "Save this instruction and continue from a fresh plan.",
          "Apply guidance",
        ]
      : [
          "New supervised run",
          "What should the computer do?",
          "Outcome",
          "Describe the outcome, constraints, and visible completion evidence.",
          "Start planning immediately",
          "The machine opens first, then model events enter the timeline.",
          "Create task",
        ];
    [
      "task-dialog-eyebrow",
      "task-dialog-title",
      "task-input-label",
      "auto-start-title",
      "auto-start-help",
      "create-task-submit",
    ].forEach((id, index) => {
      elements[id].textContent = copy[index < 3 ? index : index + 1];
    });
    elements["task-input"].placeholder = copy[3];
    elements["task-input"].maxLength = steering ? 2000 : 20000;
    clearError(elements["task-error"]);
    elements["new-task-dialog"].showModal();
    window.setTimeout(() => elements["task-input"].focus(), 0);
  }

  function openNewTask() {
    if (state.controlMode !== "external_benchmark") openTaskDialog("create");
  }

  function openSteer() {
    if (state.selectedRun && !elements["steer-button"].hidden) {
      openTaskDialog("steer");
    }
  }

  async function createTask(event) {
    event.preventDefault();
    clearError(elements["task-error"]);
    const task = elements["task-input"].value.trim();
    if (!task) {
      showError(elements["task-error"], "Describe the outcome for this run.");
      return;
    }
    const steering = state.taskDialogMode === "steer";
    setBusy(
      elements["create-task-submit"],
      true,
      steering ? "Applying guidance…" : "Opening machine…"
    );
    try {
      const run = await apiJson(
        steering
          ? `/api/runs/${encodeURIComponent(state.selectedId)}/steer`
          : "/api/runs",
        {
          method: "POST",
          body: JSON.stringify(
            steering
              ? {
                  instruction: task,
                  auto_resume: elements["auto-start-input"].checked,
                }
              : {
                  task,
                  auto_start: elements["auto-start-input"].checked,
                }
          ),
        }
      );
      state.runs = [run, ...state.runs.filter((item) => item.run_id !== run.run_id)];
      elements["new-task-form"].reset();
      elements["auto-start-input"].checked = true;
      elements["new-task-dialog"].close();
      await selectRun(run.run_id);
      toast(
        steering
          ? "Guidance saved. The harness owns the fresh plan."
          : "Task opened. The control loop is now visible."
      );
    } catch (error) {
      showError(elements["task-error"], error.message);
    } finally {
      setBusy(elements["create-task-submit"], false);
    }
  }

  async function continueRun() {
    const runId = state.selectedId;
    if (!runId) return;
    setBusy(elements["continue-button"], true, "Continuing…");
    try {
      const run = await apiJson(
        `/api/runs/${encodeURIComponent(runId)}/continue`,
        { method: "POST" }
      );
      if (runId === state.selectedId) {
        state.selectedRun = run;
        renderSelectedRun();
      }
    } catch (error) {
      toast(`Could not continue: ${error.message}`, "error");
    } finally {
      setBusy(elements["continue-button"], false);
    }
  }

  async function pauseRun() {
    const runId = state.selectedId;
    if (!runId) return;
    setBusy(elements["pause-button"], true, "Pausing…");
    try {
      const run = await apiJson(`/api/runs/${encodeURIComponent(runId)}/pause`, {
        method: "POST",
        body: JSON.stringify({ reason: "paused from operator console" }),
      });
      if (runId === state.selectedId) {
        state.selectedRun = run;
        renderSelectedRun();
      }
      toast(
        state.selectedRun?.origin === "direct_mcp"
          ? "Direct MCP actions paused. Screen inspection remains available."
          : "Control loop paused. Its durable checkpoint was retained."
      );
    } catch (error) {
      toast(`Could not pause: ${error.message}`, "error");
    } finally {
      setBusy(elements["pause-button"], false);
    }
  }

  async function resolveApproval(decision) {
    const run = state.selectedRun;
    const request = run?.pending_approval;
    if (!run || !request?.approval_id) return;
    const approvalId = request.approval_id;
    const button =
      decision === "approve"
        ? elements["approve-button"]
        : decision === "reject"
          ? elements["reject-button"]
          : elements["takeover-button"];
    setBusy(button, true, decision === "approve" ? "Approving…" : "Stopping…");
    try {
      const updated = await apiJson(
        `/api/runs/${encodeURIComponent(
          run.run_id
        )}/approvals/${encodeURIComponent(approvalId)}`,
        {
          method: "POST",
          headers: { "X-PiKVM-Approval-Intent": approvalId },
          body: JSON.stringify({
            type: decision,
            reason: elements["approval-note"].value.trim(),
          }),
        }
      );
      if (run.run_id === state.selectedId) {
        state.selectedRun = updated;
        renderSelectedRun();
      }
      toast(
        decision === "approve"
          ? "Approved once. Freshness and policy will be checked again."
          : decision === "reject"
            ? "Action rejected. The audit trail was retained."
            : "Computer control stopped for operator take-over."
      );
    } catch (error) {
      toast(`Approval decision failed: ${error.message}`, "error");
    } finally {
      setBusy(button, false);
    }
  }

  function openAbortDialog() {
    if (!state.selectedRun || elements["abort-button"].disabled) return;
    clearError(elements["abort-error"]);
    elements["abort-dialog"].showModal();
    window.setTimeout(() => {
      elements["abort-reason"].focus();
      elements["abort-reason"].select();
    }, 0);
  }

  async function abortRun(event) {
    event.preventDefault();
    const runId = state.selectedId;
    if (!runId) return;
    clearError(elements["abort-error"]);
    const reason = elements["abort-reason"].value.trim();
    if (!reason) {
      showError(elements["abort-error"], "Record why this run is being stopped.");
      return;
    }
    setBusy(elements["abort-submit"], true, "Stopping…");
    try {
      const run = await apiJson(`/api/runs/${encodeURIComponent(runId)}/abort`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      elements["abort-dialog"].close();
      if (runId === state.selectedId) {
        state.selectedRun = run;
        renderSelectedRun();
      }
      toast("Run stopped. No pending action remains.");
    } catch (error) {
      showError(elements["abort-error"], error.message);
    } finally {
      setBusy(elements["abort-submit"], false);
    }
  }

  function toggleProviders() {
    const hidden = elements["provider-popover"].hidden;
    elements["provider-popover"].hidden = !hidden;
    elements["provider-button"].setAttribute("aria-expanded", String(hidden));
  }

  function toggleFilters() {
    const hidden = elements["timeline-filters"].hidden;
    elements["timeline-filters"].hidden = !hidden;
    elements["timeline-filter-button"].setAttribute(
      "aria-expanded",
      String(hidden)
    );
  }

  function bindEvents() {
    elements["auth-form"].addEventListener("submit", authenticate);
    elements["new-task-button"].addEventListener("click", openNewTask);
    elements["rail-new-task"].addEventListener("click", openNewTask);
    elements["new-task-form"].addEventListener("submit", createTask);
    elements["steer-button"].addEventListener("click", openSteer);
    elements["pause-button"].addEventListener("click", pauseRun);
    elements["continue-button"].addEventListener("click", continueRun);
    elements["refresh-frame"].addEventListener("click", () =>
      refreshFrame()
    );
    elements["fit-frame"].addEventListener("click", () => {
      elements["live-screen"].classList.toggle("actual-size");
      elements["fit-frame"].setAttribute(
        "aria-pressed",
        String(elements["live-screen"].classList.contains("actual-size"))
      );
    });
    elements["approve-button"].addEventListener("click", () =>
      resolveApproval("approve")
    );
    elements["reject-button"].addEventListener("click", () =>
      resolveApproval("reject")
    );
    elements["takeover-button"].addEventListener("click", () =>
      resolveApproval("take_over")
    );
    elements["abort-button"].addEventListener("click", openAbortDialog);
    elements["abort-form"].addEventListener("submit", abortRun);
    elements["provider-button"].addEventListener("click", toggleProviders);
    elements["close-providers"].addEventListener("click", toggleProviders);
    elements["timeline-filter-button"].addEventListener("click", toggleFilters);
    elements["timeline-filters"].addEventListener("change", (event) => {
      const checkbox = event.target;
      if (!(checkbox instanceof HTMLInputElement)) return;
      if (checkbox.checked) state.filters.add(checkbox.value);
      else state.filters.delete(checkbox.value);
      renderTimeline();
    });
    elements["session-button"].addEventListener("click", () =>
      elements["session-dialog"].showModal()
    );
    elements["disconnect-button"].addEventListener("click", disconnect);

    for (const button of document.querySelectorAll(".dialog-close")) {
      button.addEventListener("click", () => button.closest("dialog")?.close());
    }
    for (const dialog of document.querySelectorAll("dialog")) {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    }

    document.addEventListener("click", (event) => {
      if (
        !elements["provider-popover"].hidden &&
        !elements["provider-popover"].contains(event.target) &&
        !elements["provider-button"].contains(event.target)
      ) {
        elements["provider-popover"].hidden = true;
        elements["provider-button"].setAttribute("aria-expanded", "false");
      }
    });

    document.addEventListener("keydown", (event) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        event.shiftKey &&
        event.key === "."
      ) {
        event.preventDefault();
        openAbortDialog();
      }
      if (
        (event.ctrlKey || event.metaKey) &&
        event.key === "Enter" &&
        elements["new-task-dialog"].open
      ) {
        event.preventDefault();
        elements["new-task-form"].requestSubmit();
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && state.selectedId) {
        refreshFrame(
          state.selectedId,
          state.selectedRun?.observation?.frame_id
        );
      } else {
        stopFrameLoop();
      }
    });

    window.addEventListener("pagehide", () => {
      stopStreaming();
      stopFrameLoop();
      releaseFrame();
      releaseVerificationImage();
    });
  }

  async function boot() {
    bindEvents();
    renderRuns();
    renderNoSelection();
    if (!state.token) {
      closeConsole();
      return;
    }
    try {
      await connect();
    } catch {
      disconnect();
      showError(
        elements["auth-error"],
        "The saved tab session is no longer authorized. Paste a current harness token."
      );
    }
  }

  boot();
})();
