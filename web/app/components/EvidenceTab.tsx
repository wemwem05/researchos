"use client";

import { useState } from "react";
import Link from "next/link";
import AuditEvidence from "./AuditEvidence";
import VerificationEvidence from "./VerificationEvidence";
import { EmptyState, PageHeader, SectionHeader } from "./ui";
import type { ScientificAuditView } from "../lib/audit";
import type { VerificationEvidenceView } from "../lib/verification";

type EvidenceView = "audits" | "experiments";

export default function EvidenceTab({
  projectId,
  audits,
  verifications,
}: {
  projectId: string;
  audits: ScientificAuditView[];
  verifications: VerificationEvidenceView[];
}) {
  const [view, setView] = useState<EvidenceView>("audits");

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Evidence"
        description="Two independent ways ResearchOS checks a claim. Audits recompute and reason from the paper; experiments execute code and measure."
      />

      <div
        role="tablist"
        aria-label="Evidence types"
        className="flex gap-2 border-b border-slate-200 pb-3"
      >
        {(
          [
            { id: "audits", label: `Scientific audits (${audits.length})` },
            {
              id: "experiments",
              label: `Computational experiments (${verifications.length})`,
            },
          ] as { id: EvidenceView; label: string }[]
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={view === t.id}
            onClick={() => setView(t.id)}
            className={`rounded-lg px-4 py-2 text-sm font-medium ${
              view === t.id
                ? "bg-indigo-600 text-white"
                : "border border-slate-300 bg-white text-slate-600 hover:border-slate-400"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {view === "audits" && (
        <section>
          <SectionHeader title="Checks of arithmetic, methodology and consistency" />
          <p className="-mt-1 mb-4 max-w-2xl text-[13px] leading-relaxed text-slate-500">
            An audit recomputes a claim&apos;s numbers, assesses its methodology,
            and compares it against project evidence. No code is executed — for
            executed experiments, switch to the other tab.
          </p>
          {audits.length === 0 ? (
            <EmptyState
              title="No audits yet"
              body="Open a paper in the Reader, select a claim, and choose “Audit scientific claim”. The report lands here."
              action={
                <Link
                  href={`/projects/${projectId}?tab=papers`}
                  className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
                >
                  Go to papers
                </Link>
              }
            />
          ) : (
            <div className="flex flex-col gap-4">
              {audits.map((a) => (
                <div key={a.audit_id}>
                  <AuditEvidence audit={a} />
                  <Link
                    href={`/projects/${projectId}/read/${a.paper_id}?select=${encodeURIComponent(a.claim_id)}`}
                    className="mt-1 inline-block text-[13px] font-medium text-indigo-600 hover:text-indigo-700 hover:underline"
                  >
                    Open claim in reader →
                  </Link>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {view === "experiments" && (
        <section>
          <SectionHeader title="Sandbox execution and measurements" />
          <p className="-mt-1 mb-4 max-w-2xl text-[13px] leading-relaxed text-slate-500">
            Each experiment shows its plan, executed code, measured numbers,
            and artifacts. A passing test supports the tested aspect only — it
            never proves the paper&apos;s conclusion or reproduces its result.
          </p>
          {verifications.length === 0 ? (
            <EmptyState
              title="No experiments yet"
              body="In the Reader, choose “Verify computationally” on a feasible performance or scaling claim."
              action={
                <Link
                  href={`/projects/${projectId}?tab=papers`}
                  className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700"
                >
                  Go to papers
                </Link>
              }
            />
          ) : (
            <div className="flex flex-col gap-4">
              {verifications.map((v) => (
                <VerificationEvidence key={v.id} evidence={v} />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
