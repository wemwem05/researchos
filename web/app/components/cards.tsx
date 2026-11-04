"use client";

import Link from "next/link";
import { stageLabel, type PaperStatus } from "../lib/workspace";
import type { Finding } from "../lib/project";

export interface PaperCardPaper {
  paper_id: string;
  title: string;
  added_order: number;
  n_objects: number;
  n_localized: number;
}

export function PaperCard({
  projectId,
  paper,
  status,
  findingsCount,
}: {
  projectId: string;
  paper: PaperCardPaper;
  status?: PaperStatus;
  findingsCount: number;
}) {
  const stale = status?.relevance_stale;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-5">
      <div className="min-w-0 flex-1 basis-64">
        <h3 className="line-clamp-2 font-serif text-base leading-snug text-slate-900">
          {paper.title}
        </h3>
        <p className="mt-1.5 text-xs text-slate-500">
          {paper.n_objects} objects · {paper.n_localized} localized ·{" "}
          {findingsCount} finding{findingsCount === 1 ? "" : "s"}
          {stale && (
            <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-900">
              relevance updating
            </span>
          )}
        </p>
      </div>
      <Link
        href={`/projects/${projectId}/read/${paper.paper_id}`}
        className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
      >
        Open reader
      </Link>
    </div>
  );
}

export function PendingPaperCard({
  status,
  removing,
  onRemove,
}: {
  status: PaperStatus;
  removing: boolean;
  onRemove: () => void;
}) {
  const st = status.status;
  const removable = st === "failed" || st === "cancelled" || st === "incomplete";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dashed border-slate-300 bg-white p-5">
      <div className="min-w-0">
        <div className="text-sm font-semibold text-slate-900">
          {st === "processing" && "Analyzing upload…"}
          {st === "awaiting_review" && "Waiting on your review"}
          {st === "failed" && "Processing failed"}
          {st === "cancelled" && "Processing cancelled"}
          {st === "incomplete" && "Upload staged"}
        </div>
        <p className="mt-1 text-[13px] text-slate-500">
          {st === "processing" && (
            <>
              {stageLabel(status.stage ?? "")}
              {status.stage_detail ? ` — ${status.stage_detail}` : ""}
            </>
          )}
          {st === "awaiting_review" &&
            "The agent paused with a question — answer it in Activity."}
          {st === "failed" && (
            <span className="text-red-700">{status.last_error}</span>
          )}
          {st === "cancelled" && "This upload was cancelled before it finished."}
          {st === "incomplete" && "Staged but never processed."}
        </p>
      </div>
      {removable && (
        <button
          type="button"
          onClick={onRemove}
          disabled={removing}
          className="shrink-0 rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-red-300 hover:text-red-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {removing ? "Removing…" : "Remove"}
        </button>
      )}
    </div>
  );
}

export function FindingCard({
  href,
  id,
  statement,
  source,
}: {
  href: string;
  id: string;
  statement: string;
  source: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">
      <p className="text-sm leading-relaxed text-slate-900">{statement}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Link
          href={href}
          className="rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-[11px] text-slate-600 hover:border-indigo-300 hover:text-indigo-700"
        >
          {id} ↗
        </Link>
        <span className="text-xs text-slate-500">{source}</span>
      </div>
    </div>
  );
}

export type { Finding };
