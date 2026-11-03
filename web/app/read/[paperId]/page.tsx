"use client";

import { Suspense, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Reader from "../../components/Reader";
import type { PaperEntry, ProjectData } from "../../lib/project";

function PaperReader() {
  const params = useParams();
  const search = useSearchParams();
  const paperId = params.paperId as string;
  const [paper, setPaper] = useState<PaperEntry | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    fetch("/data/project.json")
      .then((r) => {
        if (!r.ok) throw new Error("project missing");
        return r.json() as Promise<ProjectData>;
      })
      .then((p) => {
        const found = p.papers.find((x) => x.paper_id === paperId);
        if (!found || !found.web_pdf || !found.web_data) setMissing(true);
        else setPaper(found);
      })
      .catch(() => setMissing(true));
  }, [paperId]);

  if (missing) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-neutral-600">
        Unknown paper “{paperId}”. Re-run stage-web or pick a paper from the{" "}
        <a href="/project" className="ml-1 underline">project view</a>.
      </div>
    );
  }
  if (!paper) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-neutral-500">
        Loading ResearchOS…
      </div>
    );
  }
  return (
    <Reader
      pdfUrl={paper.web_pdf as string}
      dataUrl={paper.web_data as string}
      initialSelectedId={search.get("select")}
      paperId={paperId}
    />
  );
}

export default function ReadPaperPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center text-sm text-neutral-500">
          Loading ResearchOS…
        </div>
      }
    >
      <PaperReader />
    </Suspense>
  );
}
