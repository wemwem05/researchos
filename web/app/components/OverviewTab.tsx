"use client";

import { useState } from "react";
import Link from "next/link";
import type { ProjectTab } from "./Sidebar";
import ResearchQuestionPanel from "./ResearchQuestionPanel";
import { FindingCard } from "./cards";
import { EmptyState, MetricCard, PRIMARY_BUTTON, SectionHeader } from "./ui";
import type { PaperStatus, QuestionPreview } from "../lib/workspace";
import type { ScientificAuditView } from "../lib/audit";
import type { VerificationEvidenceView } from "../lib/verification";
import type {
  Connection,
  Finding,
  NoveltyRisk,
  OpenQuestion,
  PaperEntry,
  ProjectData,
} from "../lib/project";
import {
  CORPUS_SCOPE_PREFIX,
  endpointLabel,
  findingsForPaper,
  paperById,
} from "../lib/project";
import { dedupeQuestionHistory } from "../lib/workspace";

export interface OverviewProject extends ProjectData {
  research_question: string;
  question_version: number;
  title: string;
  papers: PaperEntry[];
  findings: Finding[];
  connections: Connection[];
  open_questions: OpenQuestion[];
  novelty_risks: NoveltyRisk[];
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

const TYPE_BADGE: Record<string, string> = {
  supports: "bg-green-100 text-green-900",
  contradicts: "bg-red-100 text-red-900",
  extends: "bg-indigo-100 text-indigo-900",
  overlaps_with: "bg-slate-200 text-slate-800",
  uses_same_method: "bg-slate-200 text-slate-800",
  uses_different_assumption: "bg-amber-100 text-amber-900",
  leaves_open: "bg-amber-100 text-amber-900",
};

function findingHref(projectId: string, f: Finding): string {
  const obj = f.object_ids[0];
  return `/projects/${projectId}/read/${f.paper_id}${obj ? `?select=${encodeURIComponent(obj)}` : ""}`;
}

export default function OverviewTab({
  project,
  projectId,
  status,
  reviewNeeded,
  activeJobCount,
  rebuildNeeded,
  rebuilding,
  onRebuild,
  goTab,
  editing,
  draftTitle,
  draftQuestion,
  preview,
  qBusy,
  qError,
  onStartEdit,
  onCancelEdit,
  onDraftTitle,
  onDraftQuestion,
  onPreview,
  onConfirm,
}: {
  project: OverviewProject;
  projectId: string;
  status: Record<string, PaperStatus>;
  reviewNeeded: boolean;
  activeJobCount: number;
  rebuildNeeded: boolean;
  rebuilding: boolean;
  onRebuild: () => void;
  goTab: (t: ProjectTab) => void;
  editing: boolean;
  draftTitle: string;
  draftQuestion: string;
  preview: QuestionPreview | null;
  qBusy: boolean;
  qError: string | null;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onDraftTitle: (v: string) => void;
  onDraftQuestion: (v: string) => void;
  onPreview: () => void;
  onConfirm: () => void;
}) {
  const [showAllFindings, setShowAllFindings] = useState(false);
  const [showAllQuestions, setShowAllQuestions] = useState(false);
  const [showAllRisks, setShowAllRisks] = useState(false);

  const visibleFindings = showAllFindings
    ? project.findings
    : project.findings.slice(0, 5);
  const visibleQuestions = showAllQuestions
    ? project.open_questions
    : project.open_questions.slice(0, 3);
  const visibleRisks = showAllRisks
    ? project.novelty_risks
    : project.novelty_risks.slice(0, 2);
  const recentPapers = [...project.papers].reverse().slice(0, 3);
  const history = dedupeQuestionHistory(
    (project.question_history ?? []).filter((h) => h.status === "superseded")
  );

  return (
    <div className="flex flex-col gap-6">
      <ResearchQuestionPanel
        question={project.research_question}
        version={project.question_version ?? 1}
        editing={editing}
        draftTitle={draftTitle}
        draftQuestion={draftQuestion}
        preview={preview}
        busy={qBusy}
        error={qError}
        onStartEdit={onStartEdit}
        onCancelEdit={onCancelEdit}
        onDraftTitle={onDraftTitle}
        onDraftQuestion={onDraftQuestion}
        onPreview={onPreview}
        onConfirm={onConfirm}
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricCard label="Papers analyzed" value={String(project.papers.length)} />
        <MetricCard label="Grounded findings" value={String(project.findings.length)} />
        <MetricCard
          label="Open questions"
          value={String(project.open_questions.length)}
        />
        <MetricCard label="Evidence audits" value={String(project.audits.length)} />
      </div>

      <NextActionCard
        project={project}
        projectId={projectId}
        reviewNeeded={reviewNeeded}
        activeJobCount={activeJobCount}
        rebuildNeeded={rebuildNeeded}
        rebuilding={rebuilding}
        onRebuild={onRebuild}
        goTab={goTab}
      />

      <div className="grid gap-6 lg:grid-cols-5">
        <div className="flex flex-col gap-6 lg:col-span-3">
          <section>
            <SectionHeader title={`Key findings (${project.findings.length})`} />
            {project.findings.length === 0 ? (
              <EmptyState
                title="No findings yet"
                body={
                  project.papers.length === 0
                    ? "Add papers or discover literature to begin."
                    : "Findings appear here once papers finish processing."
                }
              />
            ) : (
              <div className="flex flex-col gap-3">
                {visibleFindings.map((f) => {
                  const paper = paperById(project, f.paper_id);
                  return (
                    <FindingCard
                      key={f.id}
                      href={findingHref(projectId, f)}
                      id={f.id}
                      statement={f.statement}
                      source={`${paper?.title.split(":")[0] ?? f.paper_id} · ${f.evidence}`}
                    />
                  );
                })}
                {project.findings.length > 5 && (
                  <button
                    type="button"
                    onClick={() => setShowAllFindings((x) => !x)}
                    className="self-start text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                  >
                    {showAllFindings
                      ? "Show fewer"
                      : `Show all ${project.findings.length} findings`}
                  </button>
                )}
              </div>
            )}
          </section>

          <section>
            <SectionHeader
              title={`Recent papers (${project.papers.length})`}
              action={
                project.papers.length > 3 ? (
                  <button
                    type="button"
                    onClick={() => goTab("papers")}
                    className="text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                  >
                    View all
                  </button>
                ) : undefined
              }
            />
            {recentPapers.length === 0 ? (
              <EmptyState
                title="No papers yet"
                body="Upload PDFs or let the agent discover literature for this question."
                action={
                  <button
                    type="button"
                    onClick={() => goTab("papers")}
                    className={PRIMARY_BUTTON}
                  >
                    Add papers
                  </button>
                }
              />
            ) : (
              <div className="flex flex-col gap-3">
                {recentPapers.map((p) => (
                  <Link
                    key={p.paper_id}
                    href={`/projects/${projectId}/read/${p.paper_id}`}
                    className="rounded-xl border border-slate-200 bg-white p-4 hover:border-indigo-300"
                  >
                    <span className="line-clamp-2 font-serif text-[15px] leading-snug text-slate-900">
                      {p.title}
                    </span>
                    <span className="mt-1 block text-xs text-slate-500">
                      {p.n_objects} objects · {p.n_localized} localized ·{" "}
                      {findingsForPaper(project, p.paper_id).length} findings
                      {status[p.paper_id]?.relevance_stale && " · updating"}
                    </span>
                  </Link>
                ))}
              </div>
            )}
          </section>
        </div>

        <div className="flex flex-col gap-6 lg:col-span-2">
          <section>
            <SectionHeader
              title={`Open questions (${project.open_questions.length})`}
            />
            {project.open_questions.length === 0 ? (
              <p className="text-sm text-slate-500">
                None recorded yet — they surface as papers are analyzed.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {visibleQuestions.map((q) => (
                  <div
                    key={q.id}
                    className="rounded-xl border border-slate-200 bg-white p-4"
                  >
                    <p className="text-sm leading-relaxed text-slate-900">{q.text}</p>
                    <p className="mt-1.5 text-xs text-slate-500">
                      Raised by{" "}
                      {paperById(project, q.raised_by)?.title.split(":")[0] ??
                        q.raised_by}
                    </p>
                  </div>
                ))}
                {project.open_questions.length > 3 && (
                  <button
                    type="button"
                    onClick={() => setShowAllQuestions((x) => !x)}
                    className="self-start text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                  >
                    {showAllQuestions
                      ? "Show fewer"
                      : `Show all ${project.open_questions.length}`}
                  </button>
                )}
              </div>
            )}
          </section>

          <section>
            <SectionHeader
              title={`Novelty risks (${project.novelty_risks.length})`}
            />
            {project.novelty_risks.length === 0 ? (
              <p className="text-sm text-slate-500">
                None flagged for this corpus yet.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {visibleRisks.map((r) => (
                  <div
                    key={r.id}
                    className="rounded-xl border border-amber-200 bg-amber-50 p-4"
                  >
                    <p className="text-sm leading-relaxed text-slate-900">
                      <span className="font-medium">{CORPUS_SCOPE_PREFIX}</span>{" "}
                      {r.direction}
                    </p>
                    <p className="mt-1 text-[13px] leading-relaxed text-slate-600">
                      {r.note}
                    </p>
                  </div>
                ))}
                {project.novelty_risks.length > 2 && (
                  <button
                    type="button"
                    onClick={() => setShowAllRisks((x) => !x)}
                    className="self-start text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                  >
                    {showAllRisks
                      ? "Show fewer"
                      : `Show all ${project.novelty_risks.length}`}
                  </button>
                )}
              </div>
            )}
          </section>

          {history.length > 0 && (
            <section>
              <SectionHeader title={`Question history (${history.length})`} />
              <div className="flex flex-col gap-2">
                {history.map((h, i) => (
                  <details
                    key={`qv-${h.version}-${i}`}
                    className="rounded-xl border border-slate-200 bg-white px-4 py-3"
                  >
                    <summary className="cursor-pointer text-sm leading-relaxed text-slate-700">
                      <span className="font-medium">v{h.version}</span> — {h.question}{" "}
                      <span className="whitespace-nowrap text-xs text-slate-400">
                        ({h.findings.length} archived)
                      </span>
                    </summary>
                    <p className="mt-2 text-xs leading-relaxed text-slate-500">
                      Historical conclusions from this version are preserved for
                      provenance and never shown as current findings.
                    </p>
                  </details>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>

      <ConnectionsSection project={project} />
    </div>
  );
}

function ConnectionsSection({ project }: { project: OverviewProject }) {
  const [expanded, setExpanded] = useState(false);
  const groups = new Map<string, Connection[]>();
  for (const c of project.connections) {
    const list = groups.get(c.type) ?? [];
    list.push(c);
    groups.set(c.type, list);
  }
  if (project.connections.length === 0) return null;
  return (
    <section>
      <SectionHeader
        title={`Cross-paper connections (${project.connections.length})`}
      />
      <div className="flex flex-col gap-3">
        {(expanded ? project.connections : project.connections.slice(0, 4)).map(
          (c) => (
            <div
              key={c.id}
              className="rounded-xl border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-center gap-2 text-[13px]">
                <span className="font-mono text-xs text-slate-600">
                  {endpointLabel(project, c.from_ref)}
                </span>
                <span
                  className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${TYPE_BADGE[c.type] ?? "bg-slate-200 text-slate-800"}`}
                >
                  {c.type.replace(/_/g, " ")}
                </span>
                <span className="font-mono text-xs text-slate-600">
                  {endpointLabel(project, c.to_ref)}
                </span>
              </div>
              <p className="mt-1.5 text-[13px] leading-relaxed text-slate-600">
                {c.reason}
              </p>
            </div>
          )
        )}
        {project.connections.length > 4 && (
          <button
            type="button"
            onClick={() => setExpanded((x) => !x)}
            className="self-start text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
          >
            {expanded ? "Show fewer" : `Show all ${project.connections.length}`}
          </button>
        )}
      </div>
    </section>
  );
}

function NextActionCard({
  project,
  projectId,
  reviewNeeded,
  activeJobCount,
  rebuildNeeded,
  rebuilding,
  onRebuild,
  goTab,
}: {
  project: OverviewProject;
  projectId: string;
  reviewNeeded: boolean;
  activeJobCount: number;
  rebuildNeeded: boolean;
  rebuilding: boolean;
  onRebuild: () => void;
  goTab: (t: ProjectTab) => void;
}) {
  if (reviewNeeded) {
    return (
      <ActionCard
        tone="amber"
        title="Review needed"
        body="The research agent paused with a question for you."
        action={
          <button type="button" onClick={() => goTab("activity")} className={PRIMARY_BUTTON}>
            Review now
          </button>
        }
      />
    );
  }
  if (activeJobCount > 0) {
    return (
      <ActionCard
        tone="indigo"
        title="Work in progress"
        body={`${activeJobCount} job${activeJobCount === 1 ? " is" : "s are"} running — follow the live progress, no action needed.`}
        action={
          <button type="button" onClick={() => goTab("activity")} className={PRIMARY_BUTTON}>
            View activity
          </button>
        }
      />
    );
  }
  if (rebuildNeeded) {
    return (
      <ActionCard
        tone="amber"
        title="Rebuild paused"
        body="A findings rebuild was paused — continue it to finish the update."
        action={
          <button
            type="button"
            onClick={onRebuild}
            disabled={rebuilding}
            className={PRIMARY_BUTTON}
          >
            {rebuilding ? "Starting…" : "Continue rebuild"}
          </button>
        }
      />
    );
  }
  if (project.papers.length === 0) {
    return (
      <ActionCard
        tone="plain"
        title="Start your research"
        body="This project is empty. Upload papers you already have, or let the agent find relevant literature."
        action={
          <>
            <button type="button" onClick={() => goTab("papers")} className={PRIMARY_BUTTON}>
              Upload papers
            </button>
            <button
              type="button"
              onClick={() => goTab("discover")}
              className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:border-slate-400"
            >
              Discover literature
            </button>
          </>
        }
      />
    );
  }
  if (project.findings.length > 0 && project.audits.length === 0 && project.verifications.length === 0) {
    const first = project.papers[0];
    return (
      <ActionCard
        tone="plain"
        title="Examine your evidence"
        body="Open a paper and audit a key claim, or run a computational test where feasible."
        action={
          first ? (
            <Link
              href={`/projects/${projectId}/read/${first.paper_id}`}
              className={PRIMARY_BUTTON}
            >
              Open {first.title.split(":")[0].slice(0, 32)}
            </Link>
          ) : null
        }
      />
    );
  }
  return (
    <ActionCard
      tone="plain"
      title="Grow the corpus"
      body="Discover more literature for this research question."
      action={
        <button type="button" onClick={() => goTab("discover")} className={PRIMARY_BUTTON}>
          Discover literature
        </button>
      }
    />
  );
}

function ActionCard({
  tone,
  title,
  body,
  action,
}: {
  tone: "amber" | "indigo" | "plain";
  title: string;
  body: React.ReactNode;
  action: React.ReactNode;
}) {
  const frame =
    tone === "amber"
      ? "border-amber-300 bg-amber-50"
      : tone === "indigo"
        ? "border-indigo-200 bg-indigo-50/60"
        : "border-slate-200 bg-white";
  const titleColor =
    tone === "amber"
      ? "text-amber-950"
      : tone === "indigo"
        ? "text-indigo-950"
        : "text-slate-900";
  return (
    <div className={`flex flex-wrap items-center justify-between gap-4 rounded-xl border p-5 ${frame}`}>
      <div className="min-w-0 flex-1 basis-64">
        <div className={`text-sm font-semibold ${titleColor}`}>{title}</div>
        <div className="mt-1 text-[13px] leading-relaxed text-slate-600">{body}</div>
      </div>
      <div className="flex shrink-0 flex-wrap gap-2">{action}</div>
    </div>
  );
}
