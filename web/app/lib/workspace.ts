// Milestone 7 workspace client: multi-project backend API (prototype/workspace_server.py)
// + pure view helpers. The backend is authoritative; static fixtures are legacy only.

import { AGENT_API_BASE } from "./agent";

export const WORKSPACE_API_BASE = AGENT_API_BASE;

export interface WorkspaceEnvelope<T> {
  ok: boolean;
  data?: T;
  error?: string;
  details?: unknown;
}

export interface ProjectSummary {
  project_id: string;
  title: string;
  research_question: string;
  question_version: number;
  n_papers: number;
  n_findings: number;
  n_connections: number;
  n_verifications: number;
  n_audits: number;
  created_at: string;
  updated_at: string;
}

export interface QuestionVersionRecord {
  version: number;
  question: string;
  created_at: string;
  status: "current" | "superseded";
  findings: unknown[];
  connections: unknown[];
  open_questions: unknown[];
  novelty_risks: unknown[];
}

export interface LivePaperEntry {
  paper_id: string;
  title: string;
  filename: string;
  added_order: number;
  n_objects: number;
  n_localized: number;
  sha256: string;
  question_version: number;
}

export interface PaperStatus {
  status:
    | "ready"
    | "processing"
    | "awaiting_review"
    | "failed"
    | "cancelled"
    | "incomplete";
  job_id: string | null;
  stage?: string;
  stage_detail?: string;
  last_error?: string;
  relevance_stale?: boolean;
  result?: Record<string, unknown>;
}

export type JobState = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";

export interface WorkspaceJob {
  job_id: string;
  project_id: string;
  paper_id: string;
  job_type: string;
  state: JobState;
  stage: string;
  stage_detail: string;
  attempts: number;
  max_attempts: number;
  last_error: string;
  result: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface QuestionPreview {
  project_id: string;
  from_version: number;
  to_version: number;
  papers_to_rescore: string[];
  n_papers: number;
  conclusions_to_archive: {
    findings: number;
    connections: number;
    open_questions: number;
    novelty_risks: number;
  };
  preserved: string[];
  recomputed: string[];
}

/** Researcher-facing labels for real backend pipeline stages (never percentages). */
export const STAGE_LABELS: Record<string, string> = {
  uploading: "Uploading PDF",
  validating: "Validating PDF",
  reading_document: "Reading document",
  identifying_objects: "Identifying scientific objects",
  analyzing_content: "Analyzing scientific content",
  ranking_relevance: "Ranking relevance",
  localizing: "Localizing equations and figures",
  comparing_with_project: "Comparing with project knowledge",
  rebuilding_findings: "Rebuilding project findings",
  preparing: "Preparing",
  finalizing: "Finalizing project findings",
  awaiting_review: "Waiting on researcher review",
  ready: "Ready",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replace(/_/g, " ");
}

export function isJobActive(job: WorkspaceJob): boolean {
  return job.state === "QUEUED" || job.state === "RUNNING";
}

export class WorkspaceError extends Error {
  status: number;
  details: unknown;
  constructor(message: string, status: number, details: unknown = null) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const r = await fetch(`${WORKSPACE_API_BASE}${path}`, init);
  const body = (await r.json().catch(() => ({}))) as WorkspaceEnvelope<T>;
  if (!r.ok || body.ok === false) {
    throw new WorkspaceError(body.error || r.statusText, r.status, body.details ?? null);
  }
  return (body.data ?? {}) as T;
}

export async function probeWorkspaceApi(timeoutMs = 1500): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${WORKSPACE_API_BASE}/api/health`, {
      signal: ctrl.signal,
    });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export async function listProjects(): Promise<ProjectSummary[]> {
  const data = await request<{ projects: ProjectSummary[] }>("/api/projects");
  return data.projects ?? [];
}

export async function createProject(
  title: string,
  researchQuestion: string
): Promise<ProjectSummary & { project_id: string }> {
  return request("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, research_question: researchQuestion }),
  });
}

export interface RebuildStatus {
  needed: boolean;
  reason: string;
  papers: string[];
}

export interface ProjectView {
  project: Record<string, unknown> & {
    research_question: string;
    question_version: number;
    question_history: QuestionVersionRecord[];
  };
  paper_status: Record<string, PaperStatus>;
  rebuild?: RebuildStatus;
}

export async function fetchProjectView(projectId: string): Promise<ProjectView> {
  return request(`/api/projects/${projectId}`);
}

export async function updateProjectTitle(
  projectId: string,
  title: string
): Promise<ProjectSummary> {
  return request(`/api/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function uploadPaper(
  projectId: string,
  file: File
): Promise<{ job: WorkspaceJob; paper_id: string; deduped?: boolean }> {
  const form = new FormData();
  form.append("file", file, file.name);
  const r = await fetch(`${WORKSPACE_API_BASE}/api/projects/${projectId}/papers`, {
    method: "POST",
    body: form,
  });
  const body = (await r.json().catch(() => ({}))) as WorkspaceEnvelope<{
    job: WorkspaceJob;
    paper_id: string;
    deduped?: boolean;
  }>;
  if (!r.ok || body.ok === false) {
    throw new WorkspaceError(body.error || r.statusText, r.status, body.details ?? null);
  }
  return body.data as { job: WorkspaceJob; paper_id: string };
}

export async function fetchJob(jobId: string): Promise<WorkspaceJob> {
  const data = await request<{ job: WorkspaceJob }>(`/api/jobs/${jobId}`);
  return data.job;
}

export async function fetchProjectJobs(projectId: string): Promise<WorkspaceJob[]> {
  const data = await request<{ jobs: WorkspaceJob[] }>(
    `/api/projects/${projectId}/jobs`
  );
  return data.jobs ?? [];
}

export async function retryJob(jobId: string): Promise<WorkspaceJob> {
  const data = await request<{ job: WorkspaceJob }>(`/api/jobs/${jobId}/retry`, {
    method: "POST",
  });
  return data.job;
}

export async function requestRebuild(
  projectId: string
): Promise<{ job: WorkspaceJob; rebuild: RebuildStatus }> {
  return request(`/api/projects/${projectId}/rebuild`, { method: "POST" });
}

export async function fetchQuestionPreview(projectId: string): Promise<QuestionPreview> {
  return request(`/api/projects/${projectId}/question/preview`);
}

export async function changeQuestion(
  projectId: string,
  researchQuestion: string,
  confirm: boolean
): Promise<
  | { needs_confirmation: true; preview: QuestionPreview }
  | { project: unknown; preview: QuestionPreview; job: WorkspaceJob }
> {
  return request(`/api/projects/${projectId}/question`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ research_question: researchQuestion, confirm }),
  });
}

