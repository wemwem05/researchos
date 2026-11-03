import { describe, expect, it } from "vitest";
import {
  CONNECTION_TYPES,
  CORPUS_SCOPE_PREFIX,
  endpointLabel,
  findingReaderLink,
  findingsForPaper,
  groupConnectionsByType,
  type Connection,
  type ProjectData,
} from "./project";

const project: ProjectData = {
  schema_version: "m4-1",
  research_question: "Q?",
  papers: [
    { paper_id: "a", title: "Alpha: Stuff", filename: "a.pdf", added_order: 1, n_objects: 1, n_localized: 1, web_pdf: "/papers/a.pdf", web_data: "/data/papers/a.json" },
    { paper_id: "b", title: "Beta", filename: "b.pdf", added_order: 2, n_objects: 1, n_localized: 1, web_pdf: "/papers/b.pdf", web_data: "/data/papers/b.json" },
  ],
  findings: [
    { id: "f1", paper_id: "a", statement: "s", evidence: "p.1", page: 1, object_ids: ["eq-1"], confidence: 0.9 },
    { id: "f2", paper_id: "b", statement: "s", evidence: "p.2", page: 2, object_ids: [], confidence: 0.8 },
  ],
  connections: [
    { id: "c1", from_ref: "f2", to_ref: "f1", type: "extends", reason: "r", confidence: 0.7 },
    { id: "c2", from_ref: "f2", to_ref: "paper:a", type: "contradicts", reason: "r", confidence: 0.6 },
  ],
  contributions: [],
  open_questions: [],
  novelty_risks: [],
};

describe("project helpers", () => {
  it("enum holds exactly the seven requested types", () => {
    expect([...CONNECTION_TYPES].sort()).toEqual(
      ["supports", "contradicts", "extends", "overlaps_with", "uses_same_method", "uses_different_assumption", "leaves_open"].sort()
    );
  });

  it("groups connections by type, omitting absent types", () => {
    const g = groupConnectionsByType(project.connections);
    expect(g.get("extends")?.map((c: Connection) => c.id)).toEqual(["c1"]);
    expect(g.get("contradicts")?.map((c: Connection) => c.id)).toEqual(["c2"]);
    expect(g.has("supports")).toBe(false);
  });

  it("labels finding and paper endpoints", () => {
    expect(endpointLabel(project, "f1")).toBe("f1 · Alpha");
    expect(endpointLabel(project, "paper:a")).toBe("Alpha: Stuff");
    expect(endpointLabel(project, "ghost")).toBe("ghost");
  });

  it("filters findings per paper", () => {
    expect(findingsForPaper(project, "a").map((f) => f.id)).toEqual(["f1"]);
  });

  it("deep-links findings into the reader", () => {
    expect(findingReaderLink(project, project.findings[0])).toBe("/read/a?select=eq-1");
    expect(findingReaderLink(project, project.findings[1])).toBe("/read/b");
  });

  it("keeps the corpus-scope prefix global-claim-free", () => {
    expect(CORPUS_SCOPE_PREFIX).toMatch(/Within the papers currently analyzed/);
    expect(CORPUS_SCOPE_PREFIX).not.toMatch(/never|always|global/i);
  });
});
