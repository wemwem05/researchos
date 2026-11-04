"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  DISCOVERY_FILTERS,
  decideCandidates,
  evidenceLabel,
  fetchDiscovery,
  filterCandidates,
  filterLabel,
  importCandidates,
  isRunStale,
  listDiscoveries,
  sortCandidates,
  startDiscovery,
  statusLabel,
  verdictLabel,
  authorLine,
  candidateSourceUrl,
  type CandidateStatus,
  type DiscoveryCandidate,
  type DiscoveryFilter,
  type DiscoveryList,
  type DiscoveryRun,
  type EvidenceAccessLevel,
  type ImportItem,
  type RelevanceVerdict,
} from "../lib/discovery";
import {
  fetchJob,
  formatTime,
  isJobActive,
  stageLabel,
  WorkspaceError,
  type WorkspaceJob,
} from "../lib/workspace";

const VERDICT_BADGE: Record<RelevanceVerdict, string> = {
  highly_relevant: "bg-indigo-100 text-indigo-900",
  relevant: "bg-blue-100 text-blue-900",
  tangential: "bg-neutral-100 text-neutral-600",
  off_topic: "bg-neutral-100 text-neutral-500",
};

const EVIDENCE_BADGE: Record<EvidenceAccessLevel, string> = {
  full_text_analyzed: "bg-green-100 text-green-900",
  full_text_available: "bg-green-50 text-green-800",
  title_and_abstract: "bg-neutral-100 text-neutral-600",
  metadata_only: "bg-amber-50 text-amber-900",
};

const STATUS_BADGE: Partial<Record<CandidateStatus, string>> = {
  approved: "bg-green-100 text-green-900",
  dismissed: "bg-neutral-100 text-neutral-500",
  importing: "bg-indigo-100 text-indigo-900",
  imported: "bg-green-100 text-green-900",
  import_failed: "bg-red-100 text-red-900",
  full_text_unavailable: "bg-amber-100 text-amber-900",
};

function EvalField({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <div className="mt-2">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-400">
        {label}
      </div>
      <p className="mt-0.5 text-[13px] leading-relaxed text-neutral-700">{text}</p>
    </div>
  );
}

