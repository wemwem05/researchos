import { describe, expect, it } from "vitest";
import {
  authorLine,
  candidateSourceUrl,
  evidenceLabel,
  filterCandidates,
  isOpenAccess,
  isRunStale,
  sortCandidates,
  statusLabel,
  venueLabel,
  verdictLabel,
  type DiscoveryCandidate,
  type DiscoveryRun,
} from "./discovery";

function cand(over: Partial<DiscoveryCandidate> = {}): DiscoveryCandidate {
  return {
    candidate_id: "dc1",
    title: "T",
    authors: ["A. Uthor"],
    publication_year: 2024,
    venue: "Venue",
    work_type: "article",
    doi: "",
    openalex_id: "",
    arxiv_id: "",
    abstract: "",
    landing_page_url: "",
    open_access_url: "",
    pdf_url: "",
    license: "",
    citation_count: null,
    source_provider: "openalex",
    retrieval_query: "q",
    retrieval_round: 1,
    research_question_version: 1,
    evidence_access_level: "title_and_abstract",
    evaluation: {
      verdict: "relevant",
      why_relevant: "",
      may_contribute: "",
      may_overlap_with: "",
      still_unknown: "",
      why_read_next: "",
      novelty_overlap: false,
      novelty_note: "",
    },
    recommended: false,
    status: "pending",
    status_reason: "",
    already_in_project: false,
    project_paper_id: "",
    ...over,
  };
}

function run(over: Partial<DiscoveryRun> = {}): DiscoveryRun {
  return {
    run_id: "r1",
    thread_id: "r1",
    project_id: "p",
    research_question: "Q?",
    research_question_version: 1,
    focus: "",
    status: "completed",
    stale: false,
    created_at: "",
    updated_at: "",
    budget: {},
    strategy_notes: "",
    queries: [],
    counts: {},
    reflection_decision: "",
    reflection_explanation: "",
    critic_verdict: "",
    critic_notes: "",
    revision_used: false,
    limitations: "",
    error: "",
    candidates: [],
    usage: {},
    ...over,
  };
}

describe("discovery filters", () => {
  const all = [
    cand({ candidate_id: "dc1" }),
    cand({
      candidate_id: "dc2",
      evaluation: { ...cand().evaluation, verdict: "highly_relevant" },
    }),
    cand({
      candidate_id: "dc3",
      evaluation: { ...cand().evaluation, novelty_overlap: true },
    }),
    cand({ candidate_id: "dc4", pdf_url: "https://x.org/a.pdf" }),
    cand({ candidate_id: "dc5", already_in_project: true }),
  ];

  it("returns everything for ALL", () => {
    expect(filterCandidates(all, "ALL")).toHaveLength(5);
  });

  it("filters by verdict, novelty, access, and project membership", () => {
    expect(
      filterCandidates(all, "HIGHLY_RELEVANT").map((c) => c.candidate_id)
    ).toEqual(["dc2"]);
    expect(
      filterCandidates(all, "NOVELTY_OVERLAP").map((c) => c.candidate_id)
    ).toEqual(["dc3"]);
    expect(filterCandidates(all, "OPEN_ACCESS").map((c) => c.candidate_id)).toEqual([
      "dc4",
    ]);
    expect(
      filterCandidates(all, "ALREADY_IN_PROJECT").map((c) => c.candidate_id)
    ).toEqual(["dc5"]);
  });

  it("treats open_access_url as open access", () => {
    const c = cand({ open_access_url: "https://x.org/landing" });
    expect(isOpenAccess(c)).toBe(true);
    expect(isOpenAccess(cand())).toBe(false);
  });
});

describe("candidate sorting", () => {
  it("recommended first, then verdict rank, stable by id", () => {
    const input = [
      cand({ candidate_id: "dc3", recommended: true }),
      cand({
        candidate_id: "dc1",
        evaluation: { ...cand().evaluation, verdict: "tangential" },
      }),
      cand({
        candidate_id: "dc2",
        evaluation: { ...cand().evaluation, verdict: "highly_relevant" },
      }),
    ];
    expect(sortCandidates(input).map((c) => c.candidate_id)).toEqual([
      "dc3",
      "dc2",
      "dc1",
    ]);
  });
});

describe("discovery labels", () => {
  it("labels evidence levels honestly", () => {
    expect(evidenceLabel("metadata_only")).toBe("Metadata only");
    expect(evidenceLabel("title_and_abstract")).toBe("Title + abstract");
    expect(evidenceLabel("full_text_available")).toBe("Full text available");
    expect(evidenceLabel("full_text_analyzed")).toBe("Full text analyzed");
  });

  it("labels verdicts and statuses", () => {
    expect(verdictLabel("highly_relevant")).toBe("Highly relevant");
    expect(verdictLabel("off_topic")).toBe("Off topic");
    expect(statusLabel("import_failed")).toBe("Import failed");
    expect(statusLabel("full_text_unavailable")).toBe("Full text unavailable");
  });

  it("never presents a preprint as peer-reviewed", () => {
    expect(venueLabel(cand({ work_type: "preprint", venue: "" }))).toBe(
      "Preprint"
    );
    expect(venueLabel(cand({ work_type: "preprint", venue: "arXiv" }))).toBe(
      "Preprint (arXiv)"
    );
    expect(venueLabel(cand({ venue: "" }))).toBe("Unknown venue");
    expect(venueLabel(cand())).toBe("Venue");
  });

  it("builds the author line with venue and year", () => {
    expect(authorLine(cand())).toBe("A. Uthor — Venue (2024)");
  });
});

describe("source links", () => {
  it("prefers landing page, then DOI, then OpenAlex, then arXiv", () => {
    expect(
      candidateSourceUrl(
        cand({ landing_page_url: "https://l.example", doi: "10.1/x" })
      )
    ).toBe("https://l.example");
    expect(candidateSourceUrl(cand({ doi: "10.1/x" }))).toBe(
      "https://doi.org/10.1/x"
    );
    expect(
      candidateSourceUrl(cand({ openalex_id: "https://openalex.org/W1" }))
    ).toBe("https://openalex.org/W1");
    expect(candidateSourceUrl(cand({ arxiv_id: "2401.00001" }))).toBe(
      "https://arxiv.org/abs/2401.00001"
    );
    expect(candidateSourceUrl(cand())).toBe("");
  });
});

describe("run staleness", () => {
  it("flags older question versions and mid-search changes", () => {
    expect(isRunStale(run(), 1)).toBe(false);
    expect(isRunStale(run(), 2)).toBe(true);
    expect(isRunStale(run({ stale: true }), 1)).toBe(true);
  });
});
