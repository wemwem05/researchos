"use client";

import { EmptyState, PageHeader, PRIMARY_BUTTON } from "./ui";
import { PaperCard, PendingPaperCard, type PaperCardPaper } from "./cards";
import { formatBytes, type PaperStatus } from "../lib/workspace";
import { findingsForPaper } from "../lib/project";
import type { OverviewProject } from "./OverviewTab";

export default function PapersTab({
  project,
  projectId,
  status,
  unattached,
  files,
  uploading,
  uploadError,
  removingId,
  removeError,
  onFiles,
  onUpload,
  onRemove,
}: {
  project: OverviewProject;
  projectId: string;
  status: Record<string, PaperStatus>;
  unattached: [string, PaperStatus][];
  files: File[];
  uploading: boolean;
  uploadError: string | null;
  removingId: string | null;
  removeError: string | null;
  onFiles: (f: File[]) => void;
  onUpload: () => void;
  onRemove: (paperId: string) => void;
}) {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Your Papers"
        description="Uploaded and discovered papers live here together. Analysis runs automatically and can take several minutes per paper."
        actions={
          <label className={`${PRIMARY_BUTTON} cursor-pointer`}>
            {uploading ? "Uploading…" : "Upload PDF"}
            <input
              type="file"
              accept="application/pdf,.pdf"
              multiple
              disabled={uploading}
              onChange={(e) => {
                onFiles(Array.from(e.target.files ?? []));
                e.target.value = "";
              }}
              className="hidden"
            />
          </label>
        }
      />

      {files.length > 0 && (
        <div className="rounded-xl border border-indigo-200 bg-indigo-50/60 p-5">
          <div className="text-sm font-semibold text-slate-900">
            Ready to upload
          </div>
          <ul className="mt-2 flex flex-col gap-1">
            {files.map((f) => (
              <li key={f.name} className="text-[13px] text-slate-600">
                {f.name} · {formatBytes(f.size)}
              </li>
            ))}
          </ul>
          {uploadError && (
            <p className="mt-2 text-xs text-red-700">{uploadError}</p>
          )}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={onUpload}
              disabled={uploading}
              className={PRIMARY_BUTTON}
            >
              {uploading
                ? "Uploading…"
                : `Upload ${files.length} PDF${files.length === 1 ? "" : "s"}`}
            </button>
            <button
              type="button"
              onClick={() => onFiles([])}
              disabled={uploading}
              className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:border-slate-400 disabled:opacity-40"
            >
              Clear
            </button>
          </div>
        </div>
      )}
      {files.length === 0 && uploadError && (
        <p className="text-xs text-red-700">{uploadError}</p>
      )}

      {project.papers.length === 0 && unattached.length === 0 ? (
        <EmptyState
          title="No papers yet"
          body="Upload your first PDF above, or let the agent find literature for this question in Discover."
        />
      ) : (
        <div className="flex flex-col gap-3">
          {project.papers.map((p) => (
            <PaperCard
              key={p.paper_id}
              projectId={projectId}
              paper={p as PaperCardPaper}
              status={status[p.paper_id]}
              findingsCount={findingsForPaper(project, p.paper_id).length}
            />
          ))}
          {removeError && <p className="text-xs text-red-700">{removeError}</p>}
          {unattached.map(([pid, st]) => (
            <PendingPaperCard
              key={pid}
              status={st}
              removing={removingId === pid}
              onRemove={() => onRemove(pid)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
