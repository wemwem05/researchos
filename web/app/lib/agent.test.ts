import { describe, expect, it } from "vitest";
import {
  AGENT_ROLES,
  SPARK_OPEN_MODEL_ID,
  SPARK_OPEN_PROVIDER,
  formatLatency,
  shortThread,
  statusLabel,
  timelineSteps,
  type AgentRunRecord,
} from "./agent";

function record(over: Partial<AgentRunRecord>): AgentRunRecord {
  return {
    thread_id: "proj--p2--abc123",
    project_id: "proj",
    paper_id: "p2",
    research_question: "Q?",
    status: "completed",
    synthetic: false,
    created_at: "2026-11-03T00:00:00+00:00",
    updated_at: "2026-11-03T00:01:00+00:00",
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
  };
}

function t(node: string, detail = "") {
  return { node, at: "2026-11-03T00:00:00+00:00", detail };
}

describe("agent model identity", () => {
  it("pins the exact hackathon model id and provider", () => {
    expect(SPARK_OPEN_MODEL_ID).toBe("meta-models/muse-spark-open-1026");
    expect(SPARK_OPEN_PROVIDER).toBe("Baseten");
  });

  it("names the three agent roles", () => {
    expect([...AGENT_ROLES]).toEqual(["Investigator", "Scientific Critic", "Revision"]);
  });
});

describe("timelineSteps", () => {
  it("marks every step done on a clean completed run", () => {
    const r = record({
      transitions: [
        t("load_project_context"),
        t("investigator", "5 findings"),
        t("validate"),
        t("critic", "verdict=SUPPORTED"),
        t("decision_gate"),
        t("finalize"),
        t("apply_final_decision"),
        t("update_project_state"),
      ],
    });
    const steps = timelineSteps(r);
    expect(steps.map((s) => s.id)).toEqual([
      "investigator",
      "validation",
      "critic",
      "revision",
      "human_review",
      "outcome",
    ]);
    expect(steps.find((s) => s.id === "investigator")?.state).toBe("done");
    expect(steps.find((s) => s.id === "critic")?.state).toBe("done");
    expect(steps.find((s) => s.id === "revision")?.state).toBe("skipped");
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("skipped");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("done");
  });

  it("flags the review step waiting while interrupted", () => {
    const r = record({
      status: "awaiting_human",
      transitions: [
        t("investigator"),
        t("validate"),
        t("critic", "verdict=AMBIGUOUS_REQUIRES_HUMAN"),
        t("human_review_requested"),
      ],
      human_review_request: {
        kind: "ambiguity",
        question: "Comparable?",
        why_stopped: "Ambiguous",
        investigator_position: "Yes",
        critic_concern: "Splits differ",
        evidence_a: "A",
        evidence_b: "B",
        finding_refs: [],
        object_refs: [],
        options: ["Comparable", "Not comparable"],
        allow_free_text: true,
      },
    });
    const steps = timelineSteps(r);
    expect(steps.find((s) => s.id === "human_review")?.state).toBe("waiting");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("active");
  });

  it("shows the revision step done after a bounded correction", () => {
    const r = record({
      transitions: [
        t("investigator"),
        t("validate"),
        t("critic", "verdict=REVISION_REQUIRED"),
        t("revise_once"),
        t("validate"),
        t("critic_after_revision", "verdict=SUPPORTED"),
        t("update_project_state"),
      ],
    });
    const steps = timelineSteps(r);
    expect(steps.find((s) => s.id === "revision")?.state).toBe("done");
    expect(steps.find((s) => s.id === "outcome")?.state).toBe("done");
  });

  it("records the researcher's decision after resume", () => {
    const r = record({
      human_review_response: { choice: "Not comparable", free_text: "", decided_at: null },
      transitions: [t("investigator"), t("human_review_requested"), t("human_review_resumed")],
    });
    const review = timelineSteps(r).find((s) => s.id === "human_review");
    expect(review?.state).toBe("done");
    expect(review?.detail).toContain("Not comparable");
  });
});

describe("helpers", () => {
  it("labels every run status", () => {
    expect(statusLabel("awaiting_human")).toBe("Researcher review required");
    expect(statusLabel("completed")).toBe("Completed");
    expect(statusLabel("failed")).toBe("Failed");
  });

  it("formats latency for researchers", () => {
    expect(formatLatency(null)).toBe("—");
    expect(formatLatency(250)).toBe("250 ms");
    expect(formatLatency(62386)).toBe("62.4 s");
  });

  it("shortens thread ids", () => {
    expect(shortThread("proj--p2--abc123")).toBe("abc123");
  });
});
