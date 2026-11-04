"use client";

import { useState } from "react";
import {
  AgentRunSection,
  ModelUsageSection,
  ReviewCard,
} from "./AgentPanel";
import { EmptyState, PageHeader, SectionHeader } from "./ui";
import {
  isJobActive,
  stageLabel,
  type WorkspaceJob,
} from "../lib/workspace";
import {
  AGENT_API_BASE,
  type AgentRunRecord,
  type AgentTelemetrySummary,
} from "../lib/agent";

function jobTitle(job: WorkspaceJob): string {
  if (job.job_type === "ingest_paper") return "Analyzing paper";
  if (job.job_type === "discover_papers") return "Discovering literature";
  if (job.job_type === "import_discovery") return "Adding discovered paper";
  return "Updating for new question";
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
  const dot = active
    ? "bg-indigo-500"
    : job.state === "COMPLETED"
      ? "bg-green-500"
      : job.state === "FAILED"
        ? "bg-red-500"
        : "bg-slate-300";
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${dot}`} />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-900">
              {jobTitle(job)}
            </div>
            <p className="mt-0.5 text-[13px] text-slate-500">
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
              className="shrink-0 rounded-lg border border-slate-300 bg-white px-3 py-1 text-xs font-medium text-slate-700 hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {retrying
                ? "Retrying…"
                : job.attempts >= job.max_attempts
                  ? "Retries exhausted"
                  : "Retry"}
            </button>
          ) : (
            <span className="shrink-0 text-xs text-slate-500">
              Retry won&apos;t help for this error
            </span>
          ))}
      </div>
    </div>
  );
}

export default function ActivityTab({
  projectId,
  agentRun,
  agentSummary,
  activeJobs,
  failedJobs,
  jobActionError,
  onRetry,
  onSubmitted,
}: {
  projectId: string;
  agentRun: AgentRunRecord | null;
  agentSummary: AgentTelemetrySummary | null;
  activeJobs: WorkspaceJob[];
  failedJobs: WorkspaceJob[];
  jobActionError: string | null;
  onRetry: (jobId: string) => void;
  onSubmitted: () => void;
}) {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Activity"
        description="Review requests first, then running jobs and the latest agent run."
      />

      {agentRun && agentRun.status === "awaiting_human" && (
        <ReviewCard
          run={agentRun}
          live={true}
          onSubmitted={onSubmitted}
          reviewUrl={`${AGENT_API_BASE}/api/projects/${projectId}/runs/${agentRun.thread_id}/review`}
        />
      )}

      <section>
        <SectionHeader title="Processing" />
        {jobActionError && (
          <p className="mb-3 text-xs text-red-700">{jobActionError}</p>
        )}
        {activeJobs.length === 0 && failedJobs.length === 0 ? (
          <EmptyState
            title="Nothing running"
            body="Uploads, discovery runs, imports, and recomputations appear here with live progress."
          />
        ) : (
          <div className="flex flex-col gap-3">
            {activeJobs.map((j) => (
              <JobRow key={j.job_id} job={j} onRetry={onRetry} />
            ))}
            {failedJobs.map((j) => (
              <JobRow key={j.job_id} job={j} onRetry={onRetry} />
            ))}
          </div>
        )}
      </section>

      {(agentRun || agentSummary) && (
        <section>
          <SectionHeader title="Latest agent run" />
          <div className="flex flex-col gap-3">
            <AgentRunSection run={agentRun} />
            <ModelUsageSection summary={agentSummary} />
          </div>
        </section>
      )}
    </div>
  );
}
