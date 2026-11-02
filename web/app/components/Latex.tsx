"use client";

import { useMemo } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

export default function Latex({ tex }: { tex: string }) {
  const html = useMemo(
    () => katex.renderToString(tex, { displayMode: true, throwOnError: false }),
    [tex]
  );
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
