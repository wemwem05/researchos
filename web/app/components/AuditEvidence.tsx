"use client";

import {
  AUDIT_CHECK_BADGE,
  AUDIT_OVERALL_BADGE,
  AUDIT_SEVERITY_BADGE,
  auditCheckLabel,
  comparabilityLabel,
  type ScientificAuditView,
} from "../lib/audit";
import { formatLatency } from "../lib/agent";

function BulletList({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="list-disc pl-5 text-[13px] leading-relaxed text-slate-700">
      {items.map((x, i) => (
        <li key={i}>{x}</li>
      ))}
    </ul>
  );
}

function Subhead({ children }: { children: string }) {
  return (
    <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
      {children}
    </div>
  );
}

export default function AuditEvidence({ audit }: { audit: ScientificAuditView }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-semibold text-slate-900">
          Evidence audit {audit.audit_id} · claim {audit.claim_id}
        </div>
        <span
          className={`rounded px-2 py-0.5 text-[11px] font-semibold ${AUDIT_OVERALL_BADGE[audit.overall_assessment] ?? "bg-slate-200 text-slate-700"}`}
        >
          {audit.overall_assessment}
        </span>
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-slate-700">{audit.claim_text}</p>
      <p className="mt-0.5 font-mono text-[11px] text-slate-500">
        {audit.paper_id} · {audit.claim_id}
        {audit.claim_type.length > 0 && ` · ${audit.claim_type.join(", ")}`}
      </p>
      {audit.audit_scope && (
        <p className="mt-1 text-xs leading-relaxed text-slate-600">{audit.audit_scope}</p>
      )}
      {audit.assessment_rationale && (
        <p className="mt-1 text-xs leading-relaxed text-slate-600">
          <span className="font-medium text-slate-700">Why this assessment: </span>
          {audit.assessment_rationale}
        </p>
      )}

      {audit.checks.length > 0 && (
        <div className="mt-3">
          <Subhead>Numerical checks (recomputed deterministically)</Subhead>
          <div className="mt-1 overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500">
                  <th className="pb-1 pr-3 font-medium">check</th>
                  <th className="pb-1 pr-3 font-medium">result</th>
                  <th className="pb-1 font-medium">recomputation</th>
                </tr>
              </thead>
              <tbody>
                {audit.checks.map((c, i) => (
                  <tr
                    key={`${c.check_id || "check"}-${i}`}
                    className="border-t border-slate-100 align-top"
                  >
                    <td className="py-1 pr-3 text-slate-800">
                      <span className="font-mono text-[11px] text-slate-500">{c.check_type}</span>
                      <br />
                      {c.description}
                      {c.evidence_refs.length > 0 && (
                        <span className="font-mono text-[11px] text-slate-500">
                          <br />→ {c.evidence_refs.join(", ")}
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap py-1 pr-3">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${AUDIT_CHECK_BADGE[c.status]}`}
                      >
                        {auditCheckLabel(c.status)}
                      </span>
                    </td>
                    <td className="py-1 font-mono text-[11px] leading-relaxed text-slate-600">
                      {c.actual_computation_or_reasoning || "—"}
                      {c.limitations && (
                        <span className="block font-sans text-[11px] text-slate-500">
                          Limit: {c.limitations}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {audit.methodology_findings.length > 0 && (
        <div className="mt-3">
          <Subhead>Methodology</Subhead>
          <ul className="mt-1 flex flex-col gap-1.5">
            {audit.methodology_findings.map((m, i) => (
              <li key={i} className="text-[13px] leading-relaxed text-slate-700">
                <span
                  className={`mr-1.5 rounded px-1.5 py-0.5 text-[11px] font-semibold ${AUDIT_SEVERITY_BADGE[m.severity] ?? "bg-slate-200 text-slate-700"}`}
                >
                  {m.severity.replace(/_/g, " ").toLowerCase()}
                </span>
                {m.finding}
                {m.evidence && <span className="text-slate-500"> — {m.evidence}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {audit.cross_paper_findings.length > 0 && (
        <div className="mt-3">
          <Subhead>Project comparison</Subhead>
          <ul className="mt-1 flex flex-col gap-1.5">
            {audit.cross_paper_findings.map((c, i) => (
              <li key={i} className="text-[13px] leading-relaxed text-slate-700">
                <span className="font-medium">{comparabilityLabel(c.comparability)}</span>
                {c.finding_ref && (
                  <span className="font-mono text-[11px] text-slate-500"> · {c.finding_ref}</span>
                )}
                {c.detail && <span> — {c.detail}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {(audit.external_sources.length > 0 || audit.external_notes.length > 0) && (
        <div className="mt-3">
          <Subhead>External literature</Subhead>
          {audit.external_queries.length > 0 && (
            <p className="mt-1 font-mono text-[11px] leading-relaxed text-slate-500">
              Queries: {audit.external_queries.join(" · ")}
            </p>
          )}
          <ul className="mt-1 flex flex-col gap-1.5">
            {audit.external_sources.map((s, i) => (
              <li key={i} className="text-[13px] leading-relaxed text-slate-700">
                {s.title || s.doi || s.openalex_id}
                {s.year ? ` (${s.year})` : ""}
                {s.evidence_level && (
                  <span className="ml-1.5 rounded bg-slate-200 px-1.5 py-0.5 text-[11px] font-semibold text-slate-700">
                    {s.evidence_level}
                  </span>
                )}
                {s.doi && (
                  <span className="block font-mono text-[11px] text-slate-500">{s.doi}</span>
                )}
              </li>
            ))}
          </ul>
          <BulletList items={audit.external_notes} />
        </div>
      )}

      {audit.unresolved_questions.length > 0 && (
        <div className="mt-3">
          <Subhead>Unresolved questions</Subhead>
          <div className="mt-1">
            <BulletList items={audit.unresolved_questions} />
          </div>
        </div>
      )}

      {audit.limitations.length > 0 && (
        <div className="mt-3">
          <Subhead>Limitations</Subhead>
          <div className="mt-1">
            <BulletList items={audit.limitations} />
          </div>
        </div>
      )}

      {audit.model_telemetry.length > 0 && (
        <p className="mt-3 font-mono text-[11px] leading-relaxed text-slate-500">
          {audit.model_telemetry.length} Spark Open calls ·{" "}
          {audit.model_telemetry.map((t) => `${t.node} ${formatLatency(t.latency_ms)}`).join(" · ")}
        </p>
      )}
    </div>
  );
}
