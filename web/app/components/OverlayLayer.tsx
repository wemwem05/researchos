"use client";

import type { BoundingBox, RelevanceLevel } from "../lib/types";
import type { LensMode } from "../lib/relevance";
import { overlayTreatment } from "../lib/relevance";

export interface OverlayBox {
  objectId: string;
  label: string;
  level: RelevanceLevel;
  bbox: BoundingBox;
}

interface Props {
  boxes: OverlayBox[];
  selectedId: string | null;
  mode: LensMode;
  onSelect: (objectId: string) => void;
}

/** Level colors (spec: no red/green — those are reserved for verification). */
const LEVEL_TEXT: Record<RelevanceLevel, string> = {
  high: "text-indigo-100",
  related: "text-blue-100",
  context: "text-neutral-200",
};

function boxClass(level: RelevanceLevel, treatment: string, selected: boolean): string {
  if (selected) {
    return level === "high"
      ? "border-indigo-600 bg-indigo-500/[0.16] ring-1 ring-indigo-600/50"
      : level === "related"
        ? "border-blue-600 bg-blue-500/[0.16] ring-1 ring-blue-600/50"
        : "border-neutral-500 bg-neutral-400/[0.18] ring-1 ring-neutral-500/60";
  }
  switch (treatment) {
    case "accent": // high: persistent accent
      return "border-indigo-500/70 bg-indigo-500/[0.10] hover:bg-indigo-500/[0.18]";
    case "subtle": // related in Show All: faint blue
      return "border-blue-500/30 bg-blue-500/[0.05] hover:border-blue-600/60 hover:bg-blue-500/[0.14]";
    case "faint": // context in Show All: neutral gray
      return "border-neutral-400/40 bg-neutral-400/[0.06] hover:border-neutral-500/70 hover:bg-neutral-400/[0.14]";
    default: // hidden (focus-mode related/context): invisible until hover
      return "border-transparent bg-transparent hover:border-current hover:bg-black/[0.04]";
  }
}

/** Clickable object overlays. Percent positioning keeps boxes aligned to the
 *  rendered page at any size. Styling follows the Research Lens mode. */
export default function OverlayLayer({ boxes, selectedId, mode, onSelect }: Props) {
  return (
    <div className="pointer-events-none absolute inset-0 z-10">
      {boxes.map((box) => {
        const selected = box.objectId === selectedId;
        const treatment = overlayTreatment(box.level, mode, selected);
        const chipBelow = box.bbox.y_min < 90;
        return (
          <button
            key={box.objectId}
            type="button"
            title={box.label}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(box.objectId);
            }}
            style={{
              left: `${box.bbox.x_min / 10}%`,
              top: `${box.bbox.y_min / 10}%`,
              width: `${(box.bbox.x_max - box.bbox.x_min) / 10}%`,
              height: `${(box.bbox.y_max - box.bbox.y_min) / 10}%`,
              color:
                box.level === "high"
                  ? "rgb(79 70 229)"
                  : box.level === "related"
                    ? "rgb(37 99 235)"
                    : "rgb(115 115 115)",
            }}
            className={[
              "group pointer-events-auto absolute cursor-pointer rounded-[3px] border transition-colors",
              boxClass(box.level, treatment, selected),
            ].join(" ")}
          >
            <span
              className={[
                "absolute left-0 whitespace-nowrap rounded bg-neutral-900 px-2 py-0.5 text-[11px] font-medium shadow",
                LEVEL_TEXT[box.level],
                chipBelow ? "top-full mt-1" : "-top-7",
                selected || treatment === "accent" ? "block" : "hidden group-hover:block",
              ].join(" ")}
            >
              {box.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
