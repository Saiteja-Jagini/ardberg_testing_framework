export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type PullRequest = { number: number; title: string; head_sha: string; url: string };
export type ResolvedRepo = { repository: string; installation_id: number; pull_requests: PullRequest[] };
export type Run = {
  id: string; repository: string; pr_number: number; title: string;
  head_sha: string; base_sha: string; instruction: string;
  selected_frameworks: string[]; status: string; stage: string;
  context: Record<string, unknown>; error: string | null; report: string | null;
  created_at: string; updated_at: string;
};
export type RunSummary = Pick<Run, "id" | "repository" | "pr_number" | "title" | "head_sha" | "status" | "stage" | "created_at">;
export type RunEvent = {
  id: number; stage: string; agent: string | null; node: string | null;
  status: string; success: boolean | null; detail: Record<string, unknown>;
  created_at: string;
};
export type FlowNode = {
  id: string; label: string; x: number; y: number; group: string;
  description: string; status: string; event: RunEvent | null;
};
export type FlowEdge = { id: string; source: string; target: string; label: string };
export type FlowDefinition = {
  nodes: FlowNode[]; edges: FlowEdge[];
  run: { id: string; status: string; stage: string; instruction: string; context: Record<string, unknown> } | null;
};
export type RepoPreview = {
  analysis: { application_framework: string; native_test_framework: string; language: string; explanation: string };
  needs_framework_selection: boolean;
};
export type Connections = {
  github: { connected: boolean; slug?: string; error: string };
  model: { connected: boolean; model: string; error: string };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message
      ? detail.message + (detail.install_url ? " " + detail.install_url : "")
      : JSON.stringify(detail));
  }
  return result as T;
}

export const api = {
  health: () => request<{ status: string; github_configured: boolean; model_configured: boolean }>("/health"),
  connections: () => request<Connections>("/api/connections"),
  resolve: (url: string) => request<ResolvedRepo>("/api/resolve", {
    method: "POST", body: JSON.stringify({ url }),
  }),
  preview: (repository: string, pr_number: number) => request<RepoPreview>("/api/preview", {
    method: "POST", body: JSON.stringify({ repository, pr_number }),
  }),
  createRun: (input: { repository: string; pr_number: number; instruction: string; selected_frameworks: string[] }) =>
    request<{ run_id: string }>("/api/runs", { method: "POST", body: JSON.stringify(input) }),
  runs: () => request<RunSummary[]>("/api/runs"),
  run: (id: string) => request<Run>(`/api/runs/${id}`),
  events: (id: string) => request<RunEvent[]>(`/api/runs/${id}/events`),
  flow: (id?: string) => request<FlowDefinition>(id ? `/api/runs/${id}/flow` : "/api/flow"),
};
