"""Deterministic discovery of labeled scientific objects in a PDF.

Finds figure/table captions and displayed-equation numbers from PDF text
layout, producing a completeness checklist for the inventory stage. The model
still finalizes the inventory (titles, pages, explanations) — this only tells
it which labels must not be missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

CAPTION_RE = re.compile(r"\s*(Figure|Table)\s+(\d+)\s*[\.]")
DISPLAY_EQ_RE = re.compile(r"^,?\s*\(\s*(\d{1,2})\s*\)\s*$")


@dataclass(frozen=True)
class LabelCandidate:
    type: str  # "equation" | "figure" | "table"
    number: str
    page: int  # 1-based page of the defining occurrence

    @property
    def label(self) -> str:
        return f"{self.type.capitalize()} {self.number}"


def discover_labels(pdf_path: Path) -> list[LabelCandidate]:
    """Caption-anchored figures/tables + display-anchored equations."""
    found: dict[tuple[str, str], LabelCandidate] = {}
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            page_number = i + 1
            for b in page.get_text("blocks"):
                text = b[4].replace("\n", " ")
                m = CAPTION_RE.match(text)
                if m:
                    kind = "figure" if m.group(1) == "Figure" else "table"
                    key = (kind, m.group(2))
                    found.setdefault(key, LabelCandidate(kind, m.group(2), page_number))
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"])
                    m = DISPLAY_EQ_RE.match(text)
                    if m:
                        key = ("equation", m.group(1))
                        found.setdefault(
                            key, LabelCandidate("equation", m.group(1), page_number)
                        )
    order = {"equation": 0, "figure": 1, "table": 2}
    return sorted(
        found.values(), key=lambda c: (order[c.type], int(c.number))
    )


def format_checklist(candidates: list[LabelCandidate]) -> str:
    lines = [f"- {c.label} (defining occurrence on page {c.page})" for c in candidates]
    return "\n".join(lines)
