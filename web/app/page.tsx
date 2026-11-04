"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  createProject,
  formatTime,
  listProjects,
  probeWorkspaceApi,
  type ProjectSummary,
} from "./lib/workspace";

export default function Home() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const up = await probeWorkspaceApi().catch(() => false);
    setBackendUp(up);
    if (!up) {
      setProjects(null);
      return;
    }
    try {
      setProjects(await listProjects());
    } catch {
      setProjects(null);
      setBackendUp(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function submit() {
    setCreating(true);
    setError(null);
    try {
      const p = await createProject(title.trim(), question.trim());
      setCreatedId(p.project_id);
      setTitle("");
      setQuestion("");
      setShowForm(false);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Creation failed");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="min-h-screen bg-neutral-50">
      <header className="flex items-center justify-between border-b border-neutral-300 bg-white px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-bold tracking-tight text-neutral-900">ResearchOS</h1>
          <span className="text-xs text-neutral-500">research projects</span>
        </div>
        <Link
          href="/project"
          className="rounded-full border border-neutral-300 px-2.5 py-0.5 text-xs font-medium text-neutral-600 hover:border-neutral-500 hover:text-neutral-900"
        >
          Classic demo project
        </Link>
      </header>

      <main className="mx-auto max-w-3xl px-5 pb-16">
        <div className="mt-6 flex items-start justify-between gap-4">
          <div>
            <h2 className="font-serif text-2xl leading-snug text-neutral-900">
              Your research projects
            </h2>
            <p className="mt-1 text-sm text-neutral-600">
              Each project keeps one research question across its papers, with grounded
              findings, cross-paper reasoning, and auditable agent runs.
            </p>
          </div>
          {backendUp && !showForm && (
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="shrink-0 rounded-full bg-neutral-900 px-4 py-1.5 text-sm font-semibold text-white hover:bg-neutral-700"
            >
              New research project
            </button>
          )}
        </div>

        {backendUp === false && (
          <div className="mt-6 rounded-md border border-amber-300 bg-amber-50 p-4">
            <div className="text-sm font-semibold text-amber-950">
              Workspace backend not running
            </div>
            <p className="mt-1 text-[13px] leading-relaxed text-neutral-700">
              Start it together with the frontend from the repository root:
            </p>
            <p className="mt-1 font-mono text-xs text-neutral-900">./dev.sh</p>
            <p className="mt-1 text-[13px] text-neutral-600">
              or serve the backend alone with{" "}
              <span className="font-mono text-xs">agent.py serve-all</span>. The classic
              staged demo stays available below.
            </p>
            <button
              type="button"
              onClick={refresh}
              className="mt-2 rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500"
            >
              Retry connection
            </button>
          </div>
        )}

        {showForm && (
          <div className="mt-6 rounded-md border border-neutral-200 bg-white p-4">
            <div className="text-sm font-semibold text-neutral-900">New research project</div>
            <label className="mt-3 block">
              <span className="text-xs font-medium text-neutral-500">Project title</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g. Transport Geometry Investigation"
                className="mt-1 w-full rounded border border-neutral-300 bg-white p-2 text-sm text-neutral-900 placeholder:text-neutral-400"
              />
            </label>
            <label className="mt-3 block">
              <span className="text-xs font-medium text-neutral-500">Research question</span>
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="e.g. Can alternative transport costs improve non-rigid shape matching?"
                rows={3}
                className="mt-1 w-full rounded border border-neutral-300 bg-white p-2 text-sm text-neutral-900 placeholder:text-neutral-400"
              />
            </label>
            <p className="mt-2 text-xs text-neutral-500">
              Creates a persistent empty project — no PDF upload required yet.
            </p>
            {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={submit}
                disabled={creating || !title.trim() || !question.trim()}
                className="rounded-full bg-neutral-900 px-4 py-1.5 text-sm font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {creating ? "Creating…" : "Create project"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowForm(false);
                  setError(null);
                }}
                className="rounded-full border border-neutral-300 px-4 py-1.5 text-sm text-neutral-700 hover:border-neutral-500"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {createdId && (
          <div className="mt-6 rounded-md border border-green-300 bg-green-50 p-4">
            <p className="text-sm text-green-900">
              Project created.{" "}
              <Link href={`/projects/${createdId}`} className="font-semibold underline">
                Open it to add papers →
              </Link>
            </p>
          </div>
        )}

        {backendUp && projects === null && (
          <p className="mt-6 text-sm text-neutral-500">Loading projects…</p>
        )}

        {projects && projects.length === 0 && (
          <div className="mt-6 rounded-md border border-neutral-200 bg-white p-6 text-center">
            <p className="font-serif text-lg text-neutral-900">No projects yet</p>
            <p className="mt-1 text-sm text-neutral-600">
              Create your first research project above, then add papers to it.
            </p>
          </div>
        )}

        {projects && projects.length > 0 && (
          <div className="mt-6 flex flex-col gap-3">
            {projects.map((p) => (
              <Link
                key={p.project_id}
                href={`/projects/${p.project_id}`}
                className="rounded-md border border-neutral-200 bg-white p-4 hover:border-neutral-400"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="font-serif text-lg leading-snug text-neutral-900">
                      {p.title}
                    </h3>
                    <p className="mt-0.5 truncate text-[13px] italic text-neutral-600">
                      {p.research_question}
                    </p>
                    <p className="mt-1.5 text-xs text-neutral-500">
                      {p.n_papers} papers · {p.n_findings} findings ·{" "}
                      {p.n_connections} connections · {p.n_audits} audits ·{" "}
                      {p.n_verifications} verifications
                    </p>
                  </div>
                  <span className="shrink-0 rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white">
                    Open
                  </span>
                </div>
                <p className="mt-1 font-mono text-[11px] text-neutral-400">
                  {p.project_id} · question v{p.question_version} · updated{" "}
                  {formatTime(p.updated_at)}
                </p>
              </Link>
            ))}
          </div>
        )}

        <div className="mt-10 border-t border-neutral-200 pt-4">
          <p className="text-xs text-neutral-500">
            Staged classic demo (static fixtures, always available):{" "}
            <Link href="/project" className="underline">
              demo project
            </Link>{" "}
            ·{" "}
            <Link href="/read/eot-maps" className="underline">
              EOT reader
            </Link>
          </p>
        </div>
      </main>
    </div>
  );
}
