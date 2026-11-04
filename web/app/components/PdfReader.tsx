"use client";

import { useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";
import "react-pdf/dist/Page/AnnotationLayer.css";
import OverlayLayer, { type OverlayBox } from "./OverlayLayer";
import type { LensMode } from "../lib/relevance";

pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";

// Render width in device pixels; CSS scales the canvas to its slot while the
// percent-positioned overlays stay aligned.
const RENDER_WIDTH = 900;

interface Props {
  pdfUrl: string;
  overlaysByPage: Map<number, OverlayBox[]>;
  selectedId: string | null;
  mode: LensMode;
  onSelect: (objectId: string) => void;
  registerPageRef: (pageNumber: number, el: HTMLDivElement | null) => void;
}

export default function PdfReader({
  pdfUrl,
  overlaysByPage,
  selectedId,
  mode,
  onSelect,
  registerPageRef,
}: Props) {
  const [numPages, setNumPages] = useState(0);
  const [failed, setFailed] = useState(false);

  if (failed) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-600">
        Could not load the PDF. Check that web/public/paper.pdf exists.
      </div>
    );
  }

  return (
    <Document
      file={pdfUrl}
      onLoadSuccess={(doc) => setNumPages(doc.numPages)}
      onLoadError={() => setFailed(true)}
      loading={
        <div className="flex h-full items-center justify-center text-sm text-slate-500">
          Loading paper…
        </div>
      }
      className="flex flex-col items-center gap-6 py-6"
    >
      {Array.from({ length: numPages }, (_, i) => i + 1).map((pageNumber) => (
        <div
          key={pageNumber}
          ref={(el) => registerPageRef(pageNumber, el)}
          data-page={pageNumber}
          className="relative w-full max-w-[860px] scroll-mt-4 bg-white shadow-[0_1px_3px_rgba(0,0,0,0.18)]"
        >
          <Page
            pageNumber={pageNumber}
            width={RENDER_WIDTH}
            renderAnnotationLayer={false}
          />
          <OverlayLayer
            boxes={overlaysByPage.get(pageNumber) ?? []}
            selectedId={selectedId}
            mode={mode}
            onSelect={onSelect}
          />
          <div className="pointer-events-none absolute bottom-2 right-3 text-[11px] text-slate-400">
            {pageNumber}
          </div>
        </div>
      ))}
    </Document>
  );
}
