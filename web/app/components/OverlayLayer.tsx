"use client";

import type { BoundingBox } from "../lib/types";

export interface OverlayBox {
  objectId: string;
  label: string;
  bbox: BoundingBox;
}

interface Props {
  boxes: OverlayBox[];
  selectedId: string | null;
  onSelect: (objectId: string) => void;
}

/** Clickable object overlays. Percent positioning keeps boxes aligned to the
 *  rendered page at any size. Subtle by default; hover and selection highlight. */
export default function OverlayLayer({ boxes, selectedId, onSelect }: Props) {
  return (
    <div className="pointer-events-none absolute inset-0 z-10">
      {boxes.map((box) => {
        const selected = box.objectId === selectedId;
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
            }}
            className={[
              "group pointer-events-auto absolute cursor-pointer rounded-[3px] border transition-colors",
              selected
                ? "border-teal-700 bg-teal-600/[0.16] ring-1 ring-teal-700/50"
                : "border-teal-700/0 bg-teal-700/[0.06] hover:border-teal-700/60 hover:bg-teal-600/[0.14]",
            ].join(" ")}
          >
            <span
              className={[
                "absolute left-0 whitespace-nowrap rounded bg-neutral-900 px-2 py-0.5 text-[11px] font-medium text-white shadow",
                chipBelow ? "top-full mt-1" : "-top-7",
                selected ? "block" : "hidden group-hover:block",
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
