"use client";

import { useCallback, useEffect, useState } from "react";
import type { SciObject } from "../lib/types";
import {
  AGENT_API_BASE,
  probeAgentApi,
  type AgentRunRecord,
} from "../lib/agent";
import {
  EXTERNAL_SEARCH_BUDGET,
  fetchLiveAudits,
  startAudit,
  type ScientificAuditView,
} from "../lib/audit";
import { AgentRunSection, ReviewCard } from "./AgentPanel";
import AuditEvidence from "./AuditEvidence";

type Phase = "idle" | "confirm" | "starting" | "running" | "done";

export default function AuditClaim({ paperId, claim }: { paperId: string; claim: SciObject }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [live, setLive] = useState<boolean | null>(null);
  const [external, setExternal] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunRecord | null>(null);
  const [audit, setAudit] = useState<ScientificAuditView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    probeAgentApi()
      .then((ok) => {
        if (!cancelled) setLive(ok);
      })
      .catch(() => {
        if (!cancelled) setLive(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!threadId) return;
    try {
      const res = await fetch(`${AGENT_API_BASE}/api/agent/runs/${threadId}`);
      // The background run may not have created its record yet; keep
      // polling instead of rendering the 404 body as a run.
      if (res.status === 404) return;
      if (!res.ok) throw new Error(`Run status ${res.status}`);
      const detail = (await res.json()) as AgentRunRecord;
      if (!Array.isArray(detail.transitions)) return;
      setRun(detail);
      if (detail.status === "completed") {
        const list = await fetchLiveAudits().catch(() => []);
        setAudit(list.find((a) => a.thread_id === threadId) ?? null);
        setPhase("done");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refresh failed");
    }
  }, [threadId]);

  useEffect(() => {
    if (phase !== "running" || !threadId) return;
    refresh();
    if (run && !["running", "awaiting_human"].includes(run.status)) return;
    const id = setTimeout(() => setTick((x) => x + 1), 5000);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, threadId, tick]);

  async function start() {
    setPhase("starting");
    setError(null);
    try {
      const { thread_id } = await startAudit(paperId, claim.id, external);
      setThreadId(thread_id);
      setPhase("running");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Start failed");
      setPhase("confirm");
    }
  }

  if (phase === "idle") {
    return (
      <button
        type="button"
        onClick={() => setPhase("confirm")}
        className="rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700"
      >
        Audit scientific claim
      </button>
    );
  }

  if (phase === "confirm") {
    return (
      <div className="rounded-md border border-neutral-200 bg-white p-3">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
          Confirm evidence audit
        </div>
        <p className="mt-1 text-[13px] font-medium leading-relaxed text-neutral-900">{claim.title}</p>
        <p className="mt-0.5 font-mono text-[11px] text-neutral-500">
          {paperId} · {claim.id}
          {claim.related_objects.length > 0 && ` · refs ${claim.related_objects.join(", ")}`}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-neutral-600">
          Audits the claim against its paper evidence: recomputed numbers, methodology,
          comparison with project findings
          {external ? ", and bounded external literature." : "."} No code is executed.
        </p>
        <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs text-neutral-700">
          <input
            type="checkbox"
            checked={external}
            onChange={(e) => setExternal(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            Include external literature (OpenAlex, abstract-level only; at most{" "}
            {EXTERNAL_SEARCH_BUDGET.maxQueries} queries, {EXTERNAL_SEARCH_BUDGET.maxRecords}{" "}
            records, {EXTERNAL_SEARCH_BUDGET.maxPapers} papers)
          </span>
        </label>
        {live === false && (
          <p className="mt-1 font-mono text-[11px] leading-relaxed text-amber-800">
            Agent server not running — start it first: agent.py serve --project-dir … --port 8765
          </p>
        )}
        {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            onClick={start}
            disabled={live !== true}
            className="rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Confirm &amp; audit
          </button>
          <button
            type="button"
            onClick={() => setPhase("idle")}
            className="rounded-full border border-neutral-300 px-3 py-1 text-xs text-neutral-700 hover:border-neutral-500"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  if (phase === "starting") {
    return <p className="text-xs text-neutral-500">Starting audit…</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      {!run && <p className="text-xs text-neutral-500">Starting audit run…</p>}
      <AgentRunSection run={run} />
      {run && run.status === "awaiting_human" && (
        <ReviewCard run={run} live={true} onSubmitted={refresh} />
      )}
      {run && run.status === "failed" && (
        <p className="text-xs text-red-700">Audit failed: {run.error}</p>
      )}
      {run && run.status === "discarded" && (
        <p className="text-xs text-neutral-600">Audit discarded — project untouched.</p>
      )}
      {phase === "done" &&
        (audit ? (
          <AuditEvidence audit={audit} />
        ) : (
          <p className="text-xs text-neutral-500">
            Run completed. The report will appear in the project view after stage-web.{" "}
            <a className="underline" href="/project">
              Open project
            </a>
          </p>
        ))}
      {error && <p className="text-xs text-red-700">{error}</p>}
    </div>
  );
}
