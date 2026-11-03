"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import type { Connection, ConnectionType, ProjectData } from "../lib/project";
import {
  CORPUS_SCOPE_PREFIX,
  endpointLabel,
  findingReaderLink,
  findingsForPaper,
  groupConnectionsByType,
  paperById,
} from "../lib/project";

const TYPE_BADGE: Record<ConnectionType, string> = {
  supports: "bg-green-100 text-green-900",
  contradicts: "bg-red-100 text-red-900",
  extends: "bg-indigo-100 text-indigo-900",
  overlaps_with: "bg-neutral-200 text-neutral-800",
  uses_same_method: "bg-neutral-200 text-neutral-800",
  uses_different_assumption: "bg-amber-100 text-amber-900",
  leaves_open: "bg-amber-100 text-amber-900",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-neutral-500">
        {title}
      </h2>
      <div className="mt-3 flex flex-col gap-3">{children}</div>
    </section>
  );
}

function RefChip({ label, href }: { label: string; href?: string }) {
  const cls =
    "rounded-full border border-neutral-300 bg-white px-2 py-0.5 font-mono text-[11px] text-neutral-700";
  return href ? (
    <Link href={href} className={`${cls} hover:border-indigo-600 hover:text-indigo-800`}>
      {label}
    </Link>
  ) : (
    <span className={cls}>{label}</span>
  );
}

export default function ProjectPage() {
  const [project, setProject] = useState<ProjectData | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    fetch("/data/project.json")
      .then((r) => {
        if (!r.ok) throw new Error("project missing");
        return r.json();
      })
      .then(setProject)
      .catch(() => setMissing(true));
  }, []);

  const grouped = useMemo(
    () => (project ? groupConnectionsByType(project.connections) : new Map<ConnectionType, Connection[]>()),
    [project]
  );

  if (missing) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-neutral-600">
        No project staged yet. Run project.py init/add-paper/stage-web first.
      </div>
    );
  }
  if (!project) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-neutral-500">
        Loading project…
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-neutral-50">
      <header className="flex items-center justify-between border-b border-neutral-300 bg-white px-5 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-bold tracking-tight text-neutral-900">ResearchOS</h1>
          <span className="text-xs text-neutral-500">research project</span>
        </div>
        <Link
          href="/"
          className="rounded-full border border-neutral-300 px-2.5 py-0.5 text-xs font-medium text-neutral-600 hover:border-neutral-500 hover:text-neutral-900"
        >
          Reader
        </Link>
      </header>

      <main className="mx-auto max-w-3xl px-5 pb-16">
        <div className="mt-6 rounded-md border border-neutral-200 bg-white p-4">
          <div className="text-xs font-medium text-neutral-500">Research question</div>
          <p className="mt-1 font-serif text-xl leading-snug text-neutral-900">
            {project.research_question}
          </p>
          <p className="mt-2 text-[13px] text-neutral-600">
            {project.papers.length} papers · {project.findings.length} findings ·{" "}
            {project.connections.length} connections · {project.open_questions.length}{" "}
            open questions · {project.novelty_risks.length} novelty risks
          </p>
        </div>

        <Section title={`Papers (${project.papers.length})`}>
          {project.papers.map((p) => (
            <div key={p.paper_id} className="rounded-md border border-neutral-200 bg-white p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                    Paper {p.added_order}
                  </div>
                  <h3 className="mt-0.5 font-serif text-base leading-snug text-neutral-900">
                    {p.title}
                  </h3>
                  <p className="mt-1 text-[13px] text-neutral-600">
                    {p.n_objects} objects · {p.n_localized} localized ·{" "}
                    {findingsForPaper(project, p.paper_id).length} findings
                  </p>
                </div>
                <Link
                  href={`/read/${p.paper_id}`}
                  className="shrink-0 rounded-full bg-neutral-900 px-3 py-1 text-xs font-semibold text-white hover:bg-neutral-700"
                >
                  Open in reader
                </Link>
              </div>
            </div>
          ))}
        </Section>

        <Section title={`Key findings (${project.findings.length})`}>
          {project.findings.map((f) => {
            const paper = paperById(project, f.paper_id);
            return (
              <div key={f.id} className="rounded-md border border-neutral-200 bg-white p-4">
                <p className="text-sm leading-relaxed text-neutral-900">{f.statement}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <RefChip label={f.id} href={findingReaderLink(project, f)} />
                  <span className="text-xs text-neutral-500">
                    {paper?.title.split(":")[0]} · {f.evidence}
                  </span>
                </div>
              </div>
            );
          })}
        </Section>

        <Section title={`Cross-paper connections (${project.connections.length})`}>
          {project.connections.length === 0 && (
            <p className="text-sm text-neutral-600">
              No supported connections yet — add another paper to grow cross-paper memory.
            </p>
          )}
          {[...grouped.entries()].map(([type, conns]) => (
            <div key={type}>
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
                {type.replace(/_/g, " ")} ({conns.length})
              </div>
              <div className="flex flex-col gap-2">
                {conns.map((c) => (
                  <div key={c.id} className="rounded-md border border-neutral-200 bg-white p-3">
                    <div className="flex flex-wrap items-center gap-2 text-[13px]">
                      <span className="font-mono text-xs text-neutral-700">
                        {endpointLabel(project, c.from_ref)}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${TYPE_BADGE[c.type]}`}
                      >
                        {c.type.replace(/_/g, " ")}
                      </span>
                      <span className="font-mono text-xs text-neutral-700">
                        {endpointLabel(project, c.to_ref)}
                      </span>
                    </div>
                    <p className="mt-1.5 text-[13px] leading-relaxed text-neutral-700">{c.reason}</p>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </Section>

        <Section title={`Open questions (${project.open_questions.length})`}>
          {project.open_questions.map((q) => (
            <div key={q.id} className="rounded-md border border-neutral-200 bg-white p-4">
              <p className="text-sm leading-relaxed text-neutral-900">{q.text}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <span className="text-xs text-neutral-500">
                  Raised by {paperById(project, q.raised_by)?.title.split(":")[0] ?? q.raised_by}
                </span>
                {q.finding_refs.map((r) => {
                  const f = project.findings.find((x) => x.id === r);
                  return (
                    <RefChip key={r} label={r} href={f ? findingReaderLink(project, f) : undefined} />
                  );
                })}
              </div>
            </div>
          ))}
        </Section>

        <Section title={`Novelty risks (${project.novelty_risks.length})`}>
          {project.novelty_risks.map((r) => (
            <div key={r.id} className="rounded-md border border-amber-200 bg-amber-50 p-4">
              <p className="text-sm leading-relaxed text-neutral-900">
                <span className="font-medium">{CORPUS_SCOPE_PREFIX}</span> {r.direction}
              </p>
              <p className="mt-1.5 text-[13px] leading-relaxed text-neutral-700">{r.note}</p>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {r.overlapping_finding_refs.map((ref) => {
                  const f = project.findings.find((x) => x.id === ref);
                  return (
                    <RefChip key={ref} label={ref} href={f ? findingReaderLink(project, f) : undefined} />
                  );
                })}
              </div>
            </div>
          ))}
        </Section>
      </main>
    </div>
  );
}
