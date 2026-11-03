import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatSeconds,
  verifyStatusLabel,
  type VerificationEvidenceView,
  type VerificationStatus,
} from "./verification";
import { timelineSteps } from "./agent";

function evidence(over: Partial<VerificationEvidenceView>): VerificationEvidenceView {
  return {
    id: "v1",
    thread_id: "proj--verify--p1--claim-1--abc",
    paper_id: "p1",
    claim_id: "claim-1",
    claim_statement: "SW is efficient.",
    object_refs: ["claim-1"],
    status: "supported",
    status_label: "supported in tested conditions",
    plan: {},
    code_sha256: "abc",
    params: {},
    environment: { image: "img", runtime: "{}", pip_freeze: "" },
    criteria: [],
    result_summary: { cells: {}, median_ratios: {} },
    observed: [],
    supported_conclusions: [],
    partial: [],
    out_of_scope: [],
    limitations: [],
    scaling_comment: "",
    repairs: [],
    attempts: 1,
    modal: { sandbox_id: "sb", returncode: 0, timed_out: false, duration_s: 3, infra_error: "" },
    created_at: "2026-11-04T00:00:00+00:00",
    web_code: null,
    web_measurements: null,
    web_plot: null,
    web_stdout: null,
    web_stderr: null,
    web_evidence: null,
    ...over,
  };
}

describe("verification status labels", () => {
  it("labels every status without a reproduced badge", () => {
    const labels: Record<VerificationStatus, string> = {
      supported: verifyStatusLabel("supported"),
      not_supported: verifyStatusLabel("not_supported"),
      inconclusive: verifyStatusLabel("inconclusive"),
      execution_failed: verifyStatusLabel("execution_failed"),
      not_testable: verifyStatusLabel("not_testable"),
    };
    expect(labels.supported).toBe("Supported in tested conditions");
    expect(labels.not_supported).toBe("Not supported in tested conditions");
    expect(labels.inconclusive).toBe("Inconclusive");
    expect(labels.execution_failed).toBe("Execution failed");
    expect(labels.not_testable).toBe("Not computationally testable");
    for (const label of Object.values(labels)) {
      expect(label.toLowerCase()).not.toContain("reproduced");
    }
  });

  it("evidence fixtures carry the audit fields", () => {
    const ev = evidence({ status: "inconclusive" });
    expect(ev.status).toBe("inconclusive");
    expect(ev.code_sha256).toBeTruthy();
    expect(ev.modal.sandbox_id).toBe("sb");
  });
});

describe("formatters", () => {
  it("formats durations and bytes", () => {
    expect(formatSeconds(null)).toBe("—");
    expect(formatSeconds(0.035)).toBe("35.0 ms");
    expect(formatSeconds(2.5)).toBe("2.50 s");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
  });
});

describe("verify timeline", () => {
  function run(over: object = {}) {
    return {
      thread_id: "t",
      project_id: "p",
      kind: "verify",
      paper_id: "p1",
      claim_id: "claim-1",
      research_question: "Q?",
      status: "completed",
      synthetic: false,
      created_at: "",
      updated_at: "",
      transitions: [],
      decisions: [],
      validation_failures: [],
      revision_path: [],
      human_review_request: null,
      human_review_response: null,
      final_status: "completed",
      error: null,
      telemetry: [],
      ...over,
    } as Parameters<typeof timelineSteps>[0];
  }

  const t = (node: string, detail = "") => ({ node, at: "", detail });

  it("routes verify runs to the verification timeline", () => {
    const steps = timelineSteps(
      run({
        transitions: [
          t("select_claim"),
          t("experiment_planner", "6 sizes"),
          t("generate_code"),
          t("modal_execute", "rc=0"),
          t("interpret_result"),
          t("attach_to_project", "attached v1"),
        ],
      })
    );
    expect(steps.map((s) => s.id)).toEqual(["plan", "code", "execute", "human_review", "outcome"]);
    expect(steps.find((s) => s.id === "plan")?.state).toBe("done");
    expect(steps.find((s) => s.id === "execute")?.state).toBe("done");
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("skipped");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("done");
  });

  it("flags repair and waiting review on verify runs", () => {
    const steps = timelineSteps(
      run({
        status: "awaiting_human",
        transitions: [t("experiment_planner"), t("diagnose_and_repair"), t("human_review_requested")],
        human_review_request: {
          kind: "verification",
          question: "Approve?",
          why_stopped: "unsafe",
          investigator_position: "",
          critic_concern: "",
          evidence_a: "",
          evidence_b: "",
          finding_refs: [],
          object_refs: [],
          options: ["Abort verification", "Auto-repair and retry"],
          allow_free_text: true,
        },
      })
    );
    expect(steps.find((s) => s.id === "code")?.detail).toContain("Repaired");
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("waiting");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("active");
  });
});
