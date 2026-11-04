"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import Reader from "../../../../components/Reader";
import type { LocalizedData } from "../../../../lib/types";
import {
  paperDataUrl,
  paperFileUrl,
  stageLabel,
  type PaperStatus,
} from "../../../../lib/workspace";

function LivePaperReader() {
  const params = useParams();
  const search = useSearchParams();
  const projectId = params.id as string;
  const paperId = params.paperId as string;
  const [data, setData] = useState<LocalizedData | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "missing" | "down">("loading");
  const [paperStatus, setPaperStatus] = useState<PaperStatus | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(paperDataUrl(projectId, paperId));
      if (r.status === 404) {
        setState("missing");
        return;
      }
      if (r.status === 409) {
        const body = (await r.json().catch(() => ({}))) as {
          details?: { status?: PaperStatus };
        };
        setPaperStatus(body.details?.status ?? null);
        setState("loading");
        return;
      }
      if (!r.ok) throw new Error("fetch failed");
      setData((await r.json()) as LocalizedData);
      setState("ready");
    } catch {
      setState("down");
    }
  }, [projectId, paperId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (state !== "loading" || data) return;
    const id = setTimeout(load, 3000);
    return () => clearTimeout(id);
  }, [state, data, load]);

  if (state === "missing") {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 text-sm text-neutral-600">
        <p>Unknown paper “{paperId}” in this project.</p>
        <Link href={`/projects/${projectId}`} className="underline">
          Back to project
        </Link>
      </div>
    );
  }
  if (state === "down") {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 text-sm text-neutral-600">
        <p>Workspace backend not running — start it with ./dev.sh</p>
        <div className="flex gap-3">
          <button type="button" onClick={load} className="underline">
            Retry
          </button>
          <Link href={`/projects/${projectId}`} className="underline">
            Back to project
          </Link>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-2 text-sm text-neutral-500">
        <p>
          {paperStatus
            ? `${stageLabel(paperStatus.stage ?? "")}${paperStatus.stage_detail ? ` — ${paperStatus.stage_detail}` : ""}`
            : "Loading paper…"}
        </p>
        {paperStatus?.status === "failed" && (
          <p className="text-red-700">{paperStatus.last_error}</p>
        )}
        <Link href={`/projects/${projectId}`} className="underline">
          Back to project
        </Link>
      </div>
    );
  }
  return (
    <Reader
      pdfUrl={paperFileUrl(projectId, paperId)}
      dataUrl={paperDataUrl(projectId, paperId)}
      liveData={data}
      initialSelectedId={search.get("select")}
      paperId={paperId}
      projectId={projectId}
      projectHref={`/projects/${projectId}`}
    />
  );
}

export default function LiveReadPaperPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center text-sm text-neutral-500">
          Loading ResearchOS…
        </div>
      }
    >
      <LivePaperReader />
    </Suspense>
  );
}
