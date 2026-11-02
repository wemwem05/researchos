"use client";

import type { SciObject } from "../lib/types";
import type { TypeFilter } from "../lib/relevance";

interface Props {
  objects: SciObject[];
  selectedId: string | null;
  filter: TypeFilter;
  onFilter: (filter: TypeFilter) => void;
  onJump: (objectId: string) => void;
}

const FILTERS: { id: TypeFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "equation", label: "Equations" },
  { id: "figure", label: "Figures" },
  { id: "table", label: "Tables" },
];

const DOT: Record<SciObject["research_relevance_level"], string> = {
  high: "bg-indigo-600",
  related: "bg-blue-400",
  context: "bg-neutral-300",
};

export default function ObjectNavigator({ objects, selectedId, filter, onFilter, onJump }: Props) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="Object type filter">
        {FILTERS.map((f) => {
          const active = f.id === filter;
          return (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onFilter(f.id)}
              className={[
                "rounded px-2 py-0.5 text-xs font-medium transition-colors",
                active
                  ? "bg-neutral-900 text-white"
                  : "bg-neutral-100 text-neutral-600 hover:bg-neutral-200",
              ].join(" ")}
            >
              {f.label}
            </button>
          );
        })}
        <span className="ml-2 flex items-center gap-3 text-[11px] text-neutral-500" aria-label="Relevance legend">
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-indigo-600" /> High
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-blue-400" /> Related
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-neutral-300" /> Context
          </span>
        </span>
      </div>
      <nav aria-label="Detected objects" className="flex flex-wrap items-center gap-2">
        {objects.map((o) => {
          const selected = o.id === selectedId;
          const high = o.research_relevance_level === "high";
          return (
            <button
              key={o.id}
              type="button"
              title={`${o.label ?? o.id} — ${o.title}`}
              onClick={() => onJump(o.id)}
              className={[
                "flex items-center gap-1.5 rounded-full border px-3 py-1 text-[13px] font-medium transition-colors",
                selected
                  ? "border-indigo-700 bg-indigo-700 text-white"
                  : high
                    ? "border-indigo-300 bg-indigo-50 text-indigo-900 hover:border-indigo-500"
                    : "border-neutral-300 bg-white text-neutral-700 hover:border-blue-500 hover:text-blue-800",
              ].join(" ")}
            >
              <span className={["inline-block h-1.5 w-1.5 rounded-full", DOT[o.research_relevance_level]].join(" ")} />
              {o.label ?? o.id}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
