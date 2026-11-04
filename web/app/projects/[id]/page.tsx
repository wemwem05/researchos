"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import {
  changeQuestion,
  deletePaper,
  fetchProjectJobs,
  fetchProjectView,
  fetchQuestionPreview,
  isJobActive,
  requestRebuild,
  retryJob,
  updateProjectTitle,
  uploadPaper,
  WorkspaceError,
  type ProjectView,
  type QuestionPreview,
  type WorkspaceJob,
} from "../../lib/workspace";
import {
  AGENT_API_BASE,
  type AgentRunRecord,
  type AgentTelemetrySummary,
} from "../../lib/agent";
import AppShell from "../../components/AppShell";
import Sidebar, { type ProjectTab } from "../../components/Sidebar";
import OverviewTab from "../../components/OverviewTab";
import PapersTab from "../../components/PapersTab";
import EvidenceTab from "../../components/EvidenceTab";
import ActivityTab from "../../components/ActivityTab";
import DiscoverySection from "../../components/DiscoverySection";
import { PageHeader } from "../../components/ui";
import type { ScientificAuditView } from "../../lib/audit";
import type { VerificationEvidenceView } from "../../lib/verification";
import type { ProjectData } from "../../lib/project";
import { paperById } from "../../lib/project";

const TABS: ProjectTab[] = ["overview", "papers", "discover", "evidence", "activity"];

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

export default function LiveProjectPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center text-sm text-slate-500">
          Loading project…
        </div>
      }
    >
      <LiveProjectView />
    </Suspense>
  );
}

