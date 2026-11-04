"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  listProjects,
  type ProjectSummary,
} from "../lib/workspace";

export type ProjectTab =
  | "overview"
  | "papers"
  | "discover"
  | "evidence"
  | "activity";

const SECTIONS: { id: ProjectTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "papers", label: "Papers" },
  { id: "discover", label: "Discover" },
  { id: "evidence", label: "Evidence" },
  { id: "activity", label: "Activity" },
];

function tabHref(projectId: string, tab: ProjectTab): string {
  return tab === "overview"
    ? `/projects/${projectId}`
    : `/projects/${projectId}?tab=${tab}`;
}

function ProjectSwitcher({
  currentId,
  currentTitle,
}: {
  currentId: string;
  currentTitle: string;
}) {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!expanded || projects !== null) return;
    let cancelled = false;
    listProjects()
      .then((list: ProjectSummary[]) => {
        if (!cancelled) setProjects(list);
      })
      .catch(() => {
        if (!cancelled) setProjects([]);
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, projects]);

  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
      <button
        type="button"
        onClick={() => setExpanded((x) => !x)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2 text-left"
      >
        <span className="min-w-0">
          <span className="block text-[11px] font-medium uppercase tracking-wider text-slate-400">
            Project
          </span>
          <span className="block truncate text-sm font-semibold text-slate-900">
            {currentTitle || currentId}
          </span>
        </span>
        <span className="shrink-0 text-xs text-slate-400">
          {expanded ? "▾" : "▸"}
        </span>
      </button>
      {expanded && (
        <div className="mt-2 flex max-h-48 flex-col gap-0.5 overflow-y-auto border-t border-slate-200 pt-2">
          {projects === null && (
            <span className="px-2 py-1 text-xs text-slate-400">Loading…</span>
          )}
          {(projects ?? []).map((p) => (
            <button
              key={p.project_id}
              type="button"
              disabled={p.project_id === currentId}
              onClick={() => router.push(`/projects/${p.project_id}`)}
              className={`truncate rounded-md px-2 py-1 text-left text-[13px] ${
                p.project_id === currentId
                  ? "font-semibold text-indigo-700"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              {p.title || p.project_id}
            </button>
          ))}
          <Link
            href="/"
            className="mt-1 rounded-md px-2 py-1 text-[13px] font-medium text-indigo-600 hover:bg-indigo-50"
          >
            All projects →
          </Link>
        </div>
      )}
    </div>
  );
}

export default function Sidebar({
  project,
  tab,
  counts,
  attention,
}: {
  project?: { id: string; title: string } | null;
  tab?: ProjectTab;
  counts?: { papers: number; evidence: number };
  attention?: boolean;
}) {
  return (
    <div className="flex h-full flex-col gap-1 p-4">
      <Link
        href="/"
        className="rounded-lg px-2 py-2 text-lg font-bold tracking-tight text-slate-900 hover:bg-slate-100"
      >
        ResearchOS
      </Link>

      <Link
        href="/"
        aria-current={!project ? "page" : undefined}
        className={`mt-2 rounded-lg px-3 py-2 text-sm font-medium ${
          !project
            ? "bg-indigo-50 text-indigo-700"
            : "text-slate-600 hover:bg-slate-100"
        }`}
      >
        Projects
      </Link>

      {project && (
        <>
          <div className="mt-4">
            <ProjectSwitcher currentId={project.id} currentTitle={project.title} />
          </div>
          <nav aria-label="Project sections" className="mt-3 flex flex-col gap-0.5">
            {SECTIONS.map((s) => {
              const active = tab === s.id;
              const count =
                s.id === "papers"
                  ? counts?.papers
                  : s.id === "evidence"
                    ? counts?.evidence
                    : null;
              return (
                <Link
                  key={s.id}
                  href={tabHref(project.id, s.id)}
                  aria-current={active ? "page" : undefined}
                  className={`relative flex items-center justify-between rounded-lg px-3 py-2 text-sm ${
                    active
                      ? "bg-indigo-50 font-semibold text-indigo-700"
                      : "font-medium text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  <span className="flex items-center gap-2">
                    {s.label}
                    {s.id === "activity" && attention && !active && (
                      <span
                        aria-label="Needs attention"
                        className="h-2 w-2 rounded-full bg-amber-500"
                      />
                    )}
                  </span>
                  {count !== null && count !== undefined && count > 0 && (
                    <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[11px] font-semibold text-slate-600">
                      {count}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>
        </>
      )}

      <div className="mt-auto pt-6">
        <Link
          href="/project"
          className="block rounded-lg px-3 py-2 text-[13px] font-medium text-slate-500 hover:bg-slate-100"
        >
          Classic staged demo
        </Link>
        <p className="px-3 pt-1 text-[11px] text-slate-400">
          ResearchOS · local workspace
        </p>
      </div>
    </div>
  );
}
