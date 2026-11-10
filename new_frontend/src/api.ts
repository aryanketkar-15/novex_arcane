// Typed client for the ARCANE FastAPI backend (proxied through Vite at /api).

export type Screen =
  | "home"
  | "repair"
  | "logs"
  | "conflict"
  | "pr"
  | "people"
  | "history"
  | "settings"
  | "components";

export type RunStatus = "working" | "ready" | "person" | "failed";

export interface Step {
  key: string;
  title: string;
  body: string;
  evidence: string;
  status: "pending" | "running" | "done" | "failed";
}

export interface LogLine {
  t: string;
  level: "INFO" | "WARN" | "ERROR";
  msg: string;
}

export interface Run {
  id: string;
  repo: string;
  branch: string;
  sha: string;
  commitMessage: string;
  author: string;
  source: string;
  hint: string;
  workflowRun: string;
  status: RunStatus;
  stage: string;
  title: string;
  failingTest: string;
  failingFile: string;
  failingLine: number;
  suspectedFunction: string;
  rootCause: string;
  bisectReport: string;
  cascadeReport: string;
  confidence: number | null;
  testsPassed: boolean | null;
  autoApproved: boolean;
  model: string;
  attempts: number;
  prUrl: string | null;
  error: string | null;
  memoryHit: boolean;
  conflict: { detected: boolean; action: string; score: number; details: unknown[] } | null;
  conflictResolution?: { text: string; by: string; at: string };
  validatorSummary: string;
  executionMode?: "local" | "hosted" | "auto" | string;
  validationStatus?: "PASSED" | "FAILED" | "SKIPPED" | "ERROR" | string;
  testVerification?: "VERIFIED" | "NOT_VERIFIED" | string;
  isDraftPr?: boolean;
  agentsUsed: string[];
  steps: Step[];
  decision: "none" | "accepted" | "rejected";
  rejectReason: string;
  assignee: string;
  reason: string;
  startedAt: string;
  finishedAt: string | null;
  diffSummary: { files: number; add: number; del: number };
  logCount: number;
  // detail only
  logs?: LogLine[];
  failureLog?: string;
  patchDiff?: string;
  diffFiles?: { path: string; add: number; del: number }[];
  regressionTest?: string;
}

export interface Overview {
  pipeline: {
    current_state: string;
    agents: Record<string, string>;
    current_run_id: string | null;
    error: string | null;
  };
  counts: { all: number; ready: number; working: number; person: number; failed: number };
  week: { prepared: number; merged: number; sentBack: number };
  avgSeconds: number | null;
}

export interface RepoSetting {
  name: string;
  testCommand: string;
  language: string;
  timeoutMin: number;
  autoPr: boolean;
}

export interface Settings {
  workspace: string;
  repositories: RepoSetting[];
  models: { id: string; name: string; role: string }[];
  modelStatus: Record<string, { connected: boolean; detail: string }>;
  safety: {
    minConfidence: number;
    escalateBelow: number;
    protectedBranches: string[];
    protectedFiles: string;
    network: string;
    cpu: number;
    memoryGb: number;
    runtimeMin: number;
  };
  team: { name: string; email: string; role: string; lead?: string }[];
  notifications: {
    events: Record<string, { on: boolean; email: boolean; slack: boolean }>;
    email: string;
    slack: string;
  };
  integrations: { githubPat: boolean; webhookSecret: boolean; webhookPath: string };
}

export interface MemoryItem {
  id: string;
  key: string;
  title: string;
  repo: string;
  language: string;
  similarity: number | null;
  reuses: number;
  commit: string;
  date: string;
  patch: string;
  testFile: string;
  reuse: boolean;
}

export interface AuditRow {
  t: string;
  action: string;
  scope: string;
  actor: string;
}

export class ApiError extends Error {
  status = 0;
}

export interface Me {
  login: string;
  name: string;
  email: string;
  avatar: string;
  workspace: string;
  oauth: boolean;
}

export interface GhRepo {
  name: string;
  private: boolean;
  defaultBranch: string;
  watched?: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError("Cannot reach the ARCANE backend. Is `uvicorn api.main:app` running on port 8000?");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* ignore */
    }
    const err = new ApiError(detail);
    err.status = res.status;
    throw err;
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