function LiveProjectView() {
  const params = useParams();
  const searchParams = useSearchParams();
  const projectId = params.id as string;
  const [view, setView] = useState<ProjectView | null>(null);
  const [jobs, setJobs] = useState<WorkspaceJob[]>([]);
  const [missing, setMissing] = useState(false);
  const [backendDown, setBackendDown] = useState(false);
  const [tick, setTick] = useState(0);

  const tabParam = searchParams.get("tab");
  const tab: ProjectTab = TABS.includes(tabParam as ProjectTab)
    ? (tabParam as ProjectTab)
    : "overview";

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
  const reviewNeeded = agentRun?.status === "awaiting_human";

  useEffect(() => {
    if (!view || backendDown || missing) return;
    if (activeJobs.length === 0 && !runActive) return;
    const id = setTimeout(() => setTick((x) => x + 1), 3000);
    return () => clearTimeout(id);
  }, [view, backendDown, missing, activeJobs.length, runActive]);

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

  function goTab(t: ProjectTab) {
    // Sidebar links are the primary navigation; in-content buttons that
    // need programmatic switching use a plain location change so the URL
    // (and deep links) stay canonical.
    window.location.assign(
      t === "overview" ? `/projects/${projectId}` : `/projects/${projectId}?tab=${t}`
    );
  }

  if (missing) {
    return (
      <AppShell sidebar={<Sidebar />}>
        <div className="flex flex-col items-center gap-2 py-16 text-sm text-slate-500">
          <p>Unknown project “{projectId}”.</p>
          <Link href="/" className="font-medium text-indigo-600 hover:underline">
            Back to projects
          </Link>
        </div>
      </AppShell>
    );
  }
  if (backendDown) {
    return (
      <AppShell sidebar={<Sidebar />}>
        <div className="flex flex-col items-center gap-2 py-16 text-sm text-slate-500">
          <p>Workspace backend not running — start it with ./dev.sh</p>
          <div className="flex gap-4">
            <button
              type="button"
              onClick={refresh}
              className="font-medium text-indigo-600 hover:underline"
            >
              Retry
            </button>
            <Link href="/" className="font-medium text-indigo-600 hover:underline">
              Back to projects
            </Link>
          </div>
        </div>
      </AppShell>
    );
  }
  if (!project || !view) {
    return (
      <AppShell sidebar={<Sidebar />}>
        <div className="flex items-center justify-center py-16 text-sm text-slate-500">
          Loading project…
        </div>
      </AppShell>
    );
  }

  const failedJobs = jobs.filter((j) => j.state === "FAILED").slice(0, 5);
  const audits = project.audits ?? [];
  const verifications = project.verifications ?? [];
  const needsAttention =
    reviewNeeded || activeJobs.length > 0 || failedJobs.length > 0;
  const unattached = Object.entries(status).filter(
    ([pid]) => !paperById(project, pid)
  );

  return (
    <AppShell
      sidebar={
        <Sidebar
          project={{ id: projectId, title: project.title || projectId }}
          tab={tab}
          counts={{
            papers: project.papers.length,
            evidence: audits.length + verifications.length,
          }}
          attention={!!needsAttention}
        />
      }
    >
      <div className="flex flex-col gap-6">
        <PageHeader
          breadcrumb={
            <>
              <Link href="/" className="hover:text-indigo-600 hover:underline">
                Projects
              </Link>{" "}
              / {project.title || projectId}
            </>
          }
          title={project.title || projectId}
          description={`${project.papers.length} papers · ${project.findings.length} findings · question v${project.question_version ?? 1}`}
        />
        {tab !== "overview" && (
          <p className="-mt-3 line-clamp-2 border-l-2 border-indigo-200 pl-3 font-serif text-[15px] italic leading-snug text-slate-600">
            {project.research_question}
          </p>
        )}

        {rescoreActive && (
          <div className="rounded-xl border border-indigo-200 bg-indigo-50/60 p-4">
            <p className="text-[13px] font-medium text-indigo-950">
              Updating for question v{project.question_version ?? 1} — relevance
              and findings are being recomputed. Current views stay until
              replacements are ready.
            </p>
          </div>
        )}

        {reviewNeeded && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-300 bg-amber-50 p-4">
            <p className="text-sm font-medium text-amber-950">
              The research agent paused with a question for you.
            </p>
            <button
              type="button"
              onClick={() => goTab("activity")}
              className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
            >
              Review now
            </button>
          </div>
        )}

        {view.rebuild?.needed && !rescoreActive && (
          <div className="rounded-xl border border-amber-300 bg-amber-50 p-4">
            <p className="text-sm font-medium text-amber-950">
              Rebuild incomplete — {view.rebuild.reason} (
              {view.rebuild.papers.join(", ")}).
            </p>
            {rebuildError && (
              <p className="mt-1 text-xs text-red-700">{rebuildError}</p>
            )}
            <button
              type="button"
              onClick={doRebuild}
              disabled={rebuilding}
              className="mt-3 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {rebuilding ? "Starting…" : "Continue rebuild"}
            </button>
          </div>
        )}

        {tab === "overview" && (
          <OverviewTab
            project={project}
            projectId={projectId}
            status={status}
            reviewNeeded={!!reviewNeeded}
            activeJobCount={activeJobs.length}
            rebuildNeeded={!!view.rebuild?.needed && !rescoreActive}
            rebuilding={rebuilding}
            onRebuild={doRebuild}
            goTab={goTab}
            editing={editing}
            draftTitle={draftTitle}
            draftQuestion={draftQuestion}
            preview={preview}
            qBusy={qBusy}
            qError={qError}
            onStartEdit={() => {
              setDraftTitle(project.title || "");
              setDraftQuestion(project.research_question);
              setPreview(null);
              setQError(null);
              setEditing(true);
            }}
            onCancelEdit={() => setEditing(false)}
            onDraftTitle={setDraftTitle}
            onDraftQuestion={setDraftQuestion}
            onPreview={loadPreview}
            onConfirm={confirmQuestion}
          />
        )}

        {tab === "papers" && (
          <PapersTab
            project={project}
            projectId={projectId}
            status={status}
            unattached={unattached}
            files={files}
            uploading={uploading}
            uploadError={uploadError}
            removingId={removingId}
            removeError={removeError}
            onFiles={setFiles}
            onUpload={doUpload}
            onRemove={doRemove}
          />
        )}

        {tab === "discover" && (
          <DiscoverySection
            projectId={projectId}
            question={project.research_question}
            questionVersion={project.question_version ?? 1}
            onProjectChanged={refresh}
          />
        )}

        {tab === "evidence" && (
          <EvidenceTab
            projectId={projectId}
            audits={audits}
            verifications={verifications}
          />
        )}

        {tab === "activity" && (
          <ActivityTab
            projectId={projectId}
            agentRun={agentRun}
            agentSummary={agentSummary}
            activeJobs={activeJobs}
            failedJobs={failedJobs}
            jobActionError={jobActionError}
            onRetry={doRetry}
            onSubmitted={refresh}
          />
        )}
      </div>
    </AppShell>
  );
}
