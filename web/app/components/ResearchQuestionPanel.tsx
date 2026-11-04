"use client";

import type { QuestionPreview } from "../lib/workspace";
import { PRIMARY_BUTTON, SECONDARY_BUTTON } from "./ui";

/**
 * The research-question panel: visually distinct, restrained. Owns the full
 * edit → preview → confirm flow through callbacks (state stays in the page).
 */
export default function ResearchQuestionPanel({
  question,
  version,
  editing,
  draftTitle,
  draftQuestion,
  preview,
  busy,
  error,
  onStartEdit,
  onCancelEdit,
  onDraftTitle,
  onDraftQuestion,
  onPreview,
  onConfirm,
}: {
  question: string;
  version: number;
  editing: boolean;
  draftTitle: string;
  draftQuestion: string;
  preview: QuestionPreview | null;
  busy: boolean;
  error: string | null;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onDraftTitle: (v: string) => void;
  onDraftQuestion: (v: string) => void;
  onPreview: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold uppercase tracking-wider text-indigo-600">
            Research question · v{version}
          </div>
          {!editing && (
            <p className="mt-2 font-serif text-xl leading-snug text-slate-900">
              {question}
            </p>
          )}
        </div>
        {!editing && (
          <button
            type="button"
            onClick={onStartEdit}
            className="shrink-0 rounded-lg border border-indigo-200 bg-white px-3 py-1 text-xs font-medium text-indigo-700 hover:border-indigo-300"
          >
            Edit
          </button>
        )}
      </div>

      {editing && (
        <div className="mt-4">
          <label className="block">
            <span className="text-xs font-medium text-slate-500">Project title</span>
            <input
              value={draftTitle}
              onChange={(e) => onDraftTitle(e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm text-slate-900"
            />
          </label>
          <label className="mt-3 block">
            <span className="text-xs font-medium text-slate-500">
              Research question
            </span>
            <textarea
              value={draftQuestion}
              onChange={(e) => onDraftQuestion(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm text-slate-900"
            />
          </label>
          {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
          {!preview ? (
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={onPreview}
                disabled={busy || !draftQuestion.trim()}
                className={PRIMARY_BUTTON}
              >
                {busy ? "Loading…" : "Preview changes"}
              </button>
              <button
                type="button"
                onClick={onCancelEdit}
                className={SECONDARY_BUTTON}
              >
                Cancel
              </button>
            </div>
          ) : (
            <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-4">
              <p className="text-[13px] font-medium text-amber-950">
                Changing to question v{preview.to_version} will:
              </p>
              <ul className="mt-1 list-disc pl-5 text-xs leading-relaxed text-slate-700">
                <li>
                  Archive {preview.conclusions_to_archive.findings} findings,{" "}
                  {preview.conclusions_to_archive.connections} connections as
                  historical (kept, never shown as current)
                </li>
                <li>Re-score relevance for {preview.n_papers} paper(s)</li>
                <li>Rebuild findings and connections for the new question</li>
                <li>Preserve PDFs, inventory, localizations, audits, and runs</li>
              </ul>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={onConfirm}
                  disabled={busy}
                  className={PRIMARY_BUTTON}
                >
                  {busy ? "Updating…" : "Confirm & recompute"}
                </button>
                <button
                  type="button"
                  onClick={onCancelEdit}
                  className={SECONDARY_BUTTON}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
