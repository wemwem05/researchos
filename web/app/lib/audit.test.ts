import { describe, expect, it } from "vitest";
import {
  AUDIT_CHECK_BADGE,
  AUDIT_OVERALL_BADGE,
  EXTERNAL_SEARCH_BUDGET,
  auditCheckLabel,
  comparabilityLabel,
  type AuditCheckStatus,
  type AuditOverallAssessment,
  type ScientificAuditView,
} from "./audit";
import { timelineSteps } from "./agent";

function audit(over: Partial<ScientificAuditView> = {}): ScientificAuditView {
  return {
    audit_id: "a1",
    thread_id: "proj--audit--p1--claim-1--abc",
    project_id: "proj",
    paper_id: "p1",
    claim_id: "claim-1",
    claim_text: "Ours is best.",
    claim_type: ["empirical_benchmark", "numerical"],
    audit_scope: "Table 1",
    checks: [],
    methodology_findings: [],
    cross_paper_findings: [],
    external_sources: [],
    external_queries: [],
    external_notes: [],
    unresolved_questions: [],
    overall_assessment: "partially substantiated",
    assessment_rationale: "r",
    limitations: [],
    provenance: {},
    model_telemetry: [],
    created_at: "2026-11-04T00:00:00+00:00",
    ...over,
  };
}

describe("audit assessments stay distinct from computational statuses", () => {
  it("covers all five evidence-scoped assessments", () => {
    const assessments: AuditOverallAssessment[] = [
      "substantiated within audited evidence",
      "partially substantiated",
      "discrepancies identified",
      "inconclusive on audited evidence",
      "insufficient evidence",
    ];
    for (const a of assessments) {
      expect(AUDIT_OVERALL_BADGE[a]).toBeTruthy();
      expect(a).not.toBe("inconclusive");
      expect(a.toLowerCase()).not.toContain("reproduced");
    }
    expect(audit({ overall_assessment: assessments[2] }).overall_assessment).toBe(
      "discrepancies identified"
    );
  });

  it("labels every check status", () => {
    const statuses: AuditCheckStatus[] = [
      "PASSED",
      "DISCREPANCY",
      "POTENTIAL_CONCERN",
      "INCONCLUSIVE",
      "NOT_EVALUATED",
    ];
    for (const s of statuses) {
      expect(AUDIT_CHECK_BADGE[s]).toBeTruthy();
      expect(auditCheckLabel(s)).toBeTruthy();
    }
    expect(auditCheckLabel("NOT_EVALUATED")).toBe("Not evaluated");
  });

  it("labels comparability outcomes", () => {
    expect(comparabilityLabel("DIRECTLY_COMPARABLE")).toBe("Directly comparable");
    expect(comparabilityLabel("PARTIALLY_COMPARABLE")).toBe("Partially comparable");
    expect(comparabilityLabel("NOT_DIRECTLY_COMPARABLE")).toBe("Not directly comparable");
    expect(comparabilityLabel("UNCERTAIN")).toBe("Uncertain");
  });

  it("pins the external literature budget", () => {
    expect(EXTERNAL_SEARCH_BUDGET.maxQueries).toBe(2);
    expect(EXTERNAL_SEARCH_BUDGET.maxRecords).toBe(10);
    expect(EXTERNAL_SEARCH_BUDGET.maxPapers).toBe(3);
  });
});

describe("audit timeline", () => {
  function run(over: object = {}) {
    return {
      thread_id: "t",
      project_id: "p",
      kind: "audit",
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

  it("routes audit runs to the audit timeline", () => {
    const steps = timelineSteps(
      run({
        transitions: [
          t("classify_claim", "empirical_benchmark,numerical"),
          t("plan_audit_checks", "5 checks"),
          t("execute_numerical_checks", "5 checks"),
          t("methodology_assessment", "8 findings"),
          t("compare_project_evidence", "4 comparisons"),
          t("assess_sources", "3 selected"),
          t("scientific_critic", "verdict=SUPPORTED"),
          t("attach_to_project", "attached a1"),
        ],
      })
    );
    expect(steps.map((s) => s.id)).toEqual([
      "classify",
      "checks",
      "methodology",
      "cross_paper",
      "critic",
      "human_review",
      "outcome",
    ]);
    expect(steps.find((s) => s.id === "classify")?.detail).toContain("numerical");
    expect(steps.find((s) => s.id === "cross_paper")?.detail).toContain("3 selected");
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("skipped");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("done");
  });

  it("shows waiting review on interrupted audits", () => {
    const steps = timelineSteps(
      run({
        status: "awaiting_human",
        transitions: [t("classify_claim"), t("human_review_requested")],
        human_review_request: {
          kind: "audit",
          question: "Accept?",
          why_stopped: "ambiguity",
          investigator_position: "",
          critic_concern: "",
          evidence_a: "",
          evidence_b: "",
          finding_refs: [],
          object_refs: [],
          options: ["Accept audit as-is", "Discard audit"],
          allow_free_text: true,
        },
      })
    );
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("waiting");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("active");
    expect(steps.find((s) => s.id === "methodology")?.state).toBe("active");
  });
});