/** Live Reader URLs for a workspace paper (backend is authoritative). */
export function paperFileUrl(projectId: string, paperId: string): string {
  return `${WORKSPACE_API_BASE}/api/projects/${projectId}/papers/${paperId}/file`;
}

export function paperDataUrl(projectId: string, paperId: string): string {
  return `${WORKSPACE_API_BASE}/api/projects/${projectId}/papers/${paperId}/data`;
}

export function runDetailUrl(projectId: string, threadId: string): string {
  return `${WORKSPACE_API_BASE}/api/projects/${projectId}/runs/${threadId}`;
}

export function reviewUrl(projectId: string, threadId: string): string {
  return `${WORKSPACE_API_BASE}/api/projects/${projectId}/runs/${threadId}/review`;
}

/**
 * Deduplicate question history by version. Projects written before the
 * one-record-per-version fix may hold two v1 records (the empty initial one
 * plus the archived snapshot); the snapshot (most findings) wins so React
 * keys stay unique and the display shows each version once.
 */
export function dedupeQuestionHistory(
  history: QuestionVersionRecord[]
): QuestionVersionRecord[] {
  const byVersion = new Map<number, QuestionVersionRecord>();
  for (const h of history) {
    const prev = byVersion.get(h.version);
    if (!prev || h.findings.length > prev.findings.length) {
      byVersion.set(h.version, h);
    }
  }
  return [...byVersion.values()].sort((a, b) => a.version - b.version);
}

export async function deletePaper(projectId: string, paperId: string): Promise<void> {
  let r: Response;
  try {
    r = await fetch(
      `${WORKSPACE_API_BASE}/api/projects/${projectId}/papers/${paperId}`,
      { method: "DELETE" }
    );
  } catch {
    // Network-level failure (the usual cause: a backend started before this
    // endpoint existed, whose CORS preflight rejects DELETE).
    throw new WorkspaceError(
      "Could not reach the backend. If it was started before this update, restart it: stop the process on port 8765 and re-run ./dev.sh.",
      0
    );
  }
  const body = (await r.json().catch(() => ({}))) as WorkspaceEnvelope<unknown>;
  if (!r.ok || body.ok === false) {
    throw new WorkspaceError(body.error || r.statusText, r.status, body.details ?? null);
  }
}

export function formatBytes(n: number): string {
  if (!isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

export function formatTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}
