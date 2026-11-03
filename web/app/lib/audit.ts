// Milestone 6.1 evidence-audit mirror (prototype/project.py ScientificAudit)
// + pure view helpers.

import { AGENT_API_BASE } from "./agent";

export type AuditOverallAssessment =
  | "substantiated within audited evidence"
  | "partially substantiated"
  | "discrepancies identified"
  | "inconclusive on audited evidence"
  | "insufficient evidence";

export type AuditCheckStatus =
  | "PASSED"
  | "DISCREPANCY"
  | "POTENTIAL_CONCERN"
  | "INCONCLUSIVE"
  | "NOT_EVALUATED";

export interface AuditCheckView {
  check_id: string;
  check_type: string;
  description: string;
  status: AuditCheckStatus;
  evidence_refs: string[];
  actual_computation_or_reasoning: string;
  limitations: string;
}

export interface AuditMethodologyView {
  severity: "OBSERVED_PROBLEM" | "POTENTIAL_CONCERN" | "MISSING_INFORMATION";
  category: string;
  finding: string;
  evidence: string;
  evidence_refs: string[];
}

export interface AuditCrossPaperView {
  comparability:
    | "DIRECTLY_COMPARABLE"
    | "PARTIALLY_COMPARABLE"
    | "NOT_DIRECTLY_COMPARABLE"
    | "UNCERTAIN";
  finding_ref: string;
  paper_id: string;
  relationship: string;
  detail: string;
  evidence: string;
}

export interface AuditExternalSourceView {
  openalex_id?: string;
  doi?: string;
  title?: string;
  authors?: string[];
  year?: number | null;
  cited_by?: number | null;
  abstract?: string;
  evidence_level?: string;
  url?: string;
}

export interface AuditTelemetryView {
  timestamp: string;
  provider: string;
  model: string;
  node: string;
  latency_ms: number;
  success: boolean;
}

export interface ScientificAuditView {
  audit_id: string;
  thread_id: string;
  project_id: string;
  paper_id: string;
  claim_id: string;
  claim_text: string;
  claim_type: string[];
  audit_scope: string;
  checks: AuditCheckView[];
  methodology_findings: AuditMethodologyView[];
  cross_paper_findings: AuditCrossPaperView[];
  external_sources: AuditExternalSourceView[];
  external_queries: string[];
  external_notes: string[];
  unresolved_questions: string[];
  overall_assessment: AuditOverallAssessment;
  assessment_rationale: string;
  limitations: string[];
  provenance: Record<string, unknown>;
  model_telemetry: AuditTelemetryView[];
  created_at: string;
}

export const AUDIT_OVERALL_BADGE: Record<AuditOverallAssessment, string> = {
  "substantiated within audited evidence": "bg-green-100 text-green-900",
  "partially substantiated": "bg-blue-100 text-blue-900",
  "discrepancies identified": "bg-red-100 text-red-900",
  "inconclusive on audited evidence": "bg-amber-100 text-amber-900",
  "insufficient evidence": "bg-neutral-200 text-neutral-700",
};

export const AUDIT_CHECK_BADGE: Record<AuditCheckStatus, string> = {
  PASSED: "bg-green-100 text-green-900",
  DISCREPANCY: "bg-red-100 text-red-900",
  POTENTIAL_CONCERN: "bg-amber-100 text-amber-900",
  INCONCLUSIVE: "bg-amber-100 text-amber-900",
  NOT_EVALUATED: "bg-neutral-200 text-neutral-600",
};

export const AUDIT_SEVERITY_BADGE: Record<string, string> = {
  OBSERVED_PROBLEM: "bg-red-100 text-red-900",
  POTENTIAL_CONCERN: "bg-amber-100 text-amber-900",
  MISSING_INFORMATION: "bg-neutral-200 text-neutral-700",
};

export function auditCheckLabel(status: AuditCheckStatus): string {
  switch (status) {
    case "PASSED":
      return "Passed";
    case "DISCREPANCY":
      return "Discrepancy";
    case "POTENTIAL_CONCERN":
      return "Potential concern";
    case "INCONCLUSIVE":
      return "Inconclusive";
    case "NOT_EVALUATED":
      return "Not evaluated";
  }
}

export function comparabilityLabel(value: string): string {
  switch (value) {
    case "DIRECTLY_COMPARABLE":
      return "Directly comparable";
    case "PARTIALLY_COMPARABLE":
      return "Partially comparable";
    case "NOT_DIRECTLY_COMPARABLE":
      return "Not directly comparable";
    default:
      return "Uncertain";
  }
}

/** External literature budget surfaced in the confirm dialog (mirrors openalex.py). */
export const EXTERNAL_SEARCH_BUDGET = {
  maxQueries: 2,
  maxRecords: 10,
  maxPapers: 3,
} as const;

/** Start an evidence audit from the UI. Refs only — the server validates them. */
export async function startAudit(
  paperId: string,
  claimId: string,
  external: boolean
): Promise<{ thread_id: string }> {
  const r = await fetch(`${AGENT_API_BASE}/api/agent/audits/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_id: paperId, claim_id: claimId, external }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body as { thread_id: string };
}

export async function fetchLiveAudits(): Promise<ScientificAuditView[]> {
  const r = await fetch(`${AGENT_API_BASE}/api/agent/audits`);
  if (!r.ok) throw new Error(r.statusText);
  const body = (await r.json()) as { audits: ScientificAuditView[] };
  return body.audits ?? [];
}
