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
  skipped: "bg-neutral-300",
};

const STATUS_BADGE: Record<AgentRunStatus, string> = {
  running: "bg-indigo-100 text-indigo-900",
  awaiting_human: "bg-amber-100 text-amber-900",
  completed: "bg-green-100 text-green-900",
  discarded: "bg-neutral-200 text-neutral-700",
  failed: "bg-red-100 text-red-900",
};

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-neutral-200 bg-white p-4">{children}</div>
  );
}

export function AgentRunSection({ run }: { run: AgentRunRecord | null }) {
  if (!run) return null;
  const steps = timelineSteps(run);
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-neutral-900">Agent run</div>
          <p className="mt-0.5 font-mono text-[11px] text-neutral-500">
            {run.thread_id} · paper {run.paper_id}
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
                className={`text-[13px] font-medium ${s.state === "skipped" ? "text-neutral-400" : "text-neutral-900"}`}
              >
                {s.label}
              </span>
              {s.detail && (
                <span className="block truncate text-xs text-neutral-500" title={s.detail}>
                  {s.detail}
                </span>
              )}
            </div>
          </li>
        ))}
      </ol>
      {run.telemetry.length > 0 && (
        <p className="mt-3 text-xs text-neutral-500">
          {run.telemetry.length} open-weight model calls ·{" "}
          {run.telemetry.filter((t) => t.success).length} succeeded ·{" "}
          {run.revision_path.length > 0 && `path: ${run.revision_path.join(" → ")}`}
        </p>
      )}
    </Card>
  );
}

export function ReviewCard({
  run,
  live,
  onSubmitted,
}: {
  run: AgentRunRecord;
  live: boolean;
  onSubmitted: () => void;
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
      const r = await fetch(`${AGENT_API_BASE}/api/agent/runs/${run.thread_id}/review`, {
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
    <div className="rounded-md border border-amber-300 bg-amber-50 p-4">
      <div className="text-sm font-semibold text-amber-950">Researcher review required</div>
      <p className="mt-1 font-serif text-base leading-snug text-neutral-900">{req.question}</p>
      <p className="mt-1 text-[13px] text-neutral-600">Why ResearchOS stopped: {req.why_stopped}</p>

      <div className="mt-3 grid gap-2">
        <div className="rounded border border-neutral-200 bg-white p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Investigator interpretation
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-neutral-800">{req.investigator_position}</p>
        </div>
        <div className="rounded border border-neutral-200 bg-white p-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Critic concern
          </div>
          <p className="mt-1 text-[13px] leading-relaxed text-neutral-800">{req.critic_concern}</p>
        </div>
      </div>

      <div className="mt-3 grid gap-2">
        <details className="rounded border border-neutral-200 bg-white px-3 py-2">
          <summary className="cursor-pointer text-[13px] font-medium text-neutral-800">
            Evidence from prior findings
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-neutral-600">
            {req.evidence_a}
          </p>
        </details>
        <details className="rounded border border-neutral-200 bg-white px-3 py-2">
          <summary className="cursor-pointer text-[13px] font-medium text-neutral-800">
            Evidence from the new paper
          </summary>
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-neutral-600">
            {req.evidence_b}
          </p>
        </details>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {req.options.map((opt) => (
          <button
            key={opt}
            onClick={() => setChoice(opt)}
            className={`rounded-full border px-3 py-1 text-[13px] font-medium ${
              choice === opt
                ? "border-neutral-900 bg-neutral-900 text-white"
                : "border-neutral-300 bg-white text-neutral-800 hover:border-neutral-500"
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
          className="mt-2 w-full rounded border border-neutral-300 bg-white p-2 text-[13px] text-neutral-900 placeholder:text-neutral-400"
        />
      )}
      {live ? (
        <div className="mt-2 flex items-center gap-3">
          <button
            onClick={submit}
            disabled={!choice || busy}
            className="rounded-full bg-neutral-900 px-4 py-1.5 text-[13px] font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Resuming…" : "Submit judgment & resume"}
          </button>
          {error && <span className="text-xs text-red-700">{error}</span>}
        </div>
      ) : (
        <p className="mt-2 font-mono text-[11px] leading-relaxed text-neutral-600">
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
    <div className="rounded-md border border-dashed border-amber-400 bg-white p-4">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-amber-700">
        Human-review walkthrough · synthetic demo fixture
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-neutral-700">{req.question}</p>
      <p className="mt-2 text-[11px] font-medium uppercase tracking-wide text-neutral-400">
        Response options (shown for illustration — not clickable)
      </p>
      <ul className="mt-1 list-disc pl-5 text-xs leading-relaxed text-neutral-600">
        {req.options.map((opt) => (
          <li key={opt}>{opt}</li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-neutral-500">
        Canned fixture with imaginary papers — no real evidence, no model calls. It
        exercises the same interrupt/resume path as a live review.
      </p>
    </div>
  );
}

export function ModelUsageSection({ summary }: { summary: AgentTelemetrySummary | null }) {
  return (
    <Card>
      <div className="text-sm font-semibold text-neutral-900">AI research agent</div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[13px]">
        <dt className="text-neutral-500">Model</dt>
        <dd className="text-neutral-900">Muse Spark Open</dd>
        <dt className="text-neutral-500">Hackathon model</dt>
        <dd className="break-all font-mono text-[12px] text-neutral-900">{SPARK_OPEN_MODEL_ID}</dd>
        <dt className="text-neutral-500">Provider</dt>
        <dd className="text-neutral-900">{SPARK_OPEN_PROVIDER}</dd>
        <dt className="text-neutral-500">Agent roles</dt>
        <dd className="text-neutral-900">{[...AGENT_ROLES].join(" · ")}</dd>
        {summary && (
          <>
            <dt className="text-neutral-500">Calls</dt>
            <dd className="text-neutral-900">{summary.calls}</dd>
            <dt className="text-neutral-500">Average latency</dt>
            <dd className="text-neutral-900">{formatLatency(summary.avg_latency_ms)}</dd>
          </>
        )}
      </dl>
      <p className="mt-2 text-xs leading-relaxed text-neutral-500">
        The open-weight model investigates new papers, critiques its own conclusions,
        and revises — PDF perception still uses hosted Muse. Telemetry ships with
        every run.
      </p>
    </Card>
  );
}
