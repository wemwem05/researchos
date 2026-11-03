// Milestone 5 agent-run mirror (prototype/agent_graph.py) + pure view helpers.

export type AgentRunStatus =
  | "running"
  | "awaiting_human"
  | "completed"
  | "discarded"
  | "failed";

export interface AgentTransition {
  node: string;
  at: string;
  detail: string;
}

export interface HumanReviewRequestView {
  kind: string;
  question: string;
  why_stopped: string;
  investigator_position: string;
  critic_concern: string;
  evidence_a: string;
  evidence_b: string;
  finding_refs: string[];
  object_refs: string[];
  options: string[];
  allow_free_text: boolean;
}

export interface HumanReviewResponseView {
  choice: string;
  free_text: string;
  decided_at: string | null;
}

export interface AgentTelemetryEntry {
  timestamp: string;
  provider: string;
  model: string;
  node: string;
  latency_ms: number;
  success: boolean;
}

export interface AgentRunRecord {
  thread_id: string;
  project_id: string;
  paper_id: string;
  research_question: string;
  status: AgentRunStatus;
  synthetic: boolean;
  created_at: string;
  updated_at: string;
  transitions: AgentTransition[];
  decisions: string[];
  validation_failures: string[];
  revision_path: string[];
  human_review_request: HumanReviewRequestView | null;
  human_review_response: HumanReviewResponseView | null;
  final_status: string;
  error: string | null;
  telemetry: AgentTelemetryEntry[];
}

export interface AgentTelemetrySummary {
  model: string;
  provider: string;
  calls: number;
  successful_calls: number;
  failed_calls: number;
  avg_latency_ms: number | null;
  calls_by_node: Record<string, number>;
  latest_at: string | null;
}

/** Exact hackathon model identity, mirrored from prototype/spark_open.py. */
export const SPARK_OPEN_MODEL_ID = "meta-models/muse-spark-open-1026";
export const SPARK_OPEN_PROVIDER = "Baseten";
export const AGENT_ROLES = ["Investigator", "Scientific Critic", "Revision"] as const;

export type TimelineState = "done" | "active" | "waiting" | "skipped";

export interface TimelineStep {
  id: string;
  label: string;
  state: TimelineState;
  detail: string;
}

const NODES = new Set([
  "load_project_context",
  "investigator",
  "validate",
  "critic",
  "critic_after_revision",
  "decision_gate",
  "revise_once",
  "prepare_review",
  "human_review_requested",
  "human_review_resumed",
  "finalize",
  "apply_final_decision",
  "update_project_state",
  "failed",
]);

function transitionDetail(record: AgentRunRecord, node: string): string {
  const t = record.transitions.find((x) => x.node === node);
  return t?.detail ?? "";
}

function hasNode(record: AgentRunRecord, ...nodes: string[]): boolean {
  return record.transitions.some((t) => nodes.includes(t.node));
}

/** Researcher-facing timeline derived from node transitions + run status. */
export function timelineSteps(record: AgentRunRecord): TimelineStep[] {
  const done = (id: string, label: string, detail = ""): TimelineStep => ({
    id,
    label,
    state: "done",
    detail,
  });
  const skipped = (id: string, label: string, detail = ""): TimelineStep => ({
    id,
    label,
    state: "skipped",
    detail,
  });
  const finished = ["completed", "discarded", "failed"].includes(record.status);
  const awaiting = record.status === "awaiting_human";

  const investigator: TimelineStep = hasNode(record, "investigator")
    ? done("investigator", "Investigator", transitionDetail(record, "investigator"))
    : { id: "investigator", label: "Investigator", state: finished ? "skipped" : "active", detail: "" };

  const validation: TimelineStep = hasNode(record, "validate")
    ? done("validation", "Deterministic validation", "All gates passed")
    : hasNode(record, "validate_failed")
      ? { id: "validation", label: "Deterministic validation", state: "done", detail: "Failed — project untouched" }
      : skipped("validation", "Deterministic validation");

  const critic: TimelineStep = hasNode(record, "critic", "critic_after_revision")
    ? done("critic", "Scientific critic", transitionDetail(record, "critic_after_revision") || transitionDetail(record, "critic"))
    : skipped("critic", "Scientific critic");

  const revision: TimelineStep = hasNode(record, "revise_once")
    ? done("revision", "Revision", "One bounded correction applied")
    : skipped("revision", "Revision", "Not needed");

  let review: TimelineStep;
  if (record.human_review_response) {
    review = done("human_review", "Researcher review", `Decided: ${record.human_review_response.choice}`);
  } else if (awaiting) {
    review = { id: "human_review", label: "Researcher review", state: "waiting", detail: "Waiting for your judgment" };
  } else {
    review = skipped("human_review", "Researcher review", "No ambiguity — not needed");
  }

  let outcome: TimelineStep;
  if (record.status === "completed") {
    outcome = done("outcome", "Project updated", transitionDetail(record, "update_project_state"));
  } else if (record.status === "discarded") {
    outcome = done("outcome", "Run discarded", "Researcher decision; project untouched");
  } else if (record.status === "failed") {
    outcome = { id: "outcome", label: "Run failed", state: "done", detail: record.error ?? "" };
  } else {
    outcome = { id: "outcome", label: "Project update", state: "active", detail: "" };
  }

  return [investigator, validation, critic, revision, review, outcome];
}

export function isKnownNode(node: string): boolean {
  return NODES.has(node);
}

export function statusLabel(status: AgentRunStatus): string {
  switch (status) {
    case "running":
      return "Running";
    case "awaiting_human":
      return "Researcher review required";
    case "completed":
      return "Completed";
    case "discarded":
      return "Discarded";
    case "failed":
      return "Failed";
  }
}

export function formatLatency(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Short human handle for a long thread id: last segment. */
export function shortThread(threadId: string): string {
  const parts = threadId.split("--");
  return parts[parts.length - 1] || threadId;
}

/** Local agent API base (live HITL resume). Null when unreachable. */
export const AGENT_API_BASE = "http://127.0.0.1:8765";

export async function probeAgentApi(timeoutMs = 1500): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${AGENT_API_BASE}/api/agent/runs`, { signal: ctrl.signal });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
