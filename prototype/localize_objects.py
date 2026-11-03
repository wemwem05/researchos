#!/usr/bin/env python3
"""ResearchOS Milestone 2 prototype: visual localization of scientific objects.

Takes the Milestone 1 semantic analysis (prototype/output.json), renders ONLY
the referenced PDF pages to high-resolution images, and locates each known
equation/figure/table: figures and equations via Muse vision on the page
image, tables via a hybrid path (PyMuPDF table finder + caption anchor first,
Muse vision on a caption-anchored crop on ambiguity).
Returns normalized 0..1000 bounding boxes for later clickable PDF overlays.

Usage:
    python prototype/localize_objects.py \
        --pdf samples/eot_shape_correspondence.pdf \
        --analysis prototype/output.json \
        --output prototype/localized_output.json \
        [--debug-dir prototype/debug]

Auth: API key ONLY via the MODEL_API_KEY environment variable (or a local,
never-committed .env file containing it).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, model_validator
import pymupdf

from analyze_paper import (
    API_BASE_URL,
    API_KEY_ENV_VAR,
    DEFAULT_MODEL,
    AnalysisResult,
    SciObject,
    extract_json,
)

# Render scale: 3x the PDF's 72 dpi base = 216 dpi. High enough to read
# equations/tables, small enough to send as a single image.
RENDER_MATRIX = pymupdf.Matrix(3, 3)
CROP_MATRIX = pymupdf.Matrix(4, 4)
LOCALIZABLE_TYPES = ("equation", "figure", "table")
MAX_NULL_CONFIDENCE = 0.3
NATIVE_TABLE_CONFIDENCE = 0.9


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BoundingBox(BaseModel):
    """Normalized 0..1000 box; (0,0) = top-left of the page image."""

    x_min: int = Field(ge=0, le=1000)
    y_min: int = Field(ge=0, le=1000)
    x_max: int = Field(ge=0, le=1000)
    y_max: int = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def check_order(self) -> "BoundingBox":
        if not self.x_min < self.x_max:
            raise ValueError(f"x_min ({self.x_min}) must be < x_max ({self.x_max})")
        if not self.y_min < self.y_max:
            raise ValueError(f"y_min ({self.y_min}) must be < y_max ({self.y_max})")
        return self


class ObjectLocalization(BaseModel):
    object_id: str
    page: int = Field(ge=1)
    bbox: Optional[BoundingBox] = None
    confidence: float = Field(ge=0.0, le=1.0)
    localization_method: Optional[Literal["pdf_native", "muse_vision", "hybrid"]] = None
    localization_note: Optional[str] = None

    @model_validator(mode="after")
    def check_confidence_semantics(self) -> "ObjectLocalization":
        if self.bbox is None and self.confidence > MAX_NULL_CONFIDENCE:
            raise ValueError(
                f"null bbox with confidence {self.confidence}: must be "
                f"<= {MAX_NULL_CONFIDENCE}"
            )
        return self


class PageObjectBox(BaseModel):
    """Raw per-object model output (page is attached by us).

    No confidence rule here: a single non-compliant item must not fail the
    whole page. Null-box confidence is coerced to <= MAX_NULL_CONFIDENCE
    at merge time (see coerce_confidence).
    """

    object_id: str
    bbox: Optional[BoundingBox] = None
    confidence: float = Field(ge=0.0, le=1.0)


def coerce_confidence(
    bbox: Optional[BoundingBox], confidence: float, context: str
) -> float:
    if bbox is None and confidence > MAX_NULL_CONFIDENCE:
        print(
            f"[warning] {context}: null bbox with confidence {confidence}; "
            f"clamping to {MAX_NULL_CONFIDENCE}",
            file=sys.stderr,
        )
        return MAX_NULL_CONFIDENCE
    return confidence


class PageLocalizationResponse(BaseModel):
    localizations: list[PageObjectBox] = Field(default_factory=list)


class LocalizedOutput(BaseModel):
    analysis: AnalysisResult
    localizations: list[ObjectLocalization] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Page selection, rendering, prompting
# ---------------------------------------------------------------------------


def select_localizable(objects: list[SciObject]) -> list[SciObject]:
    """Equations/figures/tables that have both a page number and a label."""
    return [
        o
        for o in objects
        if o.type in LOCALIZABLE_TYPES and o.page is not None and o.label is not None
    ]


def render_page(pdf_path: Path, page_number: int) -> bytes:
    """Render one 1-based page to PNG bytes. Only called for needed pages."""
    with pymupdf.open(pdf_path) as doc:
        if page_number < 1 or page_number > len(doc):
            raise ValueError(f"page {page_number} out of range (PDF has {len(doc)})")
        pix = doc[page_number - 1].get_pixmap(matrix=RENDER_MATRIX)
        return pix.tobytes("png")


def build_page_prompt(page_number: int, objects: list[SciObject]) -> str:
    listing = "\n".join(
        f'- id "{o.id}" | {o.label}: "{o.title}"' for o in objects
    )
    return (
        f"You are given a rendered image of page {page_number} of a scientific paper.\n"
        "Below is the list of KNOWN scientific objects on THIS page, identified by "
        "an earlier analysis step. Locate ONLY these objects. Do NOT rediscover "
        "the document, do NOT add objects, do NOT use other pages.\n\n"
        f"Objects on page {page_number}:\n{listing}\n\n"
        "For EACH listed object, return its visual region as a bounding box in "
        "normalized 0..1000 coordinates: (0,0) is the top-left of the image, "
        "(1000,1000) the bottom-right. Require x_min < x_max and y_min < y_max.\n"
        "- equation: box tightly around the displayed math (not the full paragraph).\n"
        "- figure: box around the whole figure block, including its caption.\n"
        "- table: box around the whole table, including its caption/title.\n"
        "If you cannot confidently locate an object, return bbox null with "
        "confidence at most 0.3. High confidence is only for boxes you actually "
        "return. NEVER invent coordinates."
    )


def chat_json_schema_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "page_localization",
            "schema": PageLocalizationResponse.model_json_schema(),
        },
    }


def chat_localize(
    client: OpenAI, model: str, png_bytes: bytes, prompt: str, context: str
) -> PageLocalizationResponse:
    """Send one image + prompt; structured output with plain-JSON fallback."""
    image_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url, "detail": "high"},
                },
            ],
        }
    ]
    errors = []
    for name, kwargs in [
        ("Chat Completions + json_schema", {"response_format": chat_json_schema_format()}),
        ("Chat Completions plain JSON", {}),
    ]:
        try:
            print(f"[attempt] {context}: {name} ...", file=sys.stderr)
            resp = client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
            content = resp.choices[0].message.content or ""
            return PageLocalizationResponse.model_validate_json(extract_json(content))
        except Exception as e:  # noqa: BLE001 - prototype: fall through to next attempt
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[attempt] {context}: {name} failed: {e}", file=sys.stderr)
    raise RuntimeError(f"{context}: all attempts failed:\n" + "\n".join(errors))


def localize_page(
    client: OpenAI, model: str, page_png: bytes, page_number: int, objects: list[SciObject]
) -> PageLocalizationResponse:
    return chat_localize(
        client,
        model,
        page_png,
        build_page_prompt(page_number, objects),
        f"page {page_number}",
    )


def build_localizations(
    page_number: int, known: list[SciObject], resp: PageLocalizationResponse
) -> list[ObjectLocalization]:
    """Merge one full-page vision response with known objects.

    Page is attached from the analysis (guaranteed match). Unknown ids from
    the model are dropped with a warning; known objects missing from the
    response get an explicit bbox=null entry with confidence 0. Null-box
    confidence is coerced to <= MAX_NULL_CONFIDENCE.
    """
    known_ids = {o.id for o in known}
    by_id = {b.object_id: b for b in resp.localizations}
    for object_id in by_id:
        if object_id not in known_ids:
            print(
                f"[warning] page {page_number}: unknown object_id '{object_id}' "
                "not in analysis; ignoring",
                file=sys.stderr,
            )
    out = []
    for o in known:
        box = by_id.get(o.id)
        if box is None:
            print(
                f"[warning] page {page_number}: no box returned for '{o.id}'; "
                "recording bbox=null",
                file=sys.stderr,
            )
            out.append(
                ObjectLocalization(
                    object_id=o.id,
                    page=page_number,
                    bbox=None,
                    confidence=0.0,
                    localization_method="muse_vision",
                    localization_note="No box returned by the model for this object.",
                )
            )
        else:
            out.append(
                ObjectLocalization(
                    object_id=o.id,
                    page=page_number,
                    bbox=box.bbox,
                    confidence=coerce_confidence(
                        box.bbox, box.confidence, f"page {page_number} '{o.id}'"
                    ),
                    localization_method="muse_vision",
                    localization_note="Located by Muse vision on the full page render.",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Hybrid table localization: PyMuPDF structure first, vision on crops second.
# ---------------------------------------------------------------------------


def normalize_rect(rect: pymupdf.Rect, page_rect: pymupdf.Rect) -> BoundingBox:
    """PDF-point rect -> normalized 0..1000 box."""

    def scale(v: float, size: float) -> int:
        return max(0, min(1000, int(round(v / size * 1000))))

    return BoundingBox(
        x_min=scale(rect.x0, page_rect.width),
        y_min=scale(rect.y0, page_rect.height),
        x_max=scale(rect.x1, page_rect.width),
        y_max=scale(rect.y1, page_rect.height),
    )


def native_table_candidates(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Table grid rects from PyMuPDF's finder; [] when none/unsupported."""
    try:
        return [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
    except Exception as e:  # noqa: BLE001 - prototype: native detection is best-effort
        print(f"[warning] find_tables failed: {e}; skipping native path", file=sys.stderr)
        return []


def caption_hits(page: pymupdf.Page, label: str) -> list[pymupdf.Rect]:
    """Text rects where the table label (e.g. 'Table 1') is printed."""
    hits = []
    for hit in page.search_for(label):
        probe = page.get_textbox(hit + (0, 0, 24, 0)).strip()
        rest = probe[len(label):] if probe.startswith(label) else ""
        if rest[:1].isdigit():  # 'Table 1' inside 'Table 10' etc.
            continue
        hits.append(hit)
    return hits


def caption_block_rect(page: pymupdf.Page, hit: pymupdf.Rect) -> pymupdf.Rect:
    """Full caption paragraph block containing a label hit (captions are
    usually multi-line; the label hit alone underestimates their extent)."""
    for b in page.get_text("blocks"):
        if pymupdf.Rect(b[:4]).contains(pymupdf.Point(hit.x0 + 1, hit.y0 + 1)):
            return pymupdf.Rect(b[:4])
    return hit


_STOPWORDS = frozenset(
    "a an the of on for and or to in with via from by as is are results result study".split()
)


def caption_text(page: pymupdf.Page, hit: pymupdf.Rect) -> str:
    """Full caption line(s) starting at a label hit."""
    area = pymupdf.Rect(0, hit.y0 - 2, page.rect.width, hit.y1 + 24)
    return page.get_textbox(area).strip().replace("\n", " ")


def title_overlap(title: str, caption: str) -> float:
    """Fraction of significant title words appearing in the caption text."""

    def words(s: str) -> set[str]:
        return {
            w
            for w in re.split(r"\W+", s.lower())
            if w and len(w) > 2 and w not in _STOPWORDS
        }

    tw = words(title)
    return len(tw & words(caption)) / len(tw) if tw else 0.0


def choose_native_candidate(
    candidates: list[pymupdf.Rect],
    captions: list[pymupdf.Rect],
    page_rect: pymupdf.Rect,
) -> tuple[pymupdf.Rect, pymupdf.Rect] | None:
    """Unique table grid adjacent to a caption, else None (ambiguous/absent)."""
    adjacent: dict[tuple[float, ...], tuple[float, pymupdf.Rect, pymupdf.Rect]] = {}
    for cand in candidates:
        best: tuple[float, pymupdf.Rect] | None = None
        for cap in captions:
            v_gap = min(abs(cand.y0 - cap.y1), abs(cap.y0 - cand.y1))
            h_dist = max(0.0, max(cand.x0, cap.x0) - min(cand.x1, cap.x1))
            if v_gap < 0.05 * page_rect.height and h_dist < 0.05 * page_rect.width:
                if best is None or v_gap < best[0]:
                    best = (v_gap, cap)
        if best is not None:
            key = (round(cand.x0, 1), round(cand.y0, 1), round(cand.x1, 1), round(cand.y1, 1))
            adjacent.setdefault(key, (best[0], cand, best[1]))
    if len(adjacent) != 1:
        return None
    _, cand, cap = next(iter(adjacent.values()))
    return cand, cap


def crop_around_caption(cap: pymupdf.Rect, page_rect: pymupdf.Rect) -> pymupdf.Rect:
    """Full-width vertical window around a caption (biased below it, where
    tables usually sit)."""
    h = page_rect.height
    y0 = max(0.0, cap.y0 - 0.06 * h)
    y1 = min(h, cap.y1 + 0.45 * h)
    if y1 - y0 < 0.2 * h:  # caption at page bottom: extend upward instead
        y0 = max(0.0, y1 - 0.2 * h)
    return pymupdf.Rect(0, y0, page_rect.width, y1)


def render_crop_png(page: pymupdf.Page, clip: pymupdf.Rect) -> bytes:
    return page.get_pixmap(matrix=CROP_MATRIX, clip=clip).tobytes("png")


def map_crop_box_to_page(
    crop: pymupdf.Rect, page_rect: pymupdf.Rect, box: BoundingBox
) -> BoundingBox:
    """Crop-relative 0..1000 box -> full-page 0..1000 box."""
    pdf = pymupdf.Rect(
        crop.x0 + box.x_min / 1000 * crop.width,
        crop.y0 + box.y_min / 1000 * crop.height,
        crop.x0 + box.x_max / 1000 * crop.width,
        crop.y0 + box.y_max / 1000 * crop.height,
    )
    return normalize_rect(pdf, page_rect)


def build_crop_prompt(page_number: int, table: SciObject) -> str:
    return (
        f"You are given a CROPPED region of page {page_number} of a scientific paper.\n"
        "Locate ONLY this KNOWN table:\n"
        f'- id "{table.id}" | {table.label}: "{table.title}"\n\n'
        "Do NOT rediscover the document, do NOT add objects. Return the table's "
        "visual region as a bounding box in 0..1000 coordinates RELATIVE TO THIS "
        "CROP: (0,0) is the top-left of the crop image, (1000,1000) the "
        "bottom-right. Require x_min < x_max and y_min < y_max. Box the whole "
        "table including its caption/title.\n"
        "If you cannot confidently locate it, return bbox null with confidence "
        "at most 0.3. NEVER invent coordinates."
    )


def localize_table(
    client: OpenAI, model: str, page: pymupdf.Page, page_number: int, table: SciObject
) -> ObjectLocalization | None:
    """Hybrid table localization.

    pdf_native: unique find_tables() grid adjacent to the caption.
    hybrid: ambiguous/failed native match -> vision on a caption-anchored crop.
    None: no caption anchor at all -> caller falls back to full-page vision.
    """
    label = table.label or ""
    captions = [caption_block_rect(page, h) for h in caption_hits(page, label)]
    candidates = native_table_candidates(page)
    match = choose_native_candidate(candidates, captions, page.rect)
    if match is not None:
        cand, cap = match
        overlap = title_overlap(table.title, caption_text(page, cap))
        print(
            f"[info] page {page_number} '{table.id}': native match "
            f"(caption/title overlap {overlap:.0%})",
            file=sys.stderr,
        )
        padded = ((cand | cap) + (-4, -4, 4, 4)) & page.rect
        return ObjectLocalization(
            object_id=table.id,
            page=page_number,
            bbox=normalize_rect(padded, page.rect),
            confidence=NATIVE_TABLE_CONFIDENCE,
            localization_method="pdf_native",
            localization_note=(
                f"Detected by PyMuPDF table finder and matched to {label} caption."
            ),
        )
    if not captions:
        print(
            f"[info] page {page_number} '{table.id}': no caption anchor; "
            "using full-page vision",
            file=sys.stderr,
        )
        return None
    cap = max(captions, key=lambda c: title_overlap(table.title, caption_text(page, c)))
    crop = crop_around_caption(cap, page.rect)
    crop_png = render_crop_png(page, crop)
    try:
        resp = chat_localize(
            client, model, crop_png,
            build_crop_prompt(page_number, table),
            f"crop p{page_number} '{table.id}'",
        )
    except RuntimeError as e:
        print(f"[warning] crop vision failed for '{table.id}': {e}", file=sys.stderr)
        return ObjectLocalization(
            object_id=table.id,
            page=page_number,
            bbox=None,
            confidence=0.0,
            localization_method="hybrid",
            localization_note="Caption-anchored crop vision call failed.",
        )
    box = next((b for b in resp.localizations if b.object_id == table.id), None)
    if box is None or box.bbox is None:
        conf = box.confidence if box else 0.0
        return ObjectLocalization(
            object_id=table.id,
            page=page_number,
            bbox=None,
            confidence=coerce_confidence(None, conf, f"crop p{page_number} '{table.id}'"),
            localization_method="hybrid",
            localization_note="Model could not locate the table in the caption-anchored crop.",
        )
    try:
        mapped = map_crop_box_to_page(crop, page.rect, box.bbox)
    except ValueError:
        return ObjectLocalization(
            object_id=table.id,
            page=page_number,
            bbox=None,
            confidence=0.0,
            localization_method="hybrid",
            localization_note="Crop box mapped to a degenerate page box; discarded.",
        )
    return ObjectLocalization(
        object_id=table.id,
        page=page_number,
        bbox=mapped,
        confidence=box.confidence,
        localization_method="hybrid",
        localization_note="Located by Muse vision in a caption-anchored crop; mapped to page coordinates.",
    )


def draw_debug_image(page_png: bytes, localizations: list[ObjectLocalization]) -> bytes:
    """Draw returned boxes over the page image; returns PNG bytes."""
    import io

    img = Image.open(io.BytesIO(page_png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    colors = {"muse_vision": "red", "pdf_native": "green", "hybrid": "orange"}
    for loc in localizations:
        if loc.bbox is None:
            continue
        color = colors.get(loc.localization_method or "", "red")
        x0 = loc.bbox.x_min / 1000 * w
        y0 = loc.bbox.y_min / 1000 * h
        x1 = loc.bbox.x_max / 1000 * w
        y1 = loc.bbox.y_max / 1000 * h
        draw.rectangle([x0, y0, x1, y1], outline=color, width=max(2, w // 400))
        tag = f"{loc.object_id} [{loc.localization_method or 'unknown'}]"
        tb = draw.textbbox((x0 + 4, y0 + 4), tag)
        draw.rectangle([tb[0] - 2, tb[1] - 2, tb[2] + 2, tb[3] + 2], fill="white")
        draw.text((x0 + 4, y0 + 4), tag, fill=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Localization cache: reuse SUCCESSFUL boxes across runs.
# ---------------------------------------------------------------------------
#
# Key = pdf sha256 + object_id + page: identity depends on the PDF/object/page,
# never on research relevance, so rescoring or frontend reloads never trigger
# re-localization. Only non-null bboxes are stored; failures are retried.
# Default file prototype/loc_cache.json is git-ignored.


def pdf_fingerprint(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()[:16]


def cache_key(pdf_bytes: bytes, obj: SciObject) -> str:
    return f"{pdf_fingerprint(pdf_bytes)}:{obj.id}:p{obj.page}:{obj.label}"


def load_cache(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")


def lookup_cached(
    cache: dict[str, dict], pdf_bytes: bytes, obj: SciObject
) -> ObjectLocalization | None:
    """Return a validated cached hit, or None on miss/invalid entry.

    Stale or malformed entries (unknown id shape, null bbox) are treated as
    misses, never as successful hits.
    """
    entry = cache.get(cache_key(pdf_bytes, obj))
    if not entry:
        return None
    try:
        loc = ObjectLocalization.model_validate(entry)
    except Exception:  # noqa: BLE001 - corrupt entry: treat as miss
        return None
    if loc.bbox is None or loc.object_id != obj.id or loc.page != obj.page:
        return None
    return loc


def store_for_object(
    cache: dict[str, dict], pdf_bytes: bytes, obj: SciObject, loc: ObjectLocalization
) -> None:
    """Preferred store: full key including the object's label."""
    if loc.bbox is None:
        return
    cache[cache_key(pdf_bytes, obj)] = loc.model_dump(mode="json")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ResearchOS Milestone 2: visually localize the equations, "
        "figures, and tables from a Milestone 1 analysis."
    )
    p.add_argument("--pdf", required=True, help="Path to the scientific PDF.")
    p.add_argument("--analysis", required=True, help="Path to output.json.")
    p.add_argument(
        "--output",
        default="prototype/localized_output.json",
        help="Where to write analysis + localizations (default: prototype/localized_output.json).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL}).")
    p.add_argument("--base-url", default=API_BASE_URL, help=f"API base URL (default: {API_BASE_URL}).")
    p.add_argument(
        "--debug-dir",
        default=None,
        help="If set, save rendered page images with boxes drawn here.",
    )
    p.add_argument(
        "--cache",
        default="prototype/loc_cache.json",
        help="Localization cache file for successful boxes "
        "(default: prototype/loc_cache.json).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable cache reads and writes (always re-localize).",
    )
    return p.parse_args(argv)


def main() -> int:
    load_dotenv()
    args = parse_args()

    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        print(
            f"error: {API_KEY_ENV_VAR} is not set.\n"
            f"  export {API_KEY_ENV_VAR}=...  (or put it in a local .env file; "
            "see .env.example)",
            file=sys.stderr,
        )
        return 2

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    analysis_path = Path(args.analysis)
    if not analysis_path.is_file():
        print(f"error: analysis not found: {analysis_path}", file=sys.stderr)
        return 2

    try:
        analysis = AnalysisResult.model_validate_json(analysis_path.read_text())
    except Exception as e:
        print(f"error: invalid analysis file: {e}", file=sys.stderr)
        return 2

    targets = select_localizable(analysis.objects)
    skipped = [o.id for o in analysis.objects if o not in targets]
    if skipped:
        print(f"[info] skipping (no page/label or not localizable): {skipped}", file=sys.stderr)
    if not targets:
        print("error: analysis has no localizable objects", file=sys.stderr)
        return 2

    by_page: dict[int, list[SciObject]] = {}
    for o in targets:
        by_page.setdefault(o.page, []).append(o)  # type: ignore[arg-type]
    print(
        f"[info] localizing {len(targets)} objects on pages {sorted(by_page)} "
        f"with {args.model}",
        file=sys.stderr,
    )

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    client = OpenAI(base_url=args.base_url, api_key=api_key)
    pdf_bytes = pdf_path.read_bytes()
    use_cache = not args.no_cache
    cache: dict[str, dict] = load_cache(Path(args.cache)) if use_cache else {}
    n_cache_hits = 0
    cached_by_id: dict[str, ObjectLocalization] = {}
    if use_cache:
        for o in targets:
            hit = lookup_cached(cache, pdf_bytes, o)
            if hit is not None:
                cached_by_id[o.id] = hit
                n_cache_hits += 1
        if n_cache_hits:
            print(f"[info] cache hits: {n_cache_hits}/{len(targets)}", file=sys.stderr)
    live_targets = [o for o in targets if o.id not in cached_by_id]
    if not live_targets:
        print("[info] all localizations served from cache", file=sys.stderr)
    by_live_page: dict[int, list[SciObject]] = {}
    for o in live_targets:
        by_live_page.setdefault(o.page, []).append(o)  # type: ignore[arg-type]
    all_localizations: list[ObjectLocalization] = []
    try:
        with pymupdf.open(pdf_path) as doc:
            for page_number in sorted(by_live_page):
                if page_number < 1 or page_number > len(doc):
                    raise ValueError(
                        f"page {page_number} out of range (PDF has {len(doc)})"
                    )
                known = by_live_page[page_number]
                page = doc[page_number - 1]
                # Tables go through the hybrid path; figures/equations (and
                # tables with no caption anchor) use full-page vision.
                vision_known = [o for o in known if o.type != "table"]
                table_locs = []
                for t in known:
                    if t.type != "table":
                        continue
                    m = localize_table(client, args.model, page, page_number, t)
                    if m is None:
                        vision_known.append(t)
                    else:
                        table_locs.append(m)
                page_png = page.get_pixmap(matrix=RENDER_MATRIX).tobytes("png")
                vision_locs = (
                    build_localizations(
                        page_number,
                        vision_known,
                        localize_page(
                            client, args.model, page_png, page_number, vision_known
                        ),
                    )
                    if vision_known
                    else []
                )
                order = {o.id: i for i, o in enumerate(known)}
                locs = sorted(
                    vision_locs + table_locs, key=lambda m: order[m.object_id]
                )
                n_found = sum(1 for loc in locs if loc.bbox is not None)
                print(
                    f"[info] page {page_number}: located {n_found}/{len(locs)}",
                    file=sys.stderr,
                )
                if debug_dir:
                    cached_here = [
                        cached_by_id[o.id]
                        for o in by_page[page_number]
                        if o.id in cached_by_id
                    ]
                    debug_png = draw_debug_image(page_png, locs + cached_here)
                    (debug_dir / f"page-{page_number:02d}.png").write_bytes(debug_png)
                if use_cache:
                    for o, m in zip(known, locs):
                        store_for_object(cache, pdf_bytes, o, m)
                all_localizations.extend(locs)
            # Debug overlays for fully cached pages.
            if debug_dir:
                for page_number in sorted(set(by_page) - set(by_live_page)):
                    page = doc[page_number - 1]
                    page_png = page.get_pixmap(matrix=RENDER_MATRIX).tobytes("png")
                    locs = [cached_by_id[o.id] for o in by_page[page_number]]
                    (debug_dir / f"page-{page_number:02d}.png").write_bytes(
                        draw_debug_image(page_png, locs)
                    )
    except Exception as e:  # noqa: BLE001 - prototype: report and exit non-zero
        print(f"error: localization failed: {e}", file=sys.stderr)
        return 1

    if use_cache:
        save_cache(Path(args.cache), cache)
    # Restore original analysis order: cached entries first, then live ones.
    order = {o.id: i for i, o in enumerate(targets)}
    all_localizations = sorted(
        list(cached_by_id.values()) + all_localizations,
        key=lambda m: order[m.object_id],
    )

    out = LocalizedOutput(analysis=analysis, localizations=all_localizations)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out.model_dump_json(indent=2) + "\n", encoding="utf-8")
    n_found = sum(1 for loc in all_localizations if loc.bbox is not None)
    print(
        f"[done] wrote {out_path}: {n_found}/{len(all_localizations)} boxes found",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
