"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import ResearchLens from "./ResearchLens";
import ObjectNavigator from "./ObjectNavigator";
import type { LocalizedData } from "../lib/types";
import type { LensMode, TypeFilter } from "../lib/relevance";
import { applyTypeFilter, navigatorOrder } from "../lib/relevance";
import type { OverlayBox } from "./OverlayLayer";

// pdf.js needs window/document: never render the reader on the server.
const PdfReader = dynamic(() => import("./PdfReader"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-sm text-slate-500">
      Loading paper…
    </div>
  ),
});

interface Props {
  pdfUrl: string;
  dataUrl: string;
  initialSelectedId?: string | null;
  paperId?: string | null;
  /** Milestone 7 live mode: analysis passed directly (no fixture fetch). */
  liveData?: LocalizedData | null;
  projectId?: string | null;
  projectHref?: string;
}

export default function Reader({ pdfUrl, dataUrl, initialSelectedId = null, paperId = null, liveData = null, projectId = null, projectHref = "/project" }: Props) {
  const [data, setData] = useState<LocalizedData | null>(liveData);
  const [loadError, setLoadError] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId);
  const [mode, setMode] = useState<LensMode>("focus");
  const [filter, setFilter] = useState<TypeFilter>("all");
  const pageRefs = useRef(new Map<number, HTMLDivElement | null>());

  useEffect(() => {
    if (liveData) {
      setData(liveData);
      return;
    }
    fetch(dataUrl)
      .then((r) => {
        if (!r.ok) throw new Error("fixture missing");
        return r.json();
      })
      .then(setData)
      .catch(() => setLoadError(true));
  }, [dataUrl, liveData]);

  const registerPageRef = useCallback((pageNumber: number, el: HTMLDivElement | null) => {
    pageRefs.current.set(pageNumber, el);
  }, []);

  const objectsById = useMemo(() => {
    const m = new Map<string, LocalizedData["analysis"]["objects"][number]>();
    data?.analysis.objects.forEach((o) => m.set(o.id, o));
    return m;
  }, [data]);

  const overlaysByPage = useMemo(() => {
    const m = new Map<number, OverlayBox[]>();
    if (!data) return m;
    for (const loc of data.localizations) {
      if (!loc.bbox) continue;
      const obj = objectsById.get(loc.object_id);
      if (!obj) continue;
      const list = m.get(loc.page) ?? [];
      list.push({
        objectId: loc.object_id,
        label: obj.label ?? obj.id,
        level: obj.research_relevance_level,
        bbox: loc.bbox,
      });
      m.set(loc.page, list);
    }
    return m;
  }, [data, objectsById]);

  const navigable = useMemo(() => {
    if (!data) return [];
    const withBox = new Set(
      data.localizations.filter((l) => l.bbox).map((l) => l.object_id)
    );
    // Claims have no PDF bounding box but are first-class inventory items
    // (selectable, page-scrolled, verifiable).
    return navigatorOrder(
      data.analysis.objects.filter((o) => withBox.has(o.id) || o.type === "claim")
    );
  }, [data]);

  const filtered = useMemo(() => applyTypeFilter(navigable, filter), [navigable, filter]);

  const highCount = useMemo(
    () => navigable.filter((o) => o.research_relevance_level === "high").length,
    [navigable]
  );

  const jumpTo = useCallback(
    (objectId: string) => {
      setSelectedId(objectId);
      const obj = objectsById.get(objectId);
      if (obj?.page != null) {
        pageRefs.current
          .get(obj.page)
          ?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    },
    [objectsById]
  );

  // Deep links from the Project view (?select=) jump once data loads.
  useEffect(() => {
    if (initialSelectedId && objectsById.has(initialSelectedId)) {
      jumpTo(initialSelectedId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (loadError) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-slate-600">
        Could not load analysis data ({dataUrl}). Re-run stage-web or re-copy
        the prototype fixtures.
      </div>
    );
  }
  if (!data) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-slate-500">
        Loading ResearchOS…
      </div>
    );
  }

  const selected = selectedId ? objectsById.get(selectedId) ?? null : null;

  return (
    <div className="flex h-screen flex-col overflow-x-auto">
      <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-slate-200 bg-white px-4 py-2.5 sm:px-6">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <Link
            href={projectHref}
            className="shrink-0 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-semibold text-slate-700 hover:border-slate-400"
          >
            ← Project
          </Link>
          <div className="min-w-0 max-w-full truncate font-serif text-[15px] text-slate-900 sm:max-w-[46vw]">
            {data.analysis.paper_title}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <Link
            href="/"
            className="hidden text-sm font-bold tracking-tight text-slate-900 hover:text-slate-600 md:inline"
          >
            ResearchOS
          </Link>
          <div
            className="flex items-center rounded-lg border border-slate-300 bg-slate-50 p-0.5"
            role="tablist"
            aria-label="Lens mode"
          >
            {(["focus", "all"] as const).map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={mode === m}
                onClick={() => setMode(m)}
                className={[
                  "rounded-md px-3 py-1 text-xs font-semibold transition-colors",
                  mode === m
                    ? "bg-indigo-600 text-white"
                    : "text-slate-600 hover:text-slate-900",
                ].join(" ")}
              >
                {m === "focus" ? "Focus" : "Show all"}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="border-b border-slate-200 bg-white px-4 py-3 sm:px-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              Research question
            </div>
            <p className="mt-0.5 line-clamp-2 font-serif text-base leading-snug text-slate-800">
              {data.analysis.research_question}
            </p>
          </div>
        </div>
        {mode === "focus" && (
          <p className="mt-2 text-[13px] text-slate-600">
            ResearchOS found {navigable.length} scientific objects.{" "}
            {highCount} are highly relevant to your research question.
          </p>
        )}
        <div className="mt-2.5">
          <ObjectNavigator
            objects={filtered}
            selectedId={selectedId}
            filter={filter}
            onFilter={setFilter}
            onJump={jumpTo}
          />
        </div>
      </div>

      <main className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <div className="min-h-[50vh] w-full overflow-y-auto bg-slate-200 px-3 sm:px-6 lg:h-auto lg:w-[65%] lg:min-w-[560px]">
          <PdfReader
            pdfUrl={pdfUrl}
            overlaysByPage={overlaysByPage}
            selectedId={selectedId}
            mode={mode}
            onSelect={setSelectedId}
            registerPageRef={registerPageRef}
          />
        </div>
        <aside className="min-h-0 w-full flex-1 overflow-y-auto border-t border-slate-300 bg-slate-50 lg:w-[35%] lg:min-w-[320px] lg:flex-none lg:border-l lg:border-t-0">
          <ResearchLens
            data={data}
            selected={selected}
            localizedCount={navigable.length}
            highCount={highCount}
            onSelectRelated={jumpTo}
            paperId={paperId}
            projectId={projectId}
          />
        </aside>
      </main>
    </div>
  );
}