function CandidateCard({
  projectId,
  candidate,
  selected,
  busy,
  onToggleSelect,
  onDecide,
  onImport,
}: {
  projectId: string;
  candidate: DiscoveryCandidate;
  selected: boolean;
  busy: boolean;
  onToggleSelect: () => void;
  onDecide: (action: "approve" | "dismiss") => void;
  onImport: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const c = candidate;
  const ev = c.evaluation;
  const sourceUrl = candidateSourceUrl(c);
  const selectable =
    c.status === "pending" ||
    c.status === "approved" ||
    c.status === "import_failed";
  const statusBadge = STATUS_BADGE[c.status];

  return (
    <div className="rounded-md border border-neutral-200 bg-white p-4">
      <div className="flex items-start gap-3">
        {selectable && (
          <input
            type="checkbox"
            checked={selected}
            disabled={busy}
            onChange={onToggleSelect}
            aria-label={`Select ${c.title}`}
            className="mt-1 h-4 w-4 shrink-0"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {c.recommended && (
              <span className="rounded bg-indigo-900 px-1.5 py-0.5 text-[11px] font-semibold text-white">
                Recommended
              </span>
            )}
            <span
              className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${VERDICT_BADGE[ev.verdict]}`}
            >
              {verdictLabel(ev.verdict)}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${EVIDENCE_BADGE[c.evidence_access_level]}`}
            >
              {evidenceLabel(c.evidence_access_level)}
            </span>
            {ev.novelty_overlap && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-900">
                Possible novelty overlap
              </span>
            )}
            {c.already_in_project && (
              <span className="rounded bg-neutral-200 px-1.5 py-0.5 text-[11px] font-medium text-neutral-700">
                Already in project
              </span>
            )}
            {statusBadge && (
              <span
                className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${statusBadge}`}
              >
                {statusLabel(c.status)}
              </span>
            )}
          </div>
          <h3 className="mt-1.5 font-serif text-base leading-snug text-neutral-900">
            {c.title || "(untitled record)"}
          </h3>
          <p className="mt-1 text-[13px] text-neutral-600">{authorLine(c)}</p>
          {ev.why_relevant && (
            <p className="mt-1.5 text-[13px] leading-relaxed text-neutral-700">
              {ev.why_relevant}
            </p>
          )}
          <p className="mt-1 font-mono text-[11px] text-neutral-400">
            via “{c.retrieval_query}” · round {c.retrieval_round}
            {c.citation_count != null && ` · cited by ${c.citation_count}`}
          </p>

          {expanded && (
            <div className="mt-1 border-t border-neutral-100 pt-1">
              <EvalField label="May contribute" text={ev.may_contribute} />
              <EvalField label="May overlap with" text={ev.may_overlap_with} />
              <EvalField label="Still unknown" text={ev.still_unknown} />
              <EvalField label="Why read it next" text={ev.why_read_next} />
              {ev.novelty_note && (
                <EvalField label="Novelty note" text={ev.novelty_note} />
              )}
              {c.abstract && (
                <EvalField label="Abstract (provider)" text={c.abstract} />
              )}
              <div className="mt-2 font-mono text-[11px] leading-relaxed text-neutral-500">
                {c.doi && <div>doi: {c.doi}</div>}
                {c.arxiv_id && <div>arxiv: {c.arxiv_id}</div>}
                {c.openalex_id && <div>openalex: {c.openalex_id}</div>}
                {c.license && <div>license: {c.license}</div>}
              </div>
            </div>
          )}

          {c.status_reason &&
            (c.status === "import_failed" ||
              c.status === "full_text_unavailable") && (
              <p className="mt-2 text-xs text-red-700">{c.status_reason}</p>
            )}

          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setExpanded((x) => !x)}
              className="rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500"
            >
              {expanded ? "Hide detail" : "Why this paper"}
            </button>
            {sourceUrl && (
              <a
                href={sourceUrl}
                target="_blank"
                rel="noreferrer"
                className="rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500"
              >
                Open source
              </a>
            )}
            {(c.status === "pending" || c.status === "dismissed") && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onDecide("approve")}
                className="rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? "Working…" : "Approve"}
              </button>
            )}
            {(c.status === "pending" || c.status === "approved") && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onDecide("dismiss")}
                className="rounded-full border border-neutral-300 px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Dismiss
              </button>
            )}
            {(c.status === "approved" || c.status === "import_failed") && (
              <button
                type="button"
                disabled={busy}
                onClick={onImport}
                className="rounded-full bg-indigo-900 px-3 py-1 text-xs font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy
                  ? "Working…"
                  : c.status === "import_failed"
                    ? "Retry import"
                    : "Import full text"}
              </button>
            )}
            {c.status === "imported" && c.project_paper_id && (
              <Link
                href={`/projects/${projectId}/read/${c.project_paper_id}`}
                className="rounded-full bg-green-700 px-3 py-1 text-xs font-semibold text-white hover:bg-green-600"
              >
                Open in reader
              </Link>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function RunProvenance({
  projectId,
  run,
}: {
  projectId: string;
  run: DiscoveryRun;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<{
    decisions: string[];
    usage: Record<string, number>;
  } | null>(null);

  useEffect(() => {
    if (!open || detail) return;
    let cancelled = false;
    fetchDiscovery(projectId, run.run_id)
      .then((d) => {
        if (!cancelled) {
          setDetail({
            decisions: d.record?.decisions ?? [],
            usage: d.retrieval_log?.usage ?? run.usage,
          });
        }
      })
      .catch(() => {
        if (!cancelled) setDetail({ decisions: [], usage: run.usage });
      });
    return () => {
      cancelled = true;
    };
  }, [open, detail, projectId, run.run_id, run.usage]);

  return (
    <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((x) => !x)}
        className="text-[13px] font-medium text-neutral-700 hover:text-neutral-900"
      >
        {open ? "Hide search provenance" : "Show search provenance"}
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2 text-[13px] text-neutral-700">
          {run.strategy_notes && (
            <p className="leading-relaxed">
              <span className="font-medium">Strategy: </span>
              {run.strategy_notes}
            </p>
          )}
          <div>
            <span className="font-medium">Queries:</span>
            <ul className="mt-1 flex flex-col gap-1">
              {run.queries.map((q, i) => (
                <li
                  key={`${q.query}-${i}`}
                  className="font-mono text-xs text-neutral-600"
                >
                  r{q.round} [{q.strategy || "?"}] “{q.query}” → {q.n_records}{" "}
                  records{q.error && ` (failed: ${q.error})`}
                </li>
              ))}
            </ul>
          </div>
          {run.reflection_explanation && (
            <p className="leading-relaxed">
              <span className="font-medium">Reflection ({run.reflection_decision}): </span>
              {run.reflection_explanation}
            </p>
          )}
          {run.critic_verdict && (
            <p className="leading-relaxed">
              <span className="font-medium">Critic ({run.critic_verdict}): </span>
              {run.critic_notes}
              {run.revision_used && " (one bounded revision applied)"}
            </p>
          )}
          {detail && (
            <p className="font-mono text-xs text-neutral-500">
              usage:{" "}
              {Object.entries(detail.usage)
                .map(([k, v]) => `${k}=${v}`)
                .join(" · ") || "—"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export default function DiscoverySection({
  projectId,
  questionVersion,
  onProjectChanged,
}: {
  projectId: string;
  questionVersion: number;
  onProjectChanged: () => void;
}) {
  const [list, setList] = useState<DiscoveryList | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [filter, setFilter] = useState<DiscoveryFilter>("ALL");
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState("");
  const [starting, setStarting] = useState(false);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [lastImport, setLastImport] = useState<{
    jobId: string;
    items: ImportItem[];
  } | null>(null);
  const [trackedJobId, setTrackedJobId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listDiscoveries(projectId);
      setList(data);
      setLoadError(null);
      setSelectedRunId((prev) => {
        if (prev && data.discoveries.some((r) => r.run_id === prev)) return prev;
        const latest = data.discoveries[data.discoveries.length - 1];
        return latest ? latest.run_id : null;
      });
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Discovery unavailable");
    }
  }, [projectId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Track a started import job until terminal, then refresh everything.
  useEffect(() => {
    if (!trackedJobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const job = await fetchJob(trackedJobId as string);
        if (cancelled) return;
        if (isJobActive(job)) {
          timer = setTimeout(poll, 3000);
          return;
        }
        const items = (job.result?.items as ImportItem[] | undefined) ?? [];
        setLastImport({ jobId: job.job_id, items });
        setTrackedJobId(null);
        await refresh();
        onProjectChanged();
      } catch {
        if (!cancelled) timer = setTimeout(poll, 3000);
      }
    }
    timer = setTimeout(poll, 2000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [trackedJobId, refresh, onProjectChanged]);

  // While a discovery job is active, refresh the run list periodically.
  const activeJob = list?.active_job ?? null;
  useEffect(() => {
    if (!activeJob || !isJobActive(activeJob)) return;
    const id = setTimeout(refresh, 3000);
    return () => clearTimeout(id);
  }, [activeJob, refresh]);

  const runs = useMemo(() => list?.discoveries ?? [], [list]);
  const run: DiscoveryRun | null = useMemo(
    () => runs.find((r) => r.run_id === selectedRunId) ?? runs[runs.length - 1] ?? null,
    [runs, selectedRunId]
  );
  const visible = useMemo(
    () => (run ? sortCandidates(filterCandidates(run.candidates, filter)) : []),
    [run, filter]
  );
  const stale = run ? isRunStale(run, questionVersion) : false;

  async function doStart() {
    setStarting(true);
    setActionError(null);
    try {
      const key = Math.random().toString(36).slice(2);
      const { job } = await startDiscovery(projectId, focus.trim(), key);
      setTrackedJobId(job.job_id);
      setFocus("");
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Could not start discovery");
    } finally {
      setStarting(false);
    }
  }

  async function doDecide(ids: string[], action: "approve" | "dismiss") {
    if (!run || ids.length === 0) return;
    setBusyIds(new Set(ids));
    setActionError(null);
    try {
      const res = await decideCandidates(
        projectId,
        run.run_id,
        ids.map((candidate_id) => ({ candidate_id, action }))
      );
      const failures = res.items.filter((i) => !i.ok);
      if (failures.length > 0) {
        setActionError(
          failures.map((f) => `${f.candidate_id}: ${f.error}`).join("; ")
        );
      }
      setChecked(new Set());
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Decision failed");
    } finally {
      setBusyIds(new Set());
    }
  }

  async function doImport(ids: string[]) {
    if (!run || ids.length === 0) return;
    // The server rejects dismissed candidates wholesale; keep them out.
    const byId = new Map(run.candidates.map((c) => [c.candidate_id, c]));
    const eligible = ids.filter((id) => {
      const st = byId.get(id)?.status;
      return st && st !== "dismissed" && st !== "imported" && st !== "importing";
    });
    if (eligible.length === 0) {
      setActionError("Selected candidates cannot be imported in their state.");
      return;
    }
    setBusyIds(new Set(eligible));
    setActionError(null);
    try {
      const { job } = await importCandidates(projectId, run.run_id, eligible);
      setTrackedJobId(job.job_id);
      setChecked(new Set());
      await refresh();
    } catch (e) {
      if (e instanceof WorkspaceError) setActionError(e.message);
      else setActionError(e instanceof Error ? e.message : "Import failed");
    } finally {
      setBusyIds(new Set());
    }
  }

  const checkedList = useMemo(() => [...checked], [checked]);

  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-neutral-500">
        Discover papers
      </h2>

      <div className="mt-3 rounded-md border border-neutral-200 bg-white p-4">
        <div className="text-sm font-semibold text-neutral-900">
          Autonomous literature discovery
        </div>
        <p className="mt-1 text-[13px] leading-relaxed text-neutral-600">
          The research agent searches OpenAlex for papers relevant to question v
          {questionVersion}, evaluates them against this project, and recommends
          what to read next. Nothing is added without your approval.
        </p>
        <p className="mt-1 font-mono text-[11px] text-neutral-500">
          Budget: 2 rounds · ≤3 queries/round · ≤50 records · ≤8 shortlisted ·
          ≤5 recommended
        </p>
        <input
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder="Optional narrower focus, e.g. sliced Wasserstein vs entropic transport"
          maxLength={500}
          className="mt-2 w-full rounded border border-neutral-300 bg-white p-2 text-sm text-neutral-900"
        />
        {actionError && (
          <p className="mt-2 text-xs text-red-700">{actionError}</p>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={doStart}
            disabled={starting}
            className="rounded-full bg-neutral-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {starting ? "Starting…" : "Discover papers"}
          </button>
          {activeJob && isJobActive(activeJob) && (
            <span className="text-xs text-neutral-600">
              {stageLabel(activeJob.stage)}
              {activeJob.stage_detail ? ` — ${activeJob.stage_detail}` : ""}
            </span>
          )}
        </div>
      </div>

      {loadError && <p className="mt-3 text-xs text-red-700">{loadError}</p>}

      {runs.length > 0 && run && (
        <div className="mt-3 flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {runs.length > 1 && (
              <select
                value={run.run_id}
                onChange={(e) => {
                  setSelectedRunId(e.target.value);
                  setChecked(new Set());
                  setFilter("ALL");
                }}
                className="rounded border border-neutral-300 bg-white p-1.5 text-xs text-neutral-800"
                aria-label="Discovery run"
              >
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {formatTime(r.created_at)} · {r.counts.recommended ?? 0}{" "}
                    recommended
                    {isRunStale(r, questionVersion) ? " · stale" : ""}
                  </option>
                ))}
              </select>
            )}
            <span className="font-mono text-[11px] text-neutral-500">
              {run.run_id} · question v{run.research_question_version}
            </span>
            {stale && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold text-amber-900">
                Stale — question has changed
              </span>
            )}
            {run.status === "failed" && (
              <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-900">
                Failed: {run.error}
              </span>
            )}
          </div>

          {run.focus && (
            <p className="text-[13px] text-neutral-600">
              <span className="font-medium">Focus:</span> {run.focus}
            </p>
          )}
          <p className="font-mono text-[11px] text-neutral-500">
            {run.counts.retrieved ?? 0} retrieved ·{" "}
            {run.counts.duplicates ?? 0} duplicates ·{" "}
            {run.counts.shortlisted ?? 0} shortlisted ·{" "}
            {run.counts.recommended ?? 0} recommended
          </p>
          {run.limitations && (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-3">
              <p className="text-[13px] leading-relaxed text-amber-950">
                {run.limitations}
              </p>
            </div>
          )}

          <RunProvenance projectId={projectId} run={run} />

          <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Candidate filters">
            {DISCOVERY_FILTERS.map((f) => {
              const n = filterCandidates(run.candidates, f).length;
              const active = filter === f;
              return (
                <button
                  key={f}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => setFilter(f)}
                  className={`rounded-full border px-3 py-1 text-xs font-medium ${
                    active
                      ? "border-neutral-900 bg-neutral-900 text-white"
                      : "border-neutral-300 text-neutral-700 hover:border-neutral-500"
                  }`}
                >
                  {filterLabel(f)} ({n})
                </button>
              );
            })}
          </div>

          {checkedList.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-indigo-300 bg-indigo-50 p-3">
              <span className="text-xs font-medium text-indigo-950">
                {checkedList.length} selected
              </span>
              <button
                type="button"
                onClick={() => doDecide(checkedList, "approve")}
                disabled={busyIds.size > 0}
                className="rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700 disabled:opacity-40"
              >
                Approve selected
              </button>
              <button
                type="button"
                onClick={() => doImport(checkedList)}
                disabled={busyIds.size > 0}
                className="rounded-full bg-indigo-900 px-3 py-1 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-40"
              >
                Import selected
              </button>
              <button
                type="button"
                onClick={() => doDecide(checkedList, "dismiss")}
                disabled={busyIds.size > 0}
                className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500 disabled:opacity-40"
              >
                Dismiss selected
              </button>
            </div>
          )}

          {lastImport && (
            <div className="rounded-md border border-neutral-200 bg-white px-4 py-3">
              <div className="text-[13px] font-medium text-neutral-800">
                Last import ({lastImport.jobId})
              </div>
              <ul className="mt-1 flex flex-col gap-1">
                {lastImport.items.map((item) => (
                  <li key={item.candidate_id} className="text-xs text-neutral-600">
                    <span className="font-mono">{item.candidate_id}</span>:{" "}
                    {item.ok ? (
                      <span className="text-green-800">
                        imported{item.note ? ` — ${item.note}` : ""}
                        {item.paper_id ? ` (${item.paper_id})` : ""}
                      </span>
                    ) : (
                      <span className="text-red-700">{item.error}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {visible.length === 0 && (
            <p className="text-sm text-neutral-600">
              No candidates in this view — try another filter.
            </p>
          )}
          {visible.map((c) => (
            <CandidateCard
              key={c.candidate_id}
              projectId={projectId}
              candidate={c}
              selected={checked.has(c.candidate_id)}
              busy={busyIds.has(c.candidate_id)}
              onToggleSelect={() =>
                setChecked((prev) => {
                  const next = new Set(prev);
                  if (next.has(c.candidate_id)) next.delete(c.candidate_id);
                  else next.add(c.candidate_id);
                  return next;
                })
              }
              onDecide={(action) => doDecide([c.candidate_id], action)}
              onImport={() => doImport([c.candidate_id])}
            />
          ))}
        </div>
      )}

      {runs.length === 0 && !loadError && (
        <p className="mt-3 text-sm text-neutral-600">
          No discovery runs yet — start one above. It works even before any
          papers are uploaded.
        </p>
      )}
    </section>
  );
}
