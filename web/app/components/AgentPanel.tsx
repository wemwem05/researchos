"use client";

import { useState } from "react";
import {
  AGENT_API_BASE,
  AGENT_ROLES,
  SPARK_OPEN_MODEL_ID,
  SPARK_OPEN_PROVIDER,
  formatLatency,
  shortThread,
  statusLabel,
  timelineSteps,
  type AgentRunRecord,
  type AgentRunStatus,
  type AgentTelemetrySummary,
  type TimelineState,
} from "../lib/agent";

const DOT: Record<TimelineState, string> = {
  done: "bg-green-600",
  active: "bg-indigo-600",
  waiting: "bg-amber-500",
  skipped: "bg-slate-300",
};

const STATUS_BADGE: Record<AgentRunStatus, string> = {
  running: "bg-indigo-100 text-indigo-900",
  awaiting_human: "bg-amber-100 text-amber-900",
  completed: "bg-green-100 text-green-900",
  discarded: "bg-slate-200 text-slate-700",
  failed: "bg-red-100 text-red-900",
};

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">{children}</div>
  );
}

export function AgentRunSection({ run }: { run: AgentRunRecord | null }) {
  if (!run) return null;
  const steps = timelineSteps(run);
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-slate-900">Agent run</div>
          <p className="mt-0.5 font-mono text-[11px] text-slate-500">
            {run.thread_id} · paper {run.paper_id}
            {run.kind === "verify" && run.claim_id ? ` · claim ${run.claim_id}` : ""}
          </p>
        </div>
        <span
          className={`rounded px-2 py-0.5 text-[11px] font-semibold ${STATUS_BADGE[run.status]}`}
        >
          {statusLabel(run.status)}
        </span>
      </div>
      <ol className="mt-3 flex flex-col gap-2">
        {steps.map((s) => (
          <li key={s.id} className="flex items-start gap-2.5">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOT[s.state]}`} />
            <div className="min-w-0">
              <span
                className={`text-[13px] font-medium ${s.state === "skipped" ? "text-slate-400" : "text-slate-900"}`}
              >
                {s.label}
              </span>
              {s.detail && (
                <span className="block truncate text-xs text-slate-500" title={s.detail}>
                  {s.detail}
                </span>
              )}
            </div>
          </li>
        ))}
      </ol>
      {run.telemetry.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            Advanced details · {run.telemetry.length} model calls
          </summary>
          <p className="mt-1 break-words text-xs leading-relaxed text-slate-500">
            {run.telemetry.filter((t) => t.success).length} of {run.telemetry.length}{" "}
            open-weight model calls succeeded
            {run.revision_path.length > 0 && ` · path: ${run.revision_path.join(" → ")}`}
            {" · "}
            {run.telemetry.map((t) => `${t.node} ${formatLatency(t.latency_ms)}`).join(" · ")}
          </p>
        </details>
      )}
    </Card>
  );
}

export function ReviewCard({
  run,
  live,
  onSubmitted,
  reviewUrl,
}: {
  run: AgentRunRecord;
  live: boolean;
  onSubmitted: () => void;
  reviewUrl?: string;
}) {
  const req = run.human_review_request;
  const [choice, setChoice] = useState<string | null>(null);
  const [freeText, setFreeText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!req) return null;

  async function submit() {
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      const url =
        reviewUrl ?? `${AGENT_API_BASE}/api/agent/runs/${run.thread_id}/review`;
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice, free_text: freeText }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || r.statusText);
      onSubmitted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Resume failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-xl border border-amber-300 bg-amber-50 p-5">
      <div className="text-sm font-semibold text-amber-950">Researcher review required</div>
      <p className="mt-1 font-serif text-base leading-snug text-slate-900">{req.question}</p>
      <p className="mt-1 text-[13px] text-slate-600">Why ResearchOS stopped: {req.why_stopped}</p>

      <div className="mt-3 grid gap-2">
        <div className="rounded border border-slate-200 bg-white p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Investigator interpretation
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-slate-800">{req.investigator_position}</p>
        </div>
        <div className="rounded border border-slate-200 bg-white p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
            Critic concern
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-slate-800">{req.critic_concern}</p>
        </div>
      </div>

      <div className="mt-3 grid gap-2">
        <details className="rounded border border-slate-200 bg-white px-3 py-2">
          <summary className="cursor-pointer text-[13px] font-medium text-slate-800">
            Evidence from prior findings
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-slate-600">
            {req.evidence_a}
          </p>
        </details>
        <details className="rounded border border-slate-200 bg-white px-3 py-2">
          <summary className="cursor-pointer text-[13px] font-medium text-slate-800">
            Evidence from the new paper
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-slate-600">
            {req.evidence_b}
          </p>
        </details>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {req.options.map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => setChoice(opt)}
            className={`rounded-lg border px-3 py-1.5 text-[13px] font-medium ${
              choice === opt
                ? "border-indigo-600 bg-indigo-600 text-white"
                : "border-slate-300 bg-white text-slate-800 hover:border-slate-500"
            }`}
          >
            {opt}
          </button>
        ))}
      </div>
      {req.allow_free_text && (
        <textarea
          value={freeText}
          onChange={(e) => setFreeText(e.target.value)}
          placeholder="Add your own judgment (optional free text)…"
          rows={2}
          className="mt-2 w-full rounded border border-slate-300 bg-white p-2 text-[13px] text-slate-900 placeholder:text-slate-400"
        />
      )}
      {live ? (
        <div className="mt-2">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={submit}
              disabled={!choice || busy}
              className="rounded-lg bg-indigo-600 px-4 py-1.5 text-[13px] font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {busy ? "Resuming…" : "Submit judgment & resume"}
            </button>
            {error && <span className="text-xs text-red-700">{error}</span>}
          </div>
          <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
            Your judgment resumes the agent from its checkpoint — nothing is
            recomputed, and the paper attaches with the outcome your choice
            implies.
          </p>
        </div>
      ) : (
        <p className="mt-2 font-mono text-[11px] leading-relaxed text-slate-600">
          Live agent server not running — resume with:
          <br />
          agent.py review --thread-id {shortThread(run.thread_id)} --choice "…"
        </p>
      )}
    </div>
  );
}

export function SyntheticWalkthrough({ demo }: { demo: AgentRunRecord | null }) {
  if (!demo || !demo.human_review_request) return null;
  const req = demo.human_review_request;
  return (
    <div className="rounded-xl border border-dashed border-amber-400 bg-white p-5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-amber-700">
        Human-review walkthrough · synthetic demo fixture
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-slate-700">{req.question}</p>
      <p className="mt-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">
        Response options (shown for illustration — not clickable)
      </p>
      <ul className="mt-1 list-disc pl-5 text-xs leading-relaxed text-slate-600">
        {req.options.map((opt) => (
          <li key={opt}>{opt}</li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-500">
        Canned fixture with imaginary papers — no real evidence, no model calls. It
        exercises the same interrupt/resume path as a live review.
      </p>
    </div>
  );
}

export function ModelUsageSection({ summary }: { summary: AgentTelemetrySummary | null }) {
  return (
    <Card>
      <div className="text-sm font-semibold text-slate-900">AI research agent</div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[13px]">
        <dt className="text-slate-500">Model</dt>
        <dd className="text-slate-900">Muse Spark Open</dd>
        <dt className="text-slate-500">Hackathon model</dt>
        <dd className="break-all font-mono text-[12px] text-slate-900">{SPARK_OPEN_MODEL_ID}</dd>
        <dt className="text-slate-500">Provider</dt>
        <dd className="text-slate-900">{SPARK_OPEN_PROVIDER}</dd>
        <dt className="text-slate-500">Agent roles</dt>
        <dd className="text-slate-900">{[...AGENT_ROLES].join(" · ")}</dd>
        {summary && (
          <>
            <dt className="text-slate-500">Calls</dt>
            <dd className="text-slate-900">{summary.calls}</dd>
            <dt className="text-slate-500">Average latency</dt>
            <dd className="text-slate-900">{formatLatency(summary.avg_latency_ms)}</dd>
          </>
        )}
      </dl>
      <p className="mt-2 text-xs leading-relaxed text-slate-500">
        The open-weight model investigates new papers, critiques its own conclusions,
        and revises — PDF perception still uses hosted Muse. Telemetry ships with
        every run.
      </p>
    </Card>
  );
}
