"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  changeQuestion,
  dedupeQuestionHistory,
  deletePaper,
  fetchProjectJobs,
  fetchProjectView,
  fetchQuestionPreview,
  formatBytes,
  formatTime,
  isJobActive,
  requestRebuild,
  retryJob,
  stageLabel,
  updateProjectTitle,
  uploadPaper,
  WorkspaceError,
  type PaperStatus,
  type ProjectView,
  type QuestionPreview,
  type WorkspaceJob,
} from "../../lib/workspace";
import {
  AGENT_API_BASE,
  type AgentRunRecord,
  type AgentTelemetrySummary,
} from "../../lib/agent";
import {
  AgentRunSection,
  ModelUsageSection,
  ReviewCard,
} from "../../components/AgentPanel";
import AuditEvidence from "../../components/AuditEvidence";
import DiscoverySection from "../../components/DiscoverySection";
import VerificationEvidence from "../../components/VerificationEvidence";
import type { ScientificAuditView } from "../../lib/audit";
import type { VerificationEvidenceView } from "../../lib/verification";
import type {
  Connection,
  ConnectionType,
  Finding,
  ProjectData,
} from "../../lib/project";
import {
  CORPUS_SCOPE_PREFIX,
  endpointLabel,
  findingsForPaper,
  groupConnectionsByType,
  paperById,
} from "../../lib/project";

const TYPE_BADGE: Record<ConnectionType, string> = {
  supports: "bg-green-100 text-green-900",
  contradicts: "bg-red-100 text-red-900",
  extends: "bg-indigo-100 text-indigo-900",
  overlaps_with: "bg-neutral-200 text-neutral-800",
  uses_same_method: "bg-neutral-200 text-neutral-800",
  uses_different_assumption: "bg-amber-100 text-amber-900",
  leaves_open: "bg-amber-100 text-amber-900",
};

interface LiveProject extends ProjectData {
  title: string;
  question_version: number;
  question_history: {
    version: number;
    question: string;
    created_at: string;
    status: "current" | "superseded";
    findings: unknown[];
    connections: unknown[];
    open_questions: unknown[];
    novelty_risks: unknown[];
  }[];
  verifications: VerificationEvidenceView[];
  audits: ScientificAuditView[];
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-neutral-500">
        {title}
      </h2>
      <div className="mt-3 flex flex-col gap-3">{children}</div>
    </section>
  );
}

function findingLink(projectId: string, f: Finding): string {
  const obj = f.object_ids[0];
  return `/projects/${projectId}/read/${f.paper_id}${obj ? `?select=${encodeURIComponent(obj)}` : ""}`;
}

function JobRow({
  job,
  onRetry,
}: {
  job: WorkspaceJob;
  onRetry: (jobId: string) => void;
}) {
  const [retrying, setRetrying] = useState(false);
  const active = isJobActive(job);
  const retryable = (job.result?.retryable as boolean | undefined) !== false;
  return (
    <div className="rounded-md border border-neutral-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="font-mono text-xs text-neutral-500">{job.job_id}</span>
          <span className="ml-2 text-[13px] font-medium text-neutral-900">
            {job.job_type === "ingest_paper"
              ? `Adding paper ${job.paper_id}`
              : job.job_type === "discover_papers"
                ? "Discovering literature"
                : job.job_type === "import_discovery"
                  ? "Importing discovered papers"
                  : "Recomputing for new question"}
          </span>
          <p className="mt-0.5 text-xs text-neutral-600">
            {active ? (
              <>
                {stageLabel(job.stage)}
                {job.stage_detail && job.stage_detail !== stageLabel(job.stage)
                  ? ` — ${job.stage_detail}`
                  : ""}
              </>
            ) : job.state === "COMPLETED" ? (
              "Completed"
            ) : job.state === "FAILED" ? (
              <span className="text-red-700">Failed: {job.last_error}</span>
            ) : (
              "Cancelled"
            )}
          </p>
        </div>
        {job.state === "FAILED" &&
          (retryable ? (
            <button
              type="button"
              disabled={retrying || job.attempts >= job.max_attempts}
              onClick={async () => {
                setRetrying(true);
                try {
                  await onRetry(job.job_id);
                } finally {
                  setRetrying(false);
                }
              }}
              className="shrink-0 rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {retrying
                ? "Retrying…"
                : job.attempts >= job.max_attempts
                  ? "Retries exhausted"
                  : "Retry"}
            </button>
          ) : (
            <span className="shrink-0 text-xs text-neutral-500">
              Retry won&apos;t help for this error
            </span>
          ))}
      </div>
    </div>
  );
}

