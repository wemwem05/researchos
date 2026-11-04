// Milestone 8 discovery client: autonomous literature discovery API
// (prototype/workspace_server.py discovery routes) + pure view helpers.

import {
  WORKSPACE_API_BASE,
  WorkspaceError,
  type WorkspaceEnvelope,
  type WorkspaceJob,
} from "./workspace";

export type RelevanceVerdict =
  | "highly_relevant"
  | "relevant"
  | "tangential"
  | "off_topic";

export type EvidenceAccessLevel =
  | "metadata_only"
  | "title_and_abstract"
  | "full_text_available"
  | "full_text_analyzed";

export type CandidateStatus =
  | "pending"
  | "approved"
  | "dismissed"
  | "importing"
  | "imported"
  | "import_failed"
  | "full_text_unavailable";

export interface CandidateEvaluation {
  verdict: RelevanceVerdict;
  why_relevant: string;
  may_contribute: string;
  may_overlap_with: string;
  still_unknown: string;
  why_read_next: string;
  novelty_overlap: boolean;
  novelty_note: string;
}

export interface DiscoveryCandidate {
  candidate_id: string;
  title: string;
  authors: string[];
  publication_year: number | null;
  venue: string;
  work_type: string;
  doi: string;
  openalex_id: string;
  arxiv_id: string;
  abstract: string;
  landing_page_url: string;
  open_access_url: string;
  pdf_url: string;
  license: string;
  citation_count: number | null;
  source_provider: string;
  retrieval_query: string;
  retrieval_round: number;
  research_question_version: number;
  evidence_access_level: EvidenceAccessLevel;
  evaluation: CandidateEvaluation;
  recommended: boolean;
  status: CandidateStatus;
  status_reason: string;
  already_in_project: boolean;
  project_paper_id: string;
}

export interface DiscoveryQueryRecord {
  query: string;
  round: number;
  strategy: string;
  rationale: string;
  provider: string;
  n_records: number;
  error: string;
}

export interface DiscoveryRun {
  run_id: string;
  thread_id: string;
  project_id: string;
  research_question: string;
  research_question_version: number;
  focus: string;
  status: "running" | "completed" | "failed";
  stale: boolean;
  created_at: string;
  updated_at: string;
  budget: Record<string, number>;
  strategy_notes: string;
  queries: DiscoveryQueryRecord[];
  counts: Record<string, number>;
  reflection_decision: string;
  reflection_explanation: string;
  critic_verdict: string;
  critic_notes: string;
  revision_used: boolean;
  limitations: string;
  error: string;
  candidates: DiscoveryCandidate[];
  usage: Record<string, number>;
}

export interface DiscoveryList {
  discoveries: DiscoveryRun[];
  active_job: WorkspaceJob | null;
  question_version: number;
}

export interface DecisionItem {
  ok: boolean;
  candidate_id?: string;
  status?: string;
  error?: string;
}

export interface ImportItem {
  candidate_id: string;
  ok: boolean;
  paper_id?: string;
  note?: string;
  error?: string;
  retryable?: boolean;
  unavailable?: boolean;
  duplicate_of?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${WORKSPACE_API_BASE}${path}`, init);
  const body = (await r.json().catch(() => ({}))) as WorkspaceEnvelope<T>;
  if (!r.ok || body.ok === false) {
    throw new WorkspaceError(
      body.error || r.statusText,
      r.status,
      body.details ?? null
    );
  }
  return (body.data ?? {}) as T;
}

export async function listDiscoveries(
  projectId: string
): Promise<DiscoveryList> {
  return request(`/api/projects/${projectId}/discoveries`);
}

export interface DiscoveryDetail {
  run: DiscoveryRun;
  retrieval_log: {
    queries_done: DiscoveryQueryRecord[];
    duplicates_log: Record<string, unknown>[];
    usage: Record<string, number>;
  } | null;
  record: {
    status: string;
    decisions: string[];
    telemetry: Record<string, unknown>[];
  } | null;
}

export async function fetchDiscovery(
  projectId: string,
  runId: string
): Promise<DiscoveryDetail> {
  return request(`/api/projects/${projectId}/discoveries/${runId}`);
}

export async function startDiscovery(
  projectId: string,
  focus: string,
  requestKey: string
): Promise<{ job: WorkspaceJob }> {
  return request(`/api/projects/${projectId}/discoveries/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ focus, request_key: requestKey }),
  });
}

export async function decideCandidates(
  projectId: string,
  runId: string,
  decisions: { candidate_id: string; action: "approve" | "dismiss" }[]
): Promise<{ run_id: string; items: DecisionItem[] }> {
  return request(`/api/projects/${projectId}/discoveries/${runId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions }),
  });
}

export async function importCandidates(
  projectId: string,
  runId: string,
  candidateIds: string[]
): Promise<{ job: WorkspaceJob }> {
  return request(`/api/projects/${projectId}/discoveries/${runId}/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ candidate_ids: candidateIds }),
  });
}

