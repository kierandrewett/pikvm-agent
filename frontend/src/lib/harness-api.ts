const TOKEN_KEY = "pikvm-harness-token";

export class HarnessApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "HarnessApiError";
    this.status = status;
  }
}

export const readStoredToken = () => sessionStorage.getItem(TOKEN_KEY) ?? "";

export const storeToken = (token: string) => {
  sessionStorage.setItem(TOKEN_KEY, token);
};

export const clearStoredToken = () => {
  sessionStorage.removeItem(TOKEN_KEY);
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
