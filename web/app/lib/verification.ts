// Milestone 6 verification mirror (prototype/project.py VerificationEvidence)
// + pure view helpers.

import { AGENT_API_BASE } from "./agent";

export type VerificationStatus =
  | "supported"
  | "not_supported"
  | "inconclusive"
  | "execution_failed"
  | "not_testable";

export interface VerificationCriterion {
  name: string;
  kind: string;
  rule: string;
  evaluated: boolean;
  met: boolean;
  detail: string;
}

export interface VerificationCell {
  n: number;
  median_s: number;
  iqr_s: [number, number];
  min_s: number;
  max_s: number;
  median_mem_bytes: number;
}

export interface VerificationRepair {
  attempt: number;
  diagnosis: string;
  fix_strategy: string;
  code_sha256: string;
}

export interface VerificationEvidenceView {
  id: string;
  thread_id: string;
  paper_id: string;
  claim_id: string;
  claim_statement: string;
  object_refs: string[];
  status: VerificationStatus;
  status_label: string;
  plan: Record<string, unknown>;
  code_sha256: string;
  params: Record<string, unknown>;
  environment: { image: string; runtime: string; pip_freeze: string };
  criteria: VerificationCriterion[];
  result_summary: {
    cells: Record<string, VerificationCell>;
    median_ratios: Record<string, number>;
  };
  observed: string[];
  supported_conclusions: string[];
  partial: string[];
  out_of_scope: string[];
  limitations: string[];
  scaling_comment: string;
  repairs: VerificationRepair[];
  attempts: number;
  modal: {
    sandbox_id: string;
    returncode: number | null;
    timed_out: boolean;
    duration_s: number | null;
    infra_error: string;
  };
  created_at: string;
  web_code: string | null;
  web_measurements: string | null;
  web_plot: string | null;
  web_stdout: string | null;
  web_stderr: string | null;
  web_evidence: string | null;
}

export const VERIFY_STATUS_BADGE: Record<VerificationStatus, string> = {
  supported: "bg-green-100 text-green-900",
  not_supported: "bg-amber-100 text-amber-900",
  inconclusive: "bg-amber-100 text-amber-900",
  execution_failed: "bg-red-100 text-red-900",
  not_testable: "bg-neutral-200 text-neutral-700",
};

export function verifyStatusLabel(status: VerificationStatus): string {
  switch (status) {
    case "supported":
      return "Supported in tested conditions";
    case "not_supported":
      return "Not supported in tested conditions";
    case "inconclusive":
      return "Inconclusive";
    case "execution_failed":
      return "Execution failed";
    case "not_testable":
      return "Not computationally testable";
  }
}

export function formatSeconds(s: number | null): string {
  if (s === null || s === undefined || !isFinite(s)) return "—";
  if (s < 1) return `${(s * 1000).toFixed(1)} ms`;
  if (s < 60) return `${s.toFixed(2)} s`;
  return `${(s / 60).toFixed(1)} min`;
}

export function formatBytes(b: number | null): string {
  if (b === null || b === undefined || !isFinite(b)) return "—";
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KiB`;
  return `${(b / 1024 / 1024).toFixed(1)} MiB`;
}

/** Start verification from the UI. Refs only — the server validates them. */
export async function startVerification(
  paperId: string,
  claimId: string,
  projectId?: string | null
): Promise<{ thread_id: string }> {
  const url = projectId
    ? `${AGENT_API_BASE}/api/projects/${projectId}/verifications/start`
    : `${AGENT_API_BASE}/api/agent/verifications/start`;
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_id: paperId, claim_id: claimId }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  const payload = body.ok === true ? body.data : body;
  return payload as { thread_id: string };
}

export async function fetchLiveVerifications(
  projectId?: string | null
): Promise<VerificationEvidenceView[]> {
  const url = projectId
    ? `${AGENT_API_BASE}/api/projects/${projectId}/verifications`
    : `${AGENT_API_BASE}/api/agent/verifications`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText);
  const body = (await r.json()) as
    | { verifications: VerificationEvidenceView[] }
    | { ok: boolean; data: { verifications: VerificationEvidenceView[] } };
  if ("data" in body && body.data) return body.data.verifications ?? [];
  return (body as { verifications: VerificationEvidenceView[] }).verifications ?? [];
}