function normalizeRun(run: Run): Run {
  return { ...run, steps: Array.isArray(run.steps) ? run.steps : [] };
}

type RepoEntry = GhRepo & { fullName?: string };

async function githubRepos() {
  const response = await request<RepoEntry[] | { repos?: RepoEntry[] } | null>("/github/repos");
  const rows = Array.isArray(response) ? response : response?.repos;
  return {
    repos: Array.isArray(rows)
      ? rows.map((repo) => ({ ...repo, name: repo.fullName || repo.name }))
      : [],
  };
}

export type ModelCredential = { id: number; provider: "google" | "openai" | "groq"; model: string };

export const api = {
  modelKeys: () => request<ModelCredential[]>("/settings/model-keys"),
  addModelKey: (provider: ModelCredential["provider"], model: string, key: string) =>
    post("/settings/model-keys", { provider, model, key }),
  removeModelKey: (id: number) => request(`/settings/model-keys/${id}`, { method: "DELETE" }),
  overview: () => request<Overview>("/overview"),
  runs: async (q = "") => {
    const { runs } = await request<{ runs: Run[] }>(`/runs${q}`);
    return { runs: runs.map(normalizeRun) };
  },
  run: (id: string) => request<Run>(`/runs/${id}`).then(normalizeRun),
  createRun: (b: { repo: string; branch: string; workflowRun: string; context: string }) =>
    post<Run>("/runs", b).then(normalizeRun),
  retry: (id: string, b: { hint: string; files: string[]; model: string }) =>
    post<Run>(`/runs/${id}/retry`, b),
  openPr: (id: string, b: { title: string; comment: string }) =>
    post<{ prUrl: string; run: Run }>(`/runs/${id}/open-pr`, b),
  decide: (id: string, b: { action: string; reason?: string; note?: string; assignee?: string }) =>
    post<Run>(`/runs/${id}/decision`, b),
  resolveConflict: (id: string, text: string) =>
    post<{ ok: boolean }>(`/runs/${id}/conflict`, { action: "resolve", note: text }),
  memory: (q = "") =>
    request<{ items: MemoryItem[]; total: number; available: boolean; error?: string }>(
      `/memory?q=${encodeURIComponent(q)}`,
    ),
  setMemoryReuse: (key: string, reuse: boolean) =>
    request<{ ok: boolean }>(`/memory/${encodeURIComponent(key)}`, {
      method: "PATCH",
      body: JSON.stringify({ reuse }),
    }),
  settings: () => request<Settings>("/settings"),
  saveSettings: (s: Partial<Settings>) =>
    request<Settings>("/settings", { method: "PUT", body: JSON.stringify(s) }),
  testModel: (id: string) =>
    post<{ connected: boolean; detail: string }>(`/settings/models/${id}/test`),
  audit: (q = "") => request<{ rows: AuditRow[]; total: number }>(`/audit?q=${encodeURIComponent(q)}`),
  me: () => request<Me>("/me"),
  authConfig: () => request<{ oauth: boolean }>("/auth/config"),
  logout: () => post<{ ok: boolean }>("/auth/logout"),
  ghRepos: githubRepos,
  watch: (repo: string) => post<{ ok: boolean; message: string }>("/github/watch", { repo, webhook: true }),
  unwatch: (repo: string) =>
    request<{ ok: boolean }>(`/github/watch?repo=${encodeURIComponent(repo)}`, { method: "DELETE" }),
};

// ── formatting helpers ──────────────────────────────────────────────────────

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function clock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

export function duration(from: string, to: string | null): string {
  const s = Math.max(0, Math.round(((to ? new Date(to).getTime() : Date.now()) - new Date(from).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function shortRepo(full: string): string {
  return full.includes("/") ? full.split("/")[1] : full;
}

export function confidenceLabel(c: number | null): string {
  if (c == null) return "—";
  const word = c >= 95 ? "Very high" : c >= 85 ? "High" : c >= 65 ? "Medium" : "Low";
  return `${word} · ${c}%`;
}

export const statusLabel: Record<RunStatus, string> = {
  working: "Working",
  ready: "Ready",
  person: "Needs a person",
  failed: "Failed",
};
