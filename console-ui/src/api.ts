// Typed client for the console JSON API. Same-origin, cookie-authenticated; the CSRF
// token (returned at login / from /api/session) rides on every mutation as a header.

let csrfToken = "";
export function setCsrf(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const method = (opts.method || "GET").toUpperCase();
  const headers: Record<string, string> = { ...(opts.headers as Record<string, string>) };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  }
  const res = await fetch(path, { ...opts, method, headers, credentials: "same-origin" });
  let body: any = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { output: text };
    }
  }
  if (!res.ok) {
    throw new ApiError(body?.error || `request failed (${res.status})`, res.status);
  }
  return body as T;
}

function post<T = any>(path: string, data?: unknown) {
  return request<T>(path, { method: "POST", body: data ? JSON.stringify(data) : undefined });
}

// --- shapes ---------------------------------------------------------------
export interface SessionInfo {
  authenticated: boolean;
  setup_done: boolean;
  bootstrap_active: boolean;
  user?: string;
  role?: "admin" | "bootstrap";
  csrf?: string;
  import_dir?: string | null;
}
export interface TokenRow {
  id: string;
  user: string;
  read: string[];
  explicit: string[];
  groups: string[];
  write: string[];
  expires_at: number | null;
  expired: boolean;
  created_at: number | null;
  created_by: string | null;
  comment: string | null;
}
export interface GroupRow {
  name: string;
  prefixes: string[];
  members: string[];
}
export interface VerbResult {
  ok: boolean;
  exit_code: number;
  output: string;
}
export interface JobRef {
  job_id: string;
  label: string;
}
export interface JobStatus {
  id: string;
  label: string;
  status: "running" | "done" | "failed";
  exit_code: number | null;
  created_at: number;
  dropped: number;
}

export const api = {
  session: () => request<SessionInfo>("/api/session"),
  login: (token: string) => post<SessionInfo>("/api/login", { token }),
  bootstrapLogin: (bootstrap: string) => post<SessionInfo>("/api/login", { bootstrap }),
  logout: () => post("/api/logout"),

  status: () => request<VerbResult>("/api/status"),
  doctor: () => request<VerbResult>("/api/doctor"),
  inventory: () => request<VerbResult>("/api/inventory"),
  config: () => request<{ settings: any; env: any[]; editable_keys: string[] }>("/api/config"),
  setConfig: (key: string, value: string) => post<VerbResult>("/api/config", { key, value }),
  connect: () => request<{ url: string; codex_cmd: string; has_token: boolean }>("/api/connect"),

  tokens: () => request<{ tokens: TokenRow[] }>("/api/tokens"),
  mintToken: (body: Record<string, unknown>) =>
    post<{ ok: boolean; token?: string; output: string }>("/api/tokens", body),
  revokeToken: (ref: string) => post<VerbResult>("/api/tokens/revoke", { ref }),
  rotateToken: (user: string) => post<VerbResult & { token?: string }>("/api/tokens/rotate", { user }),

  groups: () => request<{ groups: GroupRow[] }>("/api/groups"),
  defineGroup: (name: string, prefixes: string[]) => post<VerbResult>("/api/groups", { name, prefixes }),
  removeGroup: (name: string) => post<VerbResult>("/api/groups/remove", { name }),

  accessCheck: (user: string, path: string) =>
    request<{ result: string; scope?: string[] }>(
      `/api/access/check?user=${encodeURIComponent(user)}&path=${encodeURIComponent(path)}`,
    ),
  accessTree: () => request<{ groups: GroupRow[]; users: any[] }>("/api/access/tree"),
  audit: (n = 50) => request<{ tokens: any[]; console: any[] }>(`/api/audit?n=${n}`),

  scheduleShow: () => request<VerbResult>("/api/schedule"),
  scheduleSet: (spec: string) => post<VerbResult>("/api/schedule", { spec }),

  build: (target: string) => post<JobRef>("/api/lifecycle/build", { target }),
  ingest: (full: boolean) => post<JobRef>("/api/lifecycle/ingest", { full }),
  serve: () => post<JobRef>("/api/lifecycle/serve"),
  stop: () => post<JobRef>("/api/lifecycle/stop"),
  backup: () => post<JobRef>("/api/lifecycle/backup"),
  wizardApply: (body: Record<string, unknown>) => post<JobRef>("/api/wizard/apply", body),

  job: (id: string) => request<JobStatus>(`/api/jobs/${id}`),
  jobLog: (id: string, after = 0) =>
    request<{ cursor: number; lines: string[]; status: string; exit_code: number | null }>(
      `/api/jobs/${id}/log?after=${after}`,
    ),
};
