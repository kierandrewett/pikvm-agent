const TOKEN_KEY = "pikvm-harness-token";
const SELECTED_RUN_KEY = "pikvm-harness-selected-run";
const PENDING_CREATE_KEY = "pikvm-harness-pending-create";

export class HarnessApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "HarnessApiError";
    this.status = status;
  }
}

const browserSessionStorage = (): Storage | undefined => {
  try {
    return typeof sessionStorage === "undefined" ? undefined : sessionStorage;
  } catch {
    return undefined;
  }
};

export const readStoredToken = () => {
  try {
    return browserSessionStorage()?.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
};

export const storeToken = (token: string) => {
  try {
    browserSessionStorage()?.setItem(TOKEN_KEY, token);
  } catch {
    // Storage is a convenience only; the active React state still owns access.
  }
};

export const clearStoredToken = () => {
  try {
    browserSessionStorage()?.removeItem(TOKEN_KEY);
  } catch {
    // A blocked storage backend must not prevent an explicit disconnect.
  }
};

export const readStoredRunId = () => {
  try {
    return browserSessionStorage()?.getItem(SELECTED_RUN_KEY) ?? "";
  } catch {
    return "";
  }
};

export const storeRunId = (runId: string) => {
  try {
    browserSessionStorage()?.setItem(SELECTED_RUN_KEY, runId);
  } catch {
    // Session selection is best-effort; active React state still owns it.
  }
};

export const clearStoredRunId = () => {
  try {
    browserSessionStorage()?.removeItem(SELECTED_RUN_KEY);
  } catch {
    // A blocked storage backend must not prevent explicit navigation.
  }
};

type PendingCreate = {
  task: string;
  requestId: string;
};

const newRequestId = () => {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch {
    // Fall through to a browser-compatible opaque identifier.
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
};

export const pendingCreateRequestId = (task: string) => {
  const storage = browserSessionStorage();
  try {
    const stored = storage?.getItem(PENDING_CREATE_KEY);
    if (stored) {
      const pending = JSON.parse(stored) as Partial<PendingCreate>;
      if (
        pending.task === task &&
        typeof pending.requestId === "string" &&
        pending.requestId
      ) {
        return pending.requestId;
      }
    }
  } catch {
    // Replace malformed or unavailable state with a fresh request identity.
  }
  const requestId = newRequestId();
  try {
    storage?.setItem(
      PENDING_CREATE_KEY,
      JSON.stringify({ task, requestId } satisfies PendingCreate),
    );
  } catch {
    // Server-side idempotency still applies for this in-memory request.
  }
  return requestId;
};

export const clearPendingCreate = (requestId: string) => {
  const storage = browserSessionStorage();
  try {
    const stored = storage?.getItem(PENDING_CREATE_KEY);
    if (!stored) return;
    const pending = JSON.parse(stored) as Partial<PendingCreate>;
    if (pending.requestId === requestId) {
      storage?.removeItem(PENDING_CREATE_KEY);
    }
  } catch {
    // A malformed best-effort replay marker can be ignored after success.
  }
};

const responseMessage = async (response: Response) => {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") return payload.detail;
  } catch {
    // The generic status below remains safe and useful.
  }
  return `${response.status} ${response.statusText}`.trim();
};

export async function harnessJson<T>(
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new HarnessApiError(response.status, await responseMessage(response));
  }
  return (await response.json()) as T;
}

export async function harnessBlob(
  token: string,
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new HarnessApiError(response.status, await responseMessage(response));
  }
  return response.blob();
}

export type HarnessStreamMessage = {
  event: string;
  id?: string;
  data: unknown;
};

type HarnessEventStreamOptions = {
  signal?: AbortSignal;
  onMessage: (message: HarnessStreamMessage) => void;
};

const decodeSseMessage = (block: string): HarnessStreamMessage | null => {
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value || "message";
    if (field === "id") id = value;
    if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  const body = data.join("\n");
  let payload: unknown = body;
  try {
    payload = JSON.parse(body);
  } catch {
    // A non-JSON diagnostic remains visible to the caller without breaking
    // the authenticated stream.
  }
  return { event, ...(id == null ? {} : { id }), data: payload };
};

export async function harnessEventStream(
  token: string,
  path: string,
  options: HarnessEventStreamOptions,
): Promise<void> {
  const headers = new Headers();
  headers.set("Authorization", `Bearer ${token}`);
  headers.set("Accept", "text/event-stream");
  const response = await fetch(path, {
    headers,
    cache: "no-store",
    signal: options.signal,
  });
  if (!response.ok) {
    throw new HarnessApiError(response.status, await responseMessage(response));
  }
  if (!response.body) {
    throw new HarnessApiError(502, "Live update stream has no response body.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (block: string) => {
    const message = decodeSseMessage(block);
    if (message) options.onMessage(message);
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    buffer = buffer.replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) dispatch(buffer);
}