export default function LiveProjectPage() {
  const params = useParams();
  const projectId = params.id as string;
  const [view, setView] = useState<ProjectView | null>(null);
  const [jobs, setJobs] = useState<WorkspaceJob[]>([]);
  const [missing, setMissing] = useState(false);
  const [backendDown, setBackendDown] = useState(false);
  const [tick, setTick] = useState(0);

  // Question editing
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftQuestion, setDraftQuestion] = useState("");
  const [preview, setPreview] = useState<QuestionPreview | null>(null);
  const [qBusy, setQBusy] = useState(false);
  const [qError, setQError] = useState<string | null>(null);

  // Upload
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildError, setRebuildError] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [jobActionError, setJobActionError] = useState<string | null>(null);

  // Agent
  const [agentRun, setAgentRun] = useState<AgentRunRecord | null>(null);
  const [agentSummary, setAgentSummary] = useState<AgentTelemetrySummary | null>(null);

  const refresh = useCallback(async () => {
    try {
      const v = await fetchProjectView(projectId);
      setView(v);
      setBackendDown(false);
      const jl = await fetchProjectJobs(projectId).catch(() => []);
      setJobs(jl);
      const runsRes = await fetch(`${AGENT_API_BASE}/api/projects/${projectId}/runs`).catch(
        () => null
      );
      if (runsRes && runsRes.ok) {
        const body = (await runsRes.json()) as {
          data: { runs: { thread_id: string }[] };
        };
        const list = body.data?.runs ?? [];
        if (list.length > 0) {
          const detail = await fetch(
            `${AGENT_API_BASE}/api/projects/${projectId}/runs/${list[list.length - 1].thread_id}`
          ).then((r) => r.json());
          const rec = (detail.data ?? detail) as AgentRunRecord;
          if (Array.isArray(rec.transitions)) setAgentRun(rec);
        }
      }
      const telRes = await fetch(
        `${AGENT_API_BASE}/api/projects/${projectId}/telemetry`
      ).catch(() => null);
      if (telRes && telRes.ok) {
        const body = (await telRes.json()) as {
          data: { summary: AgentTelemetrySummary };
        };
        if (body.data?.summary) setAgentSummary(body.data.summary);
      }
    } catch (e) {
      if (e instanceof WorkspaceError && e.status === 404) setMissing(true);
      else setBackendDown(true);
    }
  }, [projectId]);

  useEffect(() => {
    refresh();
  }, [refresh, tick]);

  const project = useMemo(
    () => (view ? (view.project as unknown as LiveProject) : null),
    [view]
  );
  const status = view?.paper_status ?? {};

  const activeJobs = jobs.filter(isJobActive);
  const rescoreActive = activeJobs.some(
    (j) => j.job_type === "rescore_question" || j.job_type === "rebuild_findings"
  );
  const runActive =
    agentRun && (agentRun.status === "running" || agentRun.status === "awaiting_human");

  useEffect(() => {
    if (!view || backendDown || missing) return;
    if (activeJobs.length === 0 && !runActive) return;
    const id = setTimeout(() => setTick((x) => x + 1), 3000);
    return () => clearTimeout(id);
  }, [view, backendDown, missing, activeJobs.length, runActive]);

  const grouped = useMemo(
    () =>
      project
        ? groupConnectionsByType(project.connections)
        : new Map<ConnectionType, Connection[]>(),
    [project]
  );

  async function loadPreview() {
    setQBusy(true);
    setQError(null);
    try {
      setPreview(await fetchQuestionPreview(projectId));
    } catch (e) {
      setQError(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setQBusy(false);
    }
  }

  async function confirmQuestion() {
    if (!draftQuestion.trim()) return;
    setQBusy(true);
    setQError(null);
    try {
      if (draftTitle.trim() && project && draftTitle.trim() !== project.title) {
        await updateProjectTitle(projectId, draftTitle.trim());
      }
      const res = await changeQuestion(projectId, draftQuestion.trim(), true);
      if ("needs_confirmation" in res) {
        setQError("Server asked for confirmation again — retry.");
      } else {
        setEditing(false);
        setPreview(null);
        await refresh();
      }
    } catch (e) {
      setQError(e instanceof Error ? e.message : "Question update failed");
    } finally {
      setQBusy(false);
    }
  }

  async function doUpload() {
    if (files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const f of files) {
        await uploadPaper(projectId, f);
      }
      setFiles([]);
      await refresh();
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function doRetry(jobId: string) {
    setJobActionError(null);
    try {
      await retryJob(jobId);
      await refresh();
    } catch (e) {
      setJobActionError(e instanceof Error ? e.message : "Retry failed");
    }
  }

  async function doRebuild() {
    setRebuilding(true);
    setRebuildError(null);
    try {
      await requestRebuild(projectId);
      await refresh();
    } catch (e) {
      setRebuildError(e instanceof Error ? e.message : "Rebuild failed");
    } finally {
      setRebuilding(false);
    }
  }

  async function doRemove(paperId: string) {
    setRemovingId(paperId);
    setRemoveError(null);
    try {
      await deletePaper(projectId, paperId);
      await refresh();
    } catch (e) {
      setRemoveError(e instanceof Error ? e.message : "Removal failed");
    } finally {
      setRemovingId(null);
    }
  }

  if (missing) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 text-sm text-neutral-600">
        <p>Unknown project “{projectId}”.</p>
        <Link href="/" className="underline">
          Back to projects
        </Link>
      </div>
    );
  }
  if (backendDown) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 text-sm text-neutral-600">
        <p>Workspace backend not running — start it with ./dev.sh</p>
        <div className="flex gap-3">
          <button type="button" onClick={refresh} className="underline">
            Retry
          </button>
          <Link href="/" className="underline">
            Back to projects
          </Link>
        </div>
      </div>
    );
  }
  if (!project || !view) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-neutral-500">
        Loading project…
      </div>
    );
  }

  const history = dedupeQuestionHistory(
    (project.question_history ?? []).filter((h) => h.status === "superseded")
  );
  const failedJobs = jobs.filter((j) => j.state === "FAILED").slice(0, 3);

  return (
    <div className="min-h-screen bg-neutral-50">
      <header className="flex items-center justify-between border-b border-neutral-300 bg-white px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-bold tracking-tight text-neutral-900">ResearchOS</h1>
          <span className="text-xs text-neutral-500">research project</span>
        </div>
        <Link
          href="/"
          className="rounded-full border border-neutral-300 px-2.5 py-0.5 text-xs font-medium text-neutral-600 hover:border-neutral-500 hover:text-neutral-900"
        >
          Projects
        </Link>
      </header>

      <main className="mx-auto max-w-3xl px-5 pb-16">
        <div className="mt-6 rounded-md border border-neutral-200 bg-white p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-xs font-medium text-neutral-500">
                {project.title || projectId} · question v{project.question_version ?? 1}
              </div>
              <p className="mt-1 font-serif text-xl leading-snug text-neutral-900">
                {project.research_question}
              </p>
              <p className="mt-2 text-[13px] text-neutral-600">
                {project.papers.length} papers · {project.findings.length} findings ·{" "}
                {project.connections.length} connections · {project.open_questions.length}{" "}
                open questions · {project.novelty_risks.length} novelty risks
              </p>
            </div>
            {!editing && (
              <button
                type="button"
                onClick={() => {
                  setDraftTitle(project.title || "");
                  setDraftQuestion(project.research_question);
                  setPreview(null);
                  setQError(null);
                  setEditing(true);
                }}
                className="shrink-0 rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500"
              >
                Edit
              </button>
            )}
          </div>

          {editing && (
            <div className="mt-4 border-t border-neutral-100 pt-4">
              <label className="block">
                <span className="text-xs font-medium text-neutral-500">Project title</span>
                <input
                  value={draftTitle}
                  onChange={(e) => setDraftTitle(e.target.value)}
                  className="mt-1 w-full rounded border border-neutral-300 bg-white p-2 text-sm text-neutral-900"
                />
              </label>
              <label className="mt-3 block">
                <span className="text-xs font-medium text-neutral-500">
                  Research question
                </span>
                <textarea
                  value={draftQuestion}
                  onChange={(e) => setDraftQuestion(e.target.value)}
                  rows={3}
                  className="mt-1 w-full rounded border border-neutral-300 bg-white p-2 text-sm text-neutral-900"
                />
              </label>
              {qError && <p className="mt-2 text-xs text-red-700">{qError}</p>}
              {!preview ? (
                <div className="mt-3 flex gap-2">
                  <button
                    type="button"
                    onClick={loadPreview}
                    disabled={qBusy || !draftQuestion.trim()}
                    className="rounded-full bg-neutral-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {qBusy ? "Loading…" : "Preview changes"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditing(false)}
                    className="rounded-full border border-neutral-300 px-4 py-1.5 text-xs text-neutral-700 hover:border-neutral-500"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3">
                  <p className="text-[13px] font-medium text-amber-950">
                    Changing to question v{preview.to_version} will:
                  </p>
                  <ul className="mt-1 list-disc pl-5 text-xs leading-relaxed text-neutral-700">
                    <li>
                      Archive {preview.conclusions_to_archive.findings} findings,{" "}
                      {preview.conclusions_to_archive.connections} connections as
                      historical (kept, never shown as current)
                    </li>
                    <li>Re-score relevance for {preview.n_papers} paper(s)</li>
                    <li>Rebuild findings and connections for the new question</li>
                    <li>Preserve PDFs, inventory, localizations, audits, and runs</li>
                  </ul>
                  <div className="mt-2 flex gap-2">
                    <button
                      type="button"
                      onClick={confirmQuestion}
                      disabled={qBusy}
                      className="rounded-full bg-neutral-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {qBusy ? "Updating…" : "Confirm & recompute"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditing(false)}
                      className="rounded-full border border-neutral-300 bg-white px-4 py-1.5 text-xs text-neutral-700 hover:border-neutral-500"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {rescoreActive && (
          <div className="mt-4 rounded-md border border-indigo-300 bg-indigo-50 p-3">
            <p className="text-[13px] font-medium text-indigo-950">
              Updating for question v{project.question_version ?? 1} — relevance and
              findings are being recomputed. Current views stay until replacements are
              ready.
            </p>
          </div>
        )}

        {view.rebuild?.needed && !rescoreActive && (
          <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3">
            <p className="text-[13px] font-medium text-amber-950">
              Rebuild incomplete — {view.rebuild.reason} (
              {view.rebuild.papers.join(", ")}).
            </p>
            {rebuildError && <p className="mt-1 text-xs text-red-700">{rebuildError}</p>}
            <button
              type="button"
              onClick={doRebuild}
              disabled={rebuilding}
              className="mt-2 rounded-full bg-neutral-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {rebuilding ? "Starting…" : "Continue rebuild"}
            </button>
          </div>
        )}

        {agentRun && agentRun.status === "awaiting_human" && (
          <div className="mt-6">
            <ReviewCard
              run={agentRun}
              live={true}
              onSubmitted={refresh}
              reviewUrl={`${AGENT_API_BASE}/api/projects/${projectId}/runs/${agentRun.thread_id}/review`}
            />
          </div>
        )}

        {(agentRun || agentSummary) && (
          <Section title="Research agent">
            <AgentRunSection run={agentRun} />
            <ModelUsageSection summary={agentSummary} />
          </Section>
        )}

        <Section title={`Papers (${project.papers.length})`}>
          <div className="rounded-md border border-neutral-200 bg-white p-4">
            <div className="text-sm font-semibold text-neutral-900">Add papers</div>
            <input
              type="file"
              accept="application/pdf,.pdf"
              multiple
              onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
              className="mt-2 text-xs text-neutral-700"
            />
            {files.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1">
                {files.map((f) => (
                  <li key={f.name} className="text-xs text-neutral-600">
                    {f.name} · {formatBytes(f.size)}
                  </li>
                ))}
              </ul>
            )}
            {uploadError && <p className="mt-2 text-xs text-red-700">{uploadError}</p>}
            <button
              type="button"
              onClick={doUpload}
              disabled={uploading || files.length === 0}
              className="mt-2 rounded-full bg-neutral-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {uploading ? "Uploading…" : `Upload ${files.length || ""} PDF${files.length === 1 ? "" : "s"}`}
            </button>
          </div>

          {project.papers.length === 0 && activeJobs.length === 0 && (
            <p className="text-sm text-neutral-600">
              No papers yet — upload your first PDF above to start the analysis.
            </p>
          )}

          {project.papers.map((p) => {
            const st: PaperStatus | undefined = status[p.paper_id];
            const stale = st?.relevance_stale;
            return (
              <div
                key={p.paper_id}
                className="rounded-md border border-neutral-200 bg-white p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                      Paper {p.added_order}
                    </div>
                    <h3 className="mt-0.5 font-serif text-base leading-snug text-neutral-900">
                      {p.title}
                    </h3>
                    <p className="mt-1 text-[13px] text-neutral-600">
                      {p.n_objects} objects · {p.n_localized} localized ·{" "}
                      {findingsForPaper(project, p.paper_id).length} findings
                      {stale && (
                        <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-900">
                          relevance updating
                        </span>
                      )}
                    </p>
                  </div>
                  <Link
                    href={`/projects/${projectId}/read/${p.paper_id}`}
                    className="shrink-0 rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700"
                  >
                    Open in reader
                  </Link>
                </div>
              </div>
            );
          })}

          {removeError && <p className="text-xs text-red-700">{removeError}</p>}
          {Object.entries(status)
            .filter(([pid]) => !paperById(project, pid))
            .map(([pid, st]) => (
              <div
                key={pid}
                className="rounded-md border border-neutral-200 bg-white p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                      {pid}
                    </div>
                    <p className="mt-1 text-[13px] text-neutral-700">
                      {st.status === "processing" && (
                        <>
                          {stageLabel(st.stage ?? "")}
                          {st.stage_detail ? ` — ${st.stage_detail}` : ""}
                        </>
                      )}
                      {st.status === "awaiting_review" && "Waiting on researcher review."}
                      {st.status === "failed" && (
                        <span className="text-red-700">Failed: {st.last_error}</span>
                      )}
                      {st.status === "cancelled" && "Cancelled."}
                      {st.status === "incomplete" && "Staged but never processed."}
                    </p>
                  </div>
                  {(st.status === "failed" ||
                    st.status === "cancelled" ||
                    st.status === "incomplete") && (
                    <button
                      type="button"
                      onClick={() => doRemove(pid)}
                      disabled={removingId === pid}
                      className="shrink-0 rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-red-400 hover:text-red-700 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {removingId === pid ? "Removing…" : "Remove"}
                    </button>
                  )}
                </div>
              </div>
            ))}
        </Section>

        <DiscoverySection
          projectId={projectId}
          questionVersion={project.question_version ?? 1}
          onProjectChanged={refresh}
        />

        {(activeJobs.length > 0 || failedJobs.length > 0) && (
          <Section title="Processing">
            {jobActionError && (
              <p className="text-xs text-red-700">{jobActionError}</p>
            )}
            {activeJobs.map((j) => (
              <JobRow key={j.job_id} job={j} onRetry={doRetry} />
            ))}
            {failedJobs.map((j) => (
              <JobRow key={j.job_id} job={j} onRetry={doRetry} />
            ))}
          </Section>
        )}

        {project.audits && project.audits.length > 0 && (
          <Section title={`Evidence audits (${project.audits.length})`}>
            {project.audits.map((a) => (
              <AuditEvidence key={a.audit_id} audit={a} />
            ))}
          </Section>
        )}

        {project.verifications && project.verifications.length > 0 && (
          <Section title={`Computational verification (${project.verifications.length})`}>
            {project.verifications.map((v) => (
              <VerificationEvidence key={v.id} evidence={v} />
            ))}
          </Section>
        )}

        <Section title={`Key findings (${project.findings.length})`}>
          {project.findings.length === 0 && (
            <p className="text-sm text-neutral-600">
              No findings yet — they appear here once papers finish processing.
            </p>
          )}
          {project.findings.map((f) => {
            const paper = paperById(project, f.paper_id);
            return (
              <div key={f.id} className="rounded-md border border-neutral-200 bg-white p-4">
                <p className="text-sm leading-relaxed text-neutral-900">{f.statement}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Link
                    href={findingLink(projectId, f)}
                    className="rounded-full border border-neutral-300 bg-white px-2 py-0.5 font-mono text-[11px] text-neutral-700 hover:border-indigo-600 hover:text-indigo-800"
                  >
                    {f.id}
                  </Link>
                  <span className="text-xs text-neutral-500">
                    {paper?.title.split(":")[0]} · {f.evidence}
                  </span>
                </div>
              </div>
            );
          })}
        </Section>

        <Section title={`Cross-paper connections (${project.connections.length})`}>
          {project.connections.length === 0 && (
            <p className="text-sm text-neutral-600">
              No supported connections yet — add another paper to grow cross-paper memory.
            </p>
          )}
          {[...grouped.entries()].map(([type, conns]) => (
            <div key={type}>
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                {type.replace(/_/g, " ")} ({conns.length})
              </div>
              <div className="flex flex-col gap-2">
                {conns.map((c) => (
                  <div key={c.id} className="rounded-md border border-neutral-200 bg-white p-3">
                    <div className="flex flex-wrap items-center gap-2 text-[13px]">
                      <span className="font-mono text-xs text-neutral-700">
                        {endpointLabel(project, c.from_ref)}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${TYPE_BADGE[c.type]}`}
                      >
                        {c.type.replace(/_/g, " ")}
                      </span>
                      <span className="font-mono text-xs text-neutral-700">
                        {endpointLabel(project, c.to_ref)}
                      </span>
                    </div>
                    <p className="mt-1.5 text-[13px] leading-relaxed text-neutral-700">{c.reason}</p>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </Section>

        <Section title={`Open questions (${project.open_questions.length})`}>
          {project.open_questions.map((q) => (
            <div key={q.id} className="rounded-md border border-neutral-200 bg-white p-4">
              <p className="text-sm leading-relaxed text-neutral-900">{q.text}</p>
              <div className="mt-2 text-xs text-neutral-500">
                Raised by {paperById(project, q.raised_by)?.title.split(":")[0] ?? q.raised_by}
              </div>
            </div>
          ))}
        </Section>

        <Section title={`Novelty risks (${project.novelty_risks.length})`}>
          {project.novelty_risks.map((r) => (
            <div key={r.id} className="rounded-md border border-amber-200 bg-amber-50 p-4">
              <p className="text-sm leading-relaxed text-neutral-900">
                <span className="font-medium">{CORPUS_SCOPE_PREFIX}</span> {r.direction}
              </p>
              <p className="mt-1.5 text-[13px] leading-relaxed text-neutral-700">{r.note}</p>
            </div>
          ))}
        </Section>

        {history.length > 0 && (
          <Section title={`Question history (${history.length})`}>
            {history.map((h, i) => (
              <details
                key={`qv-${h.version}-${i}`}
                className="rounded-md border border-neutral-200 bg-white px-4 py-3"
              >
                <summary className="cursor-pointer text-sm text-neutral-800">
                  <span className="font-medium">v{h.version}</span> — {h.question}{" "}
                  <span className="text-xs text-neutral-500">
                    ({h.findings.length} findings archived · {formatTime(h.created_at)})
                  </span>
                </summary>
                <p className="mt-2 text-xs leading-relaxed text-neutral-500">
                  Historical conclusions from this question version are preserved for
                  provenance and are never shown as current findings.
                </p>
              </details>
            ))}
          </Section>
        )}
      </main>
    </div>
  );
}
