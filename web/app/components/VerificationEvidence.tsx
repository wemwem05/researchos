"use client";

import {
  VERIFY_STATUS_BADGE,
  formatBytes,
  formatSeconds,
  verifyStatusLabel,
  type VerificationEvidenceView,
} from "../lib/verification";

function BulletList({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="list-disc pl-5 text-[13px] leading-relaxed text-neutral-700">
      {items.map((x, i) => (
        <li key={i}>{x}</li>
      ))}
    </ul>
  );
}

export default function VerificationEvidence({ evidence }: { evidence: VerificationEvidenceView }) {
  const cells = Object.entries(evidence.result_summary?.cells ?? {});
  const ratios = Object.entries(evidence.result_summary?.median_ratios ?? {});
  return (
    <div className="rounded-md border border-neutral-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-semibold text-neutral-900">
          Verification {evidence.id} · claim {evidence.claim_id}
        </div>
        <span
          className={`rounded px-2 py-0.5 text-[11px] font-semibold ${VERIFY_STATUS_BADGE[evidence.status]}`}
        >
          {verifyStatusLabel(evidence.status)}
        </span>
      </div>
      <p className="mt-1 text-[13px] leading-relaxed text-neutral-700">{evidence.claim_statement}</p>

      {evidence.observed.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Directly observed
          </div>
          <div className="mt-1">
            <BulletList items={evidence.observed} />
          </div>
        </div>
      )}

      {cells.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-neutral-500">
                <th className="pb-1 pr-3 font-medium">cell</th>
                <th className="pb-1 pr-3 font-medium">median</th>
                <th className="pb-1 pr-3 font-medium">IQR</th>
                <th className="pb-1 font-medium">median mem</th>
              </tr>
            </thead>
            <tbody>
              {cells.map(([key, c]) => (
                <tr key={key} className="border-t border-neutral-100 text-neutral-800">
                  <td className="py-1 pr-3 font-mono text-[11px]">{key}</td>
                  <td className="py-1 pr-3">{formatSeconds(c.median_s)}</td>
                  <td className="py-1 pr-3">
                    {formatSeconds(c.iqr_s[0])} – {formatSeconds(c.iqr_s[1])}
                  </td>
                  <td className="py-1">{formatBytes(c.median_mem_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {evidence.web_plot && (
        <div className="mt-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={evidence.web_plot}
            alt="Scaling behavior measured in the sandbox"
            className="rounded border border-neutral-200 bg-white"
          />
        </div>
      )}

      {ratios.length > 0 && (
        <p className="mt-2 text-xs leading-relaxed text-neutral-600">
          Observed scaling ratios (descriptive, not asymptotic proof):{" "}
          {ratios.map(([k, v]) => `${k} = ${v.toFixed(2)}`).join("; ")}
        </p>
      )}
      {evidence.scaling_comment && (
        <p className="mt-1 text-xs leading-relaxed text-neutral-600">{evidence.scaling_comment}</p>
      )}

      {evidence.supported_conclusions.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Interpretation (model-generated, not measured)
          </div>
          <div className="mt-1">
            <BulletList items={evidence.supported_conclusions} />
          </div>
        </div>
      )}

      {evidence.criteria.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Success criteria
          </div>
          <ul className="mt-1 flex flex-col gap-1">
            {evidence.criteria.map((c, i) => (
              <li key={i} className="text-xs leading-relaxed text-neutral-700">
                <span
                  className={`mr-1.5 inline-block h-2 w-2 rounded-full ${
                    c.evaluated ? (c.met ? "bg-green-600" : "bg-red-500") : "bg-neutral-300"
                  }`}
                />
                <span className="font-medium">{c.name}</span>
                <span className="text-neutral-500"> — {c.detail || c.rule}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {(evidence.partial.length > 0 || evidence.out_of_scope.length > 0) && (
        <div className="mt-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Partially tested / out of scope
          </div>
          <div className="mt-1">
            <BulletList items={[...evidence.partial, ...evidence.out_of_scope]} />
          </div>
        </div>
      )}

      {evidence.limitations.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
            Limitations
          </div>
          <div className="mt-1">
            <BulletList items={evidence.limitations} />
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {evidence.web_measurements && (
          <a className="underline text-neutral-700" href={evidence.web_measurements}>
            results.csv
          </a>
        )}
        {evidence.web_code && (
          <a className="underline text-neutral-700" href={evidence.web_code}>
            executed code
          </a>
        )}
        {evidence.web_stdout && (
          <a className="underline text-neutral-700" href={evidence.web_stdout}>
            stdout
          </a>
        )}
        {evidence.web_stderr && (
          <a className="underline text-neutral-700" href={evidence.web_stderr}>
            stderr
          </a>
        )}
        {evidence.web_evidence && (
          <a className="underline text-neutral-700" href={evidence.web_evidence}>
            full evidence.json
          </a>
        )}
      </div>
      <p className="mt-2 font-mono text-[11px] leading-relaxed text-neutral-500">
        code {evidence.code_sha256.slice(0, 16)}… · sandbox {evidence.modal.sandbox_id || "—"} ·
        exit {evidence.modal.returncode ?? "—"} · {formatSeconds(evidence.modal.duration_s)} ·{" "}
        {evidence.attempts} attempt(s) · {evidence.repairs.length} repair(s)
      </p>
    </div>
  );
}
