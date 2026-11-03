"use client";

import { useCallback, useEffect, useState } from "react";
import type { SciObject } from "../lib/types";
import {
  AGENT_API_BASE,
  probeAgentApi,
  type AgentRunRecord,
} from "../lib/agent";
import { fetchLiveVerifications, startVerification, type VerificationEvidenceView } from "../lib/verification";
import { AgentRunSection, ReviewCard } from "./AgentPanel";
import VerificationEvidence from "./VerificationEvidence";

type Phase = "idle" | "confirm" | "starting" | "running" | "done";

export default function VerifyClaim({ paperId, claim }: { paperId: string; claim: SciObject }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [live, setLive] = useState<boolean | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunRecord | null>(null);
  const [evidence, setEvidence] = useState<VerificationEvidenceView | null>(null);
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
        const list = await fetchLiveVerifications().catch(() => []);
        const found = list.find((v) => v.thread_id === threadId) ?? null;
        if (found) {
          const base = `${AGENT_API_BASE}/api/agent/verifications/${threadId}/files`;
          found.web_code = `${base}/experiment.py`;
          found.web_measurements = `${base}/results.csv`;
          found.web_plot = `${base}/scaling.png`;
          found.web_stdout = `${base}/stdout.txt`;
          found.web_stderr = `${base}/stderr.txt`;
          found.web_evidence = `${base}/evidence.json`;
        }
        setEvidence(found);
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
      const { thread_id } = await startVerification(paperId, claim.id);
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
        className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 hover:border-neutral-500"
      >
        Verify computationally
      </button>
    );
  }

  if (phase === "confirm") {
    return (
      <div className="rounded-md border border-neutral-200 bg-white p-3">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
          Confirm computational verification
        </div>
        <p className="mt-1 text-[13px] font-medium leading-relaxed text-neutral-900">{claim.title}</p>
        <p className="mt-0.5 font-mono text-[11px] text-neutral-500">
          {paperId} · {claim.id}
          {claim.related_objects.length > 0 && ` · refs ${claim.related_objects.join(", ")}`}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-neutral-600">
          Plans a CPU benchmark and runs it in an isolated Modal sandbox (a few minutes).
          Nothing executes until you confirm.
        </p>
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
            Confirm &amp; run
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
    return <p className="text-xs text-neutral-500">Starting verification…</p>;
  }

  return (
    <div className="flex flex-col gap-2">
      {!run && <p className="text-xs text-neutral-500">Starting verification run…</p>}
      <AgentRunSection run={run} />
      {run && run.status === "awaiting_human" && (
        <ReviewCard run={run} live={true} onSubmitted={refresh} />
      )}
      {run && run.status === "failed" && (
        <p className="text-xs text-red-700">Verification failed: {run.error}</p>
      )}
      {run && run.status === "discarded" && (
        <p className="text-xs text-neutral-600">Verification discarded — project untouched.</p>
      )}
      {phase === "done" &&
        (evidence ? (
          <VerificationEvidence evidence={evidence} />
        ) : (
          <p className="text-xs text-neutral-500">
            Run completed. Evidence will appear in the project view after stage-web.{" "}
            <a className="underline" href="/project">
              Open project
            </a>
          </p>
        ))}
      {error && <p className="text-xs text-red-700">{error}</p>}
    </div>
  );
}
