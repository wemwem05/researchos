"use client";

import type { LocalizedData, SciObject } from "../lib/types";
import { importanceLabel, relevanceLabel } from "../lib/relevance";
import Latex from "./Latex";
import AuditClaim from "./AuditClaim";
import VerifyClaim from "./VerifyClaim";

interface Props {
  data: LocalizedData;
  selected: SciObject | null;
  localizedCount: number;
  highCount: number;
  onSelectRelated: (objectId: string) => void;
  paperId?: string | null;
  projectId?: string | null;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
        {title}
      </h3>
      <div className="mt-1 text-sm leading-relaxed text-slate-800">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className="mt-0.5 text-sm leading-relaxed text-slate-800">{children}</div>
    </div>
  );
}

function TypeBadge({ type }: { type: SciObject["type"] }) {
  return (
    <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-700">
      {type}
    </span>
  );
}

const LEVEL_BADGE: Record<SciObject["research_relevance_level"], string> = {
  high: "bg-indigo-700 text-white",
  related: "bg-blue-100 text-blue-900",
  context: "bg-slate-200 text-slate-700",
};

export default function ResearchLens({ data, selected, localizedCount, highCount, onSelectRelated, paperId = null, projectId = null }: Props) {
  const { analysis } = data;

  if (!selected) {
    return (
      <div className="flex h-full flex-col gap-5 p-6">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Research Lens
          </div>
          <h2 className="mt-2 font-serif text-xl leading-snug text-slate-900">
            {analysis.paper_title}
          </h2>
        </div>
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
          <div className="text-xs font-medium text-slate-500">Your research question</div>
          <p className="mt-1 text-sm font-medium leading-relaxed text-slate-900">
            {analysis.research_question}
          </p>
        </div>
        <p className="text-sm text-slate-600">
          ResearchOS found {analysis.objects.length} scientific objects.{" "}
          {highCount} are highly relevant to your research question.
        </p>
        <p className="text-sm leading-relaxed text-slate-700">
          Select a highlighted equation, figure, or table to understand its role in
          this paper and its relevance to your research.
        </p>
      </div>
    );
  }

  const related = selected.related_objects
    .map((id) => analysis.objects.find((o) => o.id === id))
    .filter((o): o is SciObject => o !== undefined);

  return (
    <div className="flex flex-col gap-5 p-6">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          {selected.label && (
            <span className="text-sm font-semibold text-slate-900">{selected.label}</span>
          )}
          <TypeBadge type={selected.type} />
          <span
            className={[
              "rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
              LEVEL_BADGE[selected.research_relevance_level],
            ].join(" ")}
          >
            {relevanceLabel(selected.research_relevance_level)} relevance
          </span>
          {selected.page !== null && (
            <span className="text-xs text-slate-500">p. {selected.page}</span>
          )}
        </div>
        <h2 className="mt-1.5 font-serif text-lg leading-snug text-slate-900">
          {selected.title}
        </h2>
        <p className="mt-1 text-[13px] text-slate-600">
          <span className="font-medium">Supports:</span> {selected.claim_supported}
        </p>
        <dl className="mt-2 flex flex-col gap-1 text-[13px]">
          <div className="flex gap-2">
            <dt className="shrink-0 text-slate-500">Importance to paper:</dt>
            <dd className="font-medium text-slate-800">
              {importanceLabel(selected.paper_importance)}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt className="shrink-0 text-slate-500">Research relevance:</dt>
            <dd className="font-medium text-slate-800">
              {relevanceLabel(selected.research_relevance_level)}
            </dd>
          </div>
        </dl>
      </div>

      <Section title="What is this?">{selected.what_it_is}</Section>
      <Section title="Why is it here?">{selected.why_it_is_here}</Section>
      <Section title="Why it matters to your research">
        <div className="flex flex-col gap-1.5">
          <p>{selected.research_relevance}</p>
          <p className="text-[13px] text-slate-600">{selected.research_relevance_reason}</p>
        </div>
      </Section>
      <Section title="What this does not establish">{selected.limitations}</Section>

      {selected.type === "equation" && selected.equation && (
        <Section title="Equation detail">
          <div className="flex flex-col gap-3">
            {selected.equation.latex ? (
              <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white p-2">
                <Latex tex={selected.equation.latex} />
              </div>
            ) : (
              <p className="text-slate-500">LaTeX not recoverable from the PDF.</p>
            )}
            {selected.equation.symbols.length > 0 && (
              <Field label="Symbols">
                <dl className="flex flex-col gap-1.5">
                  {selected.equation.symbols.map((s) => (
                    <div key={s.symbol} className="flex gap-2">
                      <dt className="shrink-0 font-mono text-[13px] font-semibold text-slate-900">
                        {s.symbol}
                      </dt>
                      <dd className="text-[13px] text-slate-700">{s.meaning}</dd>
                    </div>
                  ))}
                </dl>
              </Field>
            )}
            {selected.equation.role_in_method && (
              <Field label="Role in method">{selected.equation.role_in_method}</Field>
            )}
          </div>
        </Section>
      )}

      {selected.type === "figure" && selected.figure && (
        <Section title="Figure detail">
          <div className="flex flex-col gap-3">
            {selected.figure.components.length > 0 && (
              <Field label="Components">
                <ul className="list-disc pl-5">
                  {selected.figure.components.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </Field>
            )}
            {selected.figure.flow.length > 0 && (
              <Field label="Information flow">
                <ol className="list-decimal pl-5">
                  {selected.figure.flow.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              </Field>
            )}
            {selected.figure.role_in_method && (
              <Field label="Role in method">{selected.figure.role_in_method}</Field>
            )}
          </div>
        </Section>
      )}

      {selected.type === "table" && selected.table && (
        <Section title="Table detail">
          <div className="flex flex-col gap-3">
            {selected.table.metric && <Field label="Metric">{selected.table.metric}</Field>}
            {selected.table.higher_or_lower_is_better && (
              <Field label="Better direction">
                <span className="rounded bg-slate-900 px-1.5 py-0.5 text-xs font-medium text-white">
                  {selected.table.higher_or_lower_is_better === "higher"
                    ? "Higher is better ↑"
                    : "Lower is better ↓"}
                </span>
              </Field>
            )}
            {selected.table.important_comparisons.length > 0 && (
              <Field label="Important comparisons">
                <ul className="list-disc pl-5">
                  {selected.table.important_comparisons.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </Field>
            )}
            {selected.table.result_interpretation && (
              <Field label="Result interpretation">
                {selected.table.result_interpretation}
              </Field>
            )}
          </div>
        </Section>
      )}

      <Section title="Evidence">
        <span className="font-mono text-[13px]">{selected.evidence}</span>
      </Section>

      {paperId && selected.type === "claim" && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Examine this claim
          </h3>
          <div className="mt-2 flex flex-col items-start gap-3">
            <div className="w-full">
              <AuditClaim paperId={paperId} claim={selected} projectId={projectId} />
              <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                Evidence audit: recomputes the numbers and assesses methodology
                from the paper. No code runs.
              </p>
            </div>
            <div className="w-full border-t border-slate-100 pt-2">
              <VerifyClaim paperId={paperId} claim={selected} projectId={projectId} />
              <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
                Computational test: runs a small benchmark in an isolated
                sandbox. Only meaningful for feasible performance or scaling
                claims — a passing test supports the tested aspect, never the
                paper&apos;s full conclusion.
              </p>
            </div>
          </div>
        </section>
      )}

      {related.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            Related objects
          </h3>
          <div className="mt-2 flex flex-wrap gap-2">
            {related.map((o) => (
              <button
                key={o.id}
                type="button"
                onClick={() => onSelectRelated(o.id)}
                className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:border-indigo-600 hover:text-indigo-800"
              >
                {o.label ?? o.id}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
