// Research Lens helpers: relevance levels, display labels, and Focus/Show All
// visibility rules. Pure functions (no React) so they are unit-testable.
//
// Level cutoffs mirror prototype/relevance.py — keep in sync when tuning.

import type { RelevanceLevel, SciObject } from "./types";

export type LensMode = "focus" | "all";
export type TypeFilter = "all" | "equation" | "figure" | "table";

export const HIGH_THRESHOLD = 0.75;
export const RELATED_THRESHOLD = 0.4;

export function levelForScore(score: number): RelevanceLevel {
  if (score >= HIGH_THRESHOLD) return "high";
  if (score >= RELATED_THRESHOLD) return "related";
  return "context";
}

export function importanceLabel(score: number): "High" | "Medium" | "Low" {
  if (score >= 0.66) return "High";
  if (score >= 0.33) return "Medium";
  return "Low";
}

export function relevanceLabel(level: RelevanceLevel): "High" | "Related" | "Context" {
  return level === "high" ? "High" : level === "related" ? "Related" : "Context";
}

/** Overlay treatment per level and mode. "hidden" still renders a transparent
 *  click target so the object stays interactive; only "none" skips rendering. */
export type OverlayTreatment = "accent" | "subtle" | "faint" | "hidden";

export function overlayTreatment(
  level: RelevanceLevel,
  mode: LensMode,
  selected: boolean
): OverlayTreatment {
  if (selected) return "accent";
  if (mode === "all") {
    return level === "high" ? "accent" : level === "related" ? "subtle" : "faint";
  }
  // Focus mode: high stays visible, related keeps a faint outline,
  // context has no persistent highlight (still clickable + hoverable).
  return level === "high" ? "accent" : level === "related" ? "subtle" : "hidden";
}

/** Navigator entries: every explainable (localized) object, high first. */
export function navigatorOrder(objects: SciObject[]): SciObject[] {
  const rank = { high: 0, related: 1, context: 2 } as const;
  return [...objects].sort(
    (a, b) =>
      rank[a.research_relevance_level] - rank[b.research_relevance_level] ||
      (a.page ?? 1e9) - (b.page ?? 1e9)
  );
}

export function applyTypeFilter(objects: SciObject[], filter: TypeFilter): SciObject[] {
  if (filter === "all") return objects;
  return objects.filter((o) => o.type === filter);
}
