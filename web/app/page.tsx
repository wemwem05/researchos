"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  createProject,
  formatTime,
  listProjects,
  probeWorkspaceApi,
  type ProjectSummary,
} from "./lib/workspace";
import AppShell from "./components/AppShell";
import Sidebar from "./components/Sidebar";
import { EmptyState, PRIMARY_BUTTON } from "./components/ui";

export default function Home() {
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const up = await probeWorkspaceApi().catch(() => false);
    setBackendUp(up);
    if (up) {
      try {
        setProjects(await listProjects());
      } catch {
        setBackendUp(false);
      }
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function doCreate() {
    if (!title.trim() || !question.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const p = await createProject(title.trim(), question.trim());
      router.push(`/projects/${p.project_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Creation failed");
    } finally {
      setCreating(false);
    }
  }

  return (
    <AppShell sidebar={<Sidebar />}>
      <div className="flex flex-col gap-8">
        <div className="pt-4">
          <h1 className="text-4xl font-bold tracking-tight text-slate-900">
            ResearchOS
          </h1>
          <p className="mt-2 font-serif text-xl text-slate-600">
            From reading papers to investigating science.
          </p>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-500">
            Discover literature, understand scientific papers, connect
            evidence, audit claims and test computational hypotheses — every
            artifact linked to your research question and its evidence.
          </p>
          {backendUp && (
            <div className="mt-4">
              {!showForm ? (
                <button
                  type="button"
                  onClick={() => setShowForm(true)}
                  className={PRIMARY_BUTTON}
                >
                  New research project
                </button>
              ) : (
                <div className="max-w-xl rounded-xl border border-slate-200 bg-white p-6">
                  <label className="block">
                    <span className="text-xs font-medium text-slate-500">
                      Project title
                    </span>
                    <input
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                      placeholder="Transport Geometry Investigation"
                      className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm text-slate-900"
                    />
                  </label>
                  <label className="mt-3 block">
                    <span className="text-xs font-medium text-slate-500">
                      Research question
                    </span>
                    <textarea
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      rows={3}
                      placeholder="Can alternative transport costs improve non-rigid shape matching?"
                      className="mt-1 w-full rounded-lg border border-slate-300 bg-white p-2 text-sm text-slate-900"
                    />
                  </label>
                  {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      onClick={doCreate}
                      disabled={creating || !title.trim() || !question.trim()}
                      className={PRIMARY_BUTTON}
                    >
                      {creating ? "Creating…" : "Create project"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setShowForm(false)}
                      className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:border-slate-400"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {backendUp === false && (
          <div className="rounded-xl border border-amber-300 bg-amber-50 p-6">
            <p className="text-sm font-medium text-amber-950">
              Workspace backend is not running.
            </p>
            <p className="mt-1 font-mono text-xs text-amber-900">
              ./dev.sh
            </p>
            <button
              type="button"
              onClick={refresh}
              className="mt-3 rounded-lg border border-amber-300 bg-white px-4 py-2 text-sm font-medium text-amber-900 hover:border-amber-400"
            >
              Retry connection
            </button>
          </div>
        )}

        {backendUp && projects === null && (
          <p className="text-sm text-slate-500">Loading projects…</p>
        )}

        {projects && projects.length === 0 && (
          <EmptyState
            title="No projects yet — start your first"
            body="Pose a research question, then upload papers you already have or let the agent discover relevant literature."
          />
        )}

        {projects && projects.length > 0 && (
          <div>
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-500">
              Your projects ({projects.length})
            </h2>
            <div className="grid gap-4 md:grid-cols-2">
              {projects.map((p) => (
                <Link
                  key={p.project_id}
                  href={`/projects/${p.project_id}`}
                  className="group rounded-xl border border-slate-200 bg-white p-5 hover:border-indigo-300 hover:shadow-sm"
                >
                  <div className="flex items-start justify-between gap-3">
                    <h3 className="line-clamp-1 font-semibold text-slate-900">
                      {p.title || p.project_id}
                    </h3>
                    <span className="shrink-0 text-xs text-slate-400">
                      {formatTime(p.updated_at)}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[13px] leading-relaxed text-slate-500">
                    {p.research_question}
                  </p>
                  <p className="mt-3 text-xs font-medium text-slate-500">
                    {p.n_papers} paper{p.n_papers === 1 ? "" : "s"} ·{" "}
                    {p.n_findings} finding{p.n_findings === 1 ? "" : "s"} ·{" "}
                    question v{p.question_version}
                  </p>
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
