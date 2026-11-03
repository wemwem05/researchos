"use client";

import Reader from "./components/Reader";

export default function Home() {
  return <Reader pdfUrl="/paper.pdf" dataUrl="/data/localized_output.json" />;
}
