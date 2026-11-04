import { describe, expect, it } from "vitest";
import {
  STAGE_LABELS,
  dedupeQuestionHistory,
  formatBytes,
  isJobActive,
  paperDataUrl,
  paperFileUrl,
  reviewUrl,
  runDetailUrl,
  stageLabel,
  type QuestionVersionRecord,
  type WorkspaceJob,
} from "./workspace";

function job(state: WorkspaceJob["state"]): WorkspaceJob {
  return {
    job_id: "job-1",
    project_id: "p",
    paper_id: "a",
    job_type: "ingest_paper",
    state,
    stage: "localizing",
    stage_detail: "",
    attempts: 1,
    max_attempts: 3,
    last_error: "",
    result: {},
    created_at: "",
    updated_at: "",
  };
}

describe("workspace helpers", () => {
  it("labels every known ingest stage with a human phrase", () => {
    for (const [stage, label] of Object.entries(STAGE_LABELS)) {
      expect(stageLabel(stage)).toBe(label);
      expect(label).not.toMatch(/%/);
    }
    expect(stageLabel("localizing")).toBe("Localizing equations and figures");
  });

  it("falls back to a readable form for unknown stages", () => {
    expect(stageLabel("some_new_stage")).toBe("some new stage");
  });

  it("detects active jobs", () => {
    expect(isJobActive(job("QUEUED"))).toBe(true);
    expect(isJobActive(job("RUNNING"))).toBe(true);
    expect(isJobActive(job("COMPLETED"))).toBe(false);
    expect(isJobActive(job("FAILED"))).toBe(false);
    expect(isJobActive(job("CANCELLED"))).toBe(false);
  });

  it("scopes reader and review URLs to the project", () => {
    expect(paperFileUrl("pa", "paper-1")).toContain("/api/projects/pa/papers/paper-1/file");
    expect(paperDataUrl("pa", "paper-1")).toContain("/api/projects/pa/papers/paper-1/data");
    expect(paperFileUrl("pb", "paper-1")).not.toBe(paperFileUrl("pa", "paper-1"));
    expect(runDetailUrl("pa", "t1")).toContain("/api/projects/pa/runs/t1");
    expect(reviewUrl("pa", "t1")).toContain("/api/projects/pa/runs/t1/review");
  });

  it("formats byte sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MiB");
    expect(formatBytes(NaN)).toBe("—");
  });

  it("dedupes question history by version, preferring the snapshot", () => {
    const empty: QuestionVersionRecord = {
      version: 1,
      question: "q1",
      created_at: "",
      status: "superseded",
      findings: [],
      connections: [],
      open_questions: [],
      novelty_risks: [],
    };
    const snapshot = { ...empty, findings: [{}, {}] };
    const v2: QuestionVersionRecord = { ...empty, version: 2, status: "current" };
    const out = dedupeQuestionHistory([empty, snapshot, v2]);
    expect(out.map((h) => h.version)).toEqual([1, 2]);
    expect(out[0].findings).toHaveLength(2);
  });
});
