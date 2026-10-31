// Single fetch wrapper (plan §4.1). Every request carries the per-session
// bearer token. In dev the token comes from VITE_API_TOKEN (.env.development);
// in production (Phase 3) pywebview injects window.__ARCH_TOKEN__ before load.

declare global {
  interface Window {
    __ARCH_TOKEN__?: string;
  }
}

export function getToken(): string {
  return window.__ARCH_TOKEN__ || import.meta.env.VITE_API_TOKEN || "";
}

export class ApiError extends Error {
  status: number;
  detail: string;
  // The whole detail when the worker sent structured data, such as clean_lock.
  body: unknown;
  constructor(status: number, detail: string, body?: unknown) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  raw = false,
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${getToken()}`,
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let detail = res.statusText;
    let body: unknown;
    try {
      const j = await res.json();
      body = j.detail;
      detail = (typeof body === "object" ? (body as { message?: string })?.message : body as string) ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, String(detail), body);
  }
  if (res.status === 204) return undefined as T;
  if (raw) return (await res.text()) as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  text: (path: string) => request<string>("GET", path, undefined, true),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

// EventSource for SSE — the token rides as a query param (EventSource can't set
// headers); the worker accepts ?token= for /api/events.
export function eventsUrl(): string {
  return `/api/events?token=${encodeURIComponent(getToken())}`;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Poll a started job until it terminates; throw on failure/cancel. Returns the
// job's result payload.
export async function waitForJob(
  jobId: string,
  failMsg: string,
): Promise<Record<string, unknown> | undefined> {
  for (;;) {
    await sleep(250);
    const j = await api.get<{ status: string; error?: string; result?: Record<string, unknown> }>(
      `/jobs/${jobId}`,
    );
    if (j.status === "done") return j.result;
    if (j.status === "failed" || j.status === "cancelled")
      throw new Error(j.error || failMsg);
  }
}
