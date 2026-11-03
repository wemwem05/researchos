import { describe, expect, it } from "vitest";
import {
  applyTypeFilter,
  importanceLabel,
  levelForScore,
  navigatorOrder,
  overlayTreatment,
  relevanceLabel,
} from "./relevance";
import type { SciObject } from "./types";

function obj(partial: Partial<SciObject> & { id: string }): SciObject {
  return {
    type: "equation",
    page: 1,
    label: null,
    title: "t",
    what_it_is: "x",
    why_it_is_here: "x",
    claim_supported: "x",
    limitations: "x",
    paper_importance: 0.5,
    paper_importance_reason: "r",
    research_relevance: "x",
    research_relevance_score: 0.5,
    research_relevance_reason: "r",
    research_relevance_level: "related",
    related_objects: [],
    confidence: 0.9,
    evidence: "p.1",
    equation: null,
    figure: null,
    table: null,
    ...partial,
  };
}

describe("levelForScore (mirrors prototype/relevance.py)", () => {
  it("maps spec cutoffs", () => {
    expect(levelForScore(1.0)).toBe("high");
    expect(levelForScore(0.75)).toBe("high");
    expect(levelForScore(0.749)).toBe("related");
    expect(levelForScore(0.4)).toBe("related");
    expect(levelForScore(0.399)).toBe("context");
    expect(levelForScore(0.0)).toBe("context");
  });
});

describe("labels", () => {
  it("never exposes raw scores", () => {
    expect(importanceLabel(0.9)).toBe("High");
    expect(importanceLabel(0.5)).toBe("Medium");
    expect(importanceLabel(0.1)).toBe("Low");
    expect(relevanceLabel("high")).toBe("High");
    expect(relevanceLabel("related")).toBe("Related");
    expect(relevanceLabel("context")).toBe("Context");
  });
});

describe("Focus mode filtering", () => {
  it("high stays accented, related subtle, context hidden", () => {
    expect(overlayTreatment("high", "focus", false)).toBe("accent");
    expect(overlayTreatment("related", "focus", false)).toBe("subtle");
    expect(overlayTreatment("context", "focus", false)).toBe("hidden");
  });

  it("selection overrides the mode treatment", () => {
    expect(overlayTreatment("context", "focus", true)).toBe("accent");
    expect(overlayTreatment("related", "focus", true)).toBe("accent");
  });
});

describe("Show All behavior", () => {
  it("every level gets a visible (non-hidden) treatment", () => {
    expect(overlayTreatment("high", "all", false)).toBe("accent");
    expect(overlayTreatment("related", "all", false)).toBe("subtle");
    expect(overlayTreatment("context", "all", false)).toBe("faint");
  });
});

describe("navigator", () => {
  const mixed = [
    obj({ id: "c", research_relevance_level: "context", page: 3 }),
    obj({ id: "r", research_relevance_level: "related", page: 4 }),
    obj({ id: "h", research_relevance_level: "high", page: 5 }),
  ];

  it("prioritizes high-relevance objects first", () => {
    expect(navigatorOrder(mixed).map((o) => o.id)).toEqual(["h", "r", "c"]);
  });

  it("keeps context objects present (never drops them)", () => {
    expect(navigatorOrder(mixed)).toHaveLength(3);
  });

  it("filters by type", () => {
    const typed = [
      obj({ id: "e", type: "equation" }),
      obj({ id: "f", type: "figure" }),
      obj({ id: "t", type: "table" }),
      obj({ id: "c", type: "claim" }),
    ];
    expect(applyTypeFilter(typed, "all")).toHaveLength(4);
    expect(applyTypeFilter(typed, "equation").map((o) => o.id)).toEqual(["e"]);
    expect(applyTypeFilter(typed, "figure").map((o) => o.id)).toEqual(["f"]);
    expect(applyTypeFilter(typed, "table").map((o) => o.id)).toEqual(["t"]);
    expect(applyTypeFilter(typed, "claim").map((o) => o.id)).toEqual(["c"]);
  });
});