// -- pure view helpers ----------------------------------------------------

export type DiscoveryFilter =
  | "ALL"
  | "HIGHLY_RELEVANT"
  | "NOVELTY_OVERLAP"
  | "OPEN_ACCESS"
  | "ALREADY_IN_PROJECT";

export const DISCOVERY_FILTERS: DiscoveryFilter[] = [
  "ALL",
  "HIGHLY_RELEVANT",
  "NOVELTY_OVERLAP",
  "OPEN_ACCESS",
  "ALREADY_IN_PROJECT",
];

export function filterLabel(f: DiscoveryFilter): string {
  switch (f) {
    case "ALL":
      return "All";
    case "HIGHLY_RELEVANT":
      return "Highly relevant";
    case "NOVELTY_OVERLAP":
      return "Possible novelty overlap";
    case "OPEN_ACCESS":
      return "Open access";
    case "ALREADY_IN_PROJECT":
      return "Already in project";
  }
}

export function isOpenAccess(c: DiscoveryCandidate): boolean {
  return Boolean(c.pdf_url || c.open_access_url);
}

export function filterCandidates(
  candidates: DiscoveryCandidate[],
  filter: DiscoveryFilter
): DiscoveryCandidate[] {
  switch (filter) {
    case "ALL":
      return candidates;
    case "HIGHLY_RELEVANT":
      return candidates.filter(
        (c) => c.evaluation.verdict === "highly_relevant"
      );
    case "NOVELTY_OVERLAP":
      return candidates.filter((c) => c.evaluation.novelty_overlap);
    case "OPEN_ACCESS":
      return candidates.filter(isOpenAccess);
    case "ALREADY_IN_PROJECT":
      return candidates.filter((c) => c.already_in_project);
  }
}

/** Recommended first, then verdict rank, stable by candidate id. */
const VERDICT_RANK: Record<RelevanceVerdict, number> = {
  highly_relevant: 0,
  relevant: 1,
  tangential: 2,
  off_topic: 3,
};

export function sortCandidates(
  candidates: DiscoveryCandidate[]
): DiscoveryCandidate[] {
  return [...candidates].sort((a, b) => {
    if (a.recommended !== b.recommended) return a.recommended ? -1 : 1;
    const r =
      VERDICT_RANK[a.evaluation.verdict] - VERDICT_RANK[b.evaluation.verdict];
    if (r !== 0) return r;
    return a.candidate_id.localeCompare(b.candidate_id);
  });
}

export function evidenceLabel(level: EvidenceAccessLevel): string {
  switch (level) {
    case "full_text_analyzed":
      return "Full text analyzed";
    case "full_text_available":
      return "Full text available";
    case "title_and_abstract":
      return "Title + abstract";
    case "metadata_only":
      return "Metadata only";
  }
}

export function verdictLabel(v: RelevanceVerdict): string {
  switch (v) {
    case "highly_relevant":
      return "Highly relevant";
    case "relevant":
      return "Relevant";
    case "tangential":
      return "Tangential";
    case "off_topic":
      return "Off topic";
  }
}

export function statusLabel(s: CandidateStatus): string {
  switch (s) {
    case "pending":
      return "Awaiting review";
    case "approved":
      return "Approved";
    case "dismissed":
      return "Dismissed";
    case "importing":
      return "Importing…";
    case "imported":
      return "Imported";
    case "import_failed":
      return "Import failed";
    case "full_text_unavailable":
      return "Full text unavailable";
  }
}

/** Best outward link: landing page, DOI, OpenAlex id, arXiv, in that order. */
export function candidateSourceUrl(c: DiscoveryCandidate): string {
  if (c.landing_page_url) return c.landing_page_url;
  if (c.doi) {
    return c.doi.startsWith("http") ? c.doi : `https://doi.org/${c.doi}`;
  }
  if (c.openalex_id) return c.openalex_id;
  if (c.arxiv_id) {
    const bare = c.arxiv_id.replace(/^arxiv:/i, "");
    return `https://arxiv.org/abs/${bare}`;
  }
  return "";
}

export function isRunStale(run: DiscoveryRun, currentQv: number): boolean {
  return run.stale || run.research_question_version !== currentQv;
}

/** Honest venue label: never present a preprint as peer-reviewed. */
export function venueLabel(c: DiscoveryCandidate): string {
  const workType = (c.work_type || "").toLowerCase();
  if (workType === "preprint" || (c.arxiv_id && !c.venue)) {
    return c.venue ? `Preprint (${c.venue})` : "Preprint";
  }
  return c.venue || "Unknown venue";
}

export function authorLine(c: DiscoveryCandidate): string {
  const authors = (c.authors ?? []).slice(0, 4).join(", ");
  const year = c.publication_year ? ` (${c.publication_year})` : "";
  const parts = [authors, venueLabel(c)].filter(Boolean).join(" — ");
  return `${parts}${year}`.trim() || "Unknown source";
}
