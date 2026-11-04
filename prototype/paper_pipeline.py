#!/usr/bin/env python3
"""ResearchOS Milestone 7: real paper processing pipeline.

Connects browser uploads to the working M1-M5 Python pipeline:

    validate_pdf -> extract_document_inventory -> explain_scientific_objects
      -> calculate_question_relevance -> localize_objects
      -> extract_project_findings -> compare_with_existing_project
      -> validate_and_persist -> READY

Reuse (never duplication):
- inventory:    inventory.discover_labels / format_checklist
- stage 1+2:    analyze_paper.analyze_pdf / score_relevance / merge_relevance
- localization: localize_objects helpers + the shared prototype/loc_cache.json
- findings:     agent.cmd_investigate (Spark Open investigator/critic graph)

Cost controls:
- content-hash dedupe (same project -> duplicate; anywhere else -> verified
  document-cache reuse with recorded provenance, then fresh relevance +
  fresh cross-paper interpretation for THIS project/question)
- per-paper stage files: retries resume; completed expensive stages rerun never
- relevance-only rescoring on question edits (no rediscovery/relocalization)

All model clients are injectable via PipelineDeps so tests run deterministically
without API keys. Production uses the Meta Model API for perception and
meta-models/muse-spark-open-1026 (Baseten) for agentic reasoning — unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf
from dotenv import load_dotenv
from openai import OpenAI

from analyze_paper import (
    API_BASE_URL,
    API_KEY_ENV_VAR,
    DEFAULT_MODEL,
    AnalysisDraft,
    AnalysisResult,
    analyze_pdf,
    draft_from_result,
    merge_relevance,
    score_relevance,
)
from inventory import discover_labels, format_checklist
from localize_objects import (
    LocalizedOutput,
    ObjectLocalization,
    build_localizations,
    load_cache,
    localize_page,
    localize_table,
    lookup_cached,
    save_cache,
    select_localizable,
    store_for_object,
)
from project import PaperEntry, load_project, save_project

DEFAULT_MAX_PDF_MB = 32
DEFAULT_MAX_PAGES = 60

# Legacy M1/M2 analyses reusable by content hash (upload bytes must match the
# recorded source file's bytes exactly; provenance is always disclosed).
LEGACY_ANALYSIS_SOURCES = (
    ("prototype/output.json", "prototype/localized_output.json",
     "samples/eot_shape_correspondence.pdf"),
    ("prototype/deep_shells_output.json", "prototype/deep_shells_localized.json",
     "samples/NeurIPS-2020-deep-shells-unsupervised-shape-correspondence-with-optimal-transport-Paper.pdf"),
)


class PipelineError(RuntimeError):
    """User-facing pipeline failure (safe to display).

    `retryable` tells the UI whether retrying could plausibly help. Failures
    caused by the document itself (not a scientific paper, no labeled
    objects) are not retryable — retrying would only burn model calls.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class DuplicatePDFError(PipelineError):
    def __init__(self, paper_id: str):
        super().__init__(f"this PDF was already added as paper '{paper_id}'",
                         retryable=False)
        self.paper_id = paper_id


NOT_A_PAPER_MESSAGE = (
    "This document doesn't appear to be a scientific paper with labeled "
    "equations, figures, or tables: none were found in its text, and the "
    "analysis found none either. ResearchOS builds its Research Lens from "
    "those objects, so this document can't be added as a research paper. "
    "Retrying won't help — remove it and upload a scientific PDF instead."
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def max_pdf_bytes() -> int:
    return int(os.environ.get("RESEARCHOS_MAX_PDF_MB", DEFAULT_MAX_PDF_MB)) * 1024 * 1024


def max_pdf_pages() -> int:
    return int(os.environ.get("RESEARCHOS_MAX_PDF_PAGES", DEFAULT_MAX_PAGES))


def safe_display_name(name: str) -> str:
    base = Path(name).name.strip() or "paper.pdf"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return base[:120] or "paper.pdf"


def validate_pdf(pdf_bytes: bytes, filename: str) -> dict[str, Any]:
    """Validate an upload: size, magic, parseability, page/encryption limits."""
    if len(pdf_bytes) > max_pdf_bytes():
        raise PipelineError(
            f"PDF is {len(pdf_bytes) / 1024 / 1024:.1f} MB; "
            f"limit is {max_pdf_bytes() / 1024 / 1024:.0f} MB"
        )
    if len(pdf_bytes) < 8 or not pdf_bytes.startswith(b"%PDF-"):
        raise PipelineError("not a valid PDF (missing %PDF- signature)")
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.needs_pass:
                raise PipelineError("encrypted PDFs are not supported")
            n_pages = len(doc)
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError(f"PDF could not be parsed: {e}") from e
    if n_pages < 1:
        raise PipelineError("PDF has no pages")
    if n_pages > max_pdf_pages():
        raise PipelineError(
            f"PDF has {n_pages} pages; limit is {max_pdf_pages()} pages"
        )
    return {
        "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "n_pages": n_pages,
        "size_bytes": len(pdf_bytes),
        "display_name": safe_display_name(filename),
    }


def meta_client() -> OpenAI:
    load_dotenv()
    key = os.environ.get(API_KEY_ENV_VAR)
    if not key:
        raise PipelineError(
            f"{API_KEY_ENV_VAR} is not set; PDF analysis needs the Meta Model API key"
        )
    return OpenAI(base_url=API_BASE_URL, api_key=key)


# ---------------------------------------------------------------------------
# Injectable dependencies (real implementations by default; fakes in tests)
# ---------------------------------------------------------------------------


def _real_analyze(pdf_bytes: bytes, filename: str, checklist: str) -> AnalysisDraft:
    return analyze_pdf(meta_client(), DEFAULT_MODEL, pdf_bytes, filename, checklist)


def _real_score(question: str, draft: AnalysisDraft):
    from analyze_paper import RelevanceList  # noqa: F401

    return score_relevance(meta_client(), DEFAULT_MODEL, question, draft)


def _real_localize(
    pdf_path: Path, analysis: AnalysisResult, cache_path: Path
) -> tuple[LocalizedOutput, dict[str, int]]:
    """Programmatic driver over localize_objects helpers (mirrors its CLI)."""
    targets = select_localizable(analysis.objects)
    if not targets:
        raise PipelineError("analysis has no localizable objects")
    pdf_bytes = pdf_path.read_bytes()
    cache = load_cache(cache_path)
    cached: dict[str, ObjectLocalization] = {}
    for o in targets:
        hit = lookup_cached(cache, pdf_bytes, o)
        if hit is not None:
            cached[o.id] = hit
    live = [o for o in targets if o.id not in cached]
    by_page: dict[int, list] = {}
    for o in live:
        by_page.setdefault(o.page, []).append(o)
    found: list[ObjectLocalization] = []
    client = meta_client() if live else None
    if live:
        assert client is not None
        with pymupdf.open(pdf_path) as doc:
            for page_number in sorted(by_page):
                if page_number < 1 or page_number > len(doc):
                    raise PipelineError(
                        f"page {page_number} out of range (PDF has {len(doc)})"
                    )
                known = by_page[page_number]
                page = doc[page_number - 1]
                vision_known = [o for o in known if o.type != "table"]
                table_locs = []
                for t in known:
                    if t.type != "table":
                        continue
                    m = localize_table(client, DEFAULT_MODEL, page, page_number, t)
                    if m is None:
                        vision_known.append(t)
                    else:
                        table_locs.append(m)
                if vision_known:
                    from localize_objects import RENDER_MATRIX

                    page_png = page.get_pixmap(matrix=RENDER_MATRIX).tobytes("png")
                    vision_locs = build_localizations(
                        page_number, vision_known,
                        localize_page(client, DEFAULT_MODEL, page_png, page_number, vision_known),
                    )
                else:
                    vision_locs = []
                order = {o.id: i for i, o in enumerate(known)}
                locs = sorted(vision_locs + table_locs, key=lambda m: order[m.object_id])
                for o, m in zip(known, locs):
                    store_for_object(cache, pdf_bytes, o, m)
                found.extend(locs)
        save_cache(cache_path, cache)
    order = {o.id: i for i, o in enumerate(targets)}
    all_locs = sorted([*cached.values(), *found], key=lambda m: order[m.object_id])
    stats = {"targets": len(targets), "cache_hits": len(cached),
             "found": sum(1 for m in all_locs if m.bbox is not None)}
    return LocalizedOutput(analysis=analysis, localizations=all_locs), stats


def _real_investigate(project_dir: Path, paper_id: str, pdf: str, analysis: str,
                      localized: str, thread_id: Optional[str] = None,
                      sha256: str = "", question_version: int = 1) -> int:
    import agent as agent_mod

    ns = argparse.Namespace(
        project_dir=str(project_dir), paper_id=paper_id, pdf=pdf,
        analysis=analysis, localized=localized, thread_id=thread_id,
        sha256=sha256, question_version=question_version,
    )
    return agent_mod.cmd_investigate(ns)


@dataclass
class PipelineDeps:
    analyze_fn: Callable[[bytes, str, str], AnalysisDraft] = _real_analyze
    score_fn: Callable[[str, AnalysisDraft], Any] = _real_score
    localize_fn: Callable[[Path, AnalysisResult, Path], tuple[LocalizedOutput, dict]] = _real_localize
    investigate_fn: Callable[..., int] = _real_investigate


# ---------------------------------------------------------------------------
# Stage files + document-cache reuse
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def find_reusable_analysis(
    projects_root: Path, sha256: str, exclude_project: str = ""
) -> Optional[dict[str, Any]]:
    """Locate a verified same-bytes analysis: other project papers first,
    then legacy prototype analyses. Returns paths + provenance, or None."""
    if projects_root.is_dir():
        for child in sorted(projects_root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name == exclude_project:
                continue
            papers = child / "papers"
            if not papers.is_dir():
                continue
            for paper_dir in sorted(papers.iterdir()):
                src = paper_dir / "source.pdf"
                draft_p = paper_dir / "draft.json"
                loc_p = paper_dir / "localized.json"
                if not (src.is_file() and draft_p.is_file() and loc_p.is_file()):
                    continue
                try:
                    digest = hashlib.sha256(src.read_bytes()).hexdigest()
                except OSError:
                    continue
                if digest == sha256:
                    return {"kind": "project-paper", "draft": str(draft_p),
                            "localized": str(loc_p),
                            "source": f"{child.name}/{paper_dir.name}"}
    root = repo_root()
    for analysis_rel, localized_rel, source_rel in LEGACY_ANALYSIS_SOURCES:
        src = root / source_rel
        draft_src = root / analysis_rel
        loc_src = root / localized_rel
        if not (src.is_file() and draft_src.is_file() and loc_src.is_file()):
            continue
        try:
            if hashlib.sha256(src.read_bytes()).hexdigest() != sha256:
                continue
        except OSError:
            continue
        return {"kind": "legacy-analysis", "draft": str(draft_src),
                "localized": str(loc_src), "source": analysis_rel}
    return None


def load_draft_from_analysis_file(path: Path) -> AnalysisDraft:
    doc = _read_json(path)
    if isinstance(doc, dict) and "analysis" in doc and "objects" not in doc:
        doc = doc["analysis"]
    result = AnalysisResult.model_validate(doc)
    return draft_from_result(result)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


ProgressFn = Callable[[str, str], None]


def run_ingest(
    projects_root: Path,
    project_id: str,
    paper_id: str,
    pdf_bytes: bytes,
    original_filename: str,
    on_progress: Optional[ProgressFn] = None,
    deps: Optional[PipelineDeps] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Run (or resume) the full ingest pipeline for one uploaded paper.

    Resumes from per-paper stage files; never re-runs completed expensive
    stages. Raises PipelineError (user-facing) or DuplicatePDFError.
    """
    from workspace import paper_dir, project_lock, resolve_project_dir

    deps = deps or PipelineDeps()
    project_dir = resolve_project_dir(projects_root, project_id)

    def progress(stage: str, detail: str = "") -> None:
        if on_progress:
            on_progress(stage, detail)
        if should_stop and should_stop():
            raise PipelineError("cancelled by researcher")

    progress("validating", "Checking the PDF")
    info = validate_pdf(pdf_bytes, original_filename)
    sha256 = info["sha256"]

    with project_lock(project_id):
        project = load_project(project_dir)
        for p in project.papers:
            if p.sha256 == sha256:
                raise DuplicatePDFError(p.paper_id)
        if any(p.paper_id == paper_id for p in project.papers):
            raise PipelineError(f"paper '{paper_id}' is already in the project")

    pdir = paper_dir(project_dir, paper_id)
    pdir.mkdir(parents=True, exist_ok=True)
    src_pdf = pdir / "source.pdf"
    if not src_pdf.is_file():
        src_pdf.write_bytes(pdf_bytes)
        (pdir / "original_name.txt").write_text(info["display_name"], encoding="utf-8")
    else:
        # Resumed retry: the stored bytes must match this upload.
        if hashlib.sha256(src_pdf.read_bytes()).hexdigest() != sha256:
            raise PipelineError("a different PDF is already staged for this paper id")
    pipeline_path = pdir / "pipeline.json"
    pipeline: dict[str, Any] = _read_json(pipeline_path) if pipeline_path.is_file() else {
        "paper_id": paper_id, "sha256": sha256, "stages_done": [],
        "reused_from": None, "stats": {},
    }

    def stage_done(name: str) -> bool:
        return name in pipeline.get("stages_done", [])

    def mark_done(name: str, **extra: Any) -> None:
        pipeline.setdefault("stages_done", []).append(name)
        pipeline["stats"].update(extra)
        _write_json(pipeline_path, pipeline)

    # -- inventory (deterministic, paper-native) ---------------------------
    progress("reading_document", "Reading document")
    checklist_path = pdir / "checklist.txt"
    if not stage_done("inventory"):
        progress("identifying_objects", "Identifying scientific objects")
        candidates = discover_labels(src_pdf)
        checklist = format_checklist(candidates)
        checklist_path.write_text(checklist, encoding="utf-8")
        mark_done("inventory", n_labels=len(candidates))
    else:
        checklist = checklist_path.read_text(encoding="utf-8") if checklist_path.is_file() else ""

    # -- document-cache reuse (verified same bytes) -------------------------
    draft_path = pdir / "draft.json"
    localized_path = pdir / "localized.json"
    analysis_path = pdir / "analysis.json"
    if not stage_done("analysis") and not draft_path.is_file():
        reused = find_reusable_analysis(projects_root, sha256, exclude_project=project_id)
        if reused is not None:
            progress("analyzing_content", f"Reusing verified document cache ({reused['source']})")
            try:
                draft = load_draft_from_analysis_file(Path(reused["draft"]))
                loc_doc = _read_json(Path(reused["localized"]))
                locs = [ObjectLocalization.model_validate(m)
                        for m in loc_doc.get("localizations", [])]
            except Exception as e:
                raise PipelineError(f"cached analysis failed validation: {e}") from e
            _write_json(draft_path, draft.model_dump(mode="json"))
            pipeline["reused_from"] = reused["source"]
            mark_done("analysis", reused_from=reused["source"],
                       n_objects=len(draft.objects))
            # Cached boxes are valid (identical bytes); persist them alongside
            # the fresh relevance-scored analysis at localize time.
            pipeline["cached_localizations"] = [m.model_dump(mode="json") for m in locs]
            _write_json(pipeline_path, pipeline)

    # -- stage 1: inventory + explanation -----------------------------------
    if not stage_done("analysis"):
        progress("analyzing_content", "Analyzing scientific content")
        try:
            draft = deps.analyze_fn(pdf_bytes, info["display_name"], checklist)
        except PipelineError:
            raise
        except Exception as e:
            from analyze_paper import InvalidAnalysisContent

            if isinstance(e, InvalidAnalysisContent) and not checklist.strip():
                # Both the deterministic label scan and the model agree:
                # this document has no labeled scientific objects. Failing
                # honestly (and non-retryably) beats burning calls.
                raise PipelineError(NOT_A_PAPER_MESSAGE,
                                    retryable=False) from e
            raise PipelineError(f"scientific analysis failed: {e}") from e
        _write_json(draft_path, draft.model_dump(mode="json"))
        mark_done("analysis", n_objects=len(draft.objects))
    else:
        draft = AnalysisDraft.model_validate(_read_json(draft_path))

    # -- stage 2: question relevance (always fresh for THIS question) --------
    with project_lock(project_id):
        project = load_project(project_dir)
        question = project.research_question
        qv = project.question_version or 1
    progress("ranking_relevance", "Ranking relevance to your question")
    try:
        rel = deps.score_fn(question, draft)
        result = merge_relevance(draft, rel, question)
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError(f"relevance scoring failed: {e}") from e
    _write_json(analysis_path, result.model_dump(mode="json"))

    # -- localization (shared cache; reused boxes kept) ----------------------
    progress("localizing", "Localizing equations and figures")
    cached_locs = pipeline.get("cached_localizations")
    if cached_locs and pipeline.get("reused_from"):
        locs = [ObjectLocalization.model_validate(m) for m in cached_locs]
        localized = LocalizedOutput(analysis=result, localizations=locs)
        stats = {"targets": len(locs), "cache_hits": len(locs),
                 "found": sum(1 for m in locs if m.bbox is not None),
                 "reused_boxes": True}
    else:
        try:
            localized, stats = deps.localize_fn(src_pdf, result, default_cache_path())
        except PipelineError:
            raise
        except Exception as e:
            raise PipelineError(f"localization failed: {e}") from e
    _write_json(localized_path, localized.model_dump(mode="json"))
    mark_done("localization", **{k: v for k, v in stats.items() if k != "reused_boxes"})

    # -- cross-paper interpretation (Spark Open agent graph) ------------------
    progress("comparing_with_project", "Comparing with project knowledge")
    from workspace import repo_relative

    with project_lock(project_id):
        project = load_project(project_dir)
        if any(p.paper_id == paper_id for p in project.papers):
            # A concurrent/repeated run already attached this paper: the
            # agent graph is idempotent at the project level via this check.
            progress("finalizing", "Paper already attached; reusing project state")
            return {"paper_id": paper_id, "reused_attach": True,
                    "sha256": sha256, "stats": pipeline["stats"]}
        order = len(project.papers) + 1
        n_localized = sum(1 for m in localized.localizations if m.bbox)
        paper = PaperEntry(
            paper_id=paper_id, title=result.paper_title,
            filename=info["display_name"], added_order=order,
            n_objects=len(result.objects), n_localized=n_localized,
            pdf_path=repo_relative(src_pdf), localized_path=repo_relative(localized_path),
            sha256=sha256, question_version=qv,
        )
    rc = deps.investigate_fn(
        project_dir, paper_id, str(src_pdf.resolve()),
        str(analysis_path.resolve()), str(localized_path.resolve()),
        sha256=sha256, question_version=qv,
    )
    # cmd_investigate returns 0 for BOTH completed and awaiting-review runs,
    # so attachment (not the return code) decides the outcome.
    from agent_graph import list_run_records

    with project_lock(project_id):
        attached_now = any(
            p.paper_id == paper_id for p in load_project(project_dir).papers)
    if not attached_now:
        runs = [r for r in list_run_records(project_dir) if r.paper_id == paper_id]
        awaiting = [r for r in runs if r.status == "awaiting_human"]
        if awaiting:
            # Not a failure: the paper attaches when the researcher answers.
            mark_done("investigation", awaiting_review=awaiting[0].thread_id)
            progress("finalizing", "Waiting on researcher review")
            return {"paper_id": paper_id, "awaiting_review": awaiting[0].thread_id,
                    "sha256": sha256, "stats": pipeline["stats"]}
        raise PipelineError(
            "cross-paper interpretation failed; retry resumes without "
            "repeating completed stages"
        )
    if rc != 0:
        raise PipelineError(
            "cross-paper interpretation reported failure after attaching; "
            "state kept, retry is safe")
    mark_done("investigation", attached=True)
    progress("finalizing", "Finalizing project findings")
    with project_lock(project_id):
        project = load_project(project_dir)
        project.updated_at = utc_now_now()
        save_project(project_dir, project)
    return {"paper_id": paper_id, "title": result.paper_title,
            "sha256": sha256, "stats": pipeline["stats"]}


def default_cache_path() -> Path:
    return Path(__file__).resolve().parent / "loc_cache.json"


def utc_now_now() -> str:
    from workspace import utc_now_iso

    return utc_now_iso()


# ---------------------------------------------------------------------------
# Question rescoring (relevance-only; inventory + boxes preserved)
# ---------------------------------------------------------------------------


def rescore_paper(
    projects_root: Path,
    project_id: str,
    paper_id: str,
    question: str,
    question_version: int,
    deps: Optional[PipelineDeps] = None,
) -> dict[str, Any]:
    """Recompute ONE paper's relevance for a new question version.

    Reuses draft.json (paper-native inventory/explanations) and the stored
    localization boxes. Never rediscovers or relocalizes.
    """
    from workspace import paper_dir, project_lock, resolve_project_dir

    deps = deps or PipelineDeps()
    project_dir = resolve_project_dir(projects_root, project_id)
    pdir = paper_dir(project_dir, paper_id)
    draft_path = pdir / "draft.json"
    if not draft_path.is_file():
        # Legacy papers (M4 CLI) have no per-paper dir: rescore from the
        # project's stored localized fixture instead.
        return _rescore_legacy_paper(project_dir, paper_id, question,
                                     question_version, deps)
    draft = AnalysisDraft.model_validate(_read_json(draft_path))
    try:
        rel = deps.score_fn(question, draft)
        result = merge_relevance(draft, rel, question)
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError(f"relevance scoring failed for '{paper_id}': {e}") from e
    _write_json(pdir / "analysis.json", result.model_dump(mode="json"))
    loc_doc = _read_json(pdir / "localized.json")
    loc_doc["analysis"] = result.model_dump(mode="json")
    _write_json(pdir / "localized.json", loc_doc)
    with project_lock(project_id):
        project = load_project(project_dir)
        for p in project.papers:
            if p.paper_id == paper_id:
                p.question_version = question_version
        project.updated_at = utc_now_now()
        save_project(project_dir, project)
    levels = {"high": 0, "related": 0, "context": 0}
    for o in result.objects:
        levels[o.research_relevance_level] += 1
    return {"paper_id": paper_id, "levels": levels}


def _rescore_legacy_paper(
    project_dir: Path,
    paper_id: str,
    question: str,
    question_version: int,
    deps: PipelineDeps,
) -> dict[str, Any]:
    """Rescore a paper added by the M4 CLI (localized fixture outside papers/).

    The legacy fixture file is rewritten in place with fresh relevance; the
    boxes inside are preserved untouched. (Demo-project papers are never
    rescored by the acceptance flow; this path exists for user-owned
    legacy projects only.)
    """
    from workspace import project_lock

    project = load_project(project_dir)
    paper = next((p for p in project.papers if p.paper_id == paper_id), None)
    if paper is None or not paper.localized_path:
        raise PipelineError(f"paper '{paper_id}' has no stored analysis to rescore")
    loc_path = repo_root() / paper.localized_path
    if not loc_path.is_file():
        raise PipelineError(f"stored analysis missing for '{paper_id}'")
    doc = _read_json(loc_path)
    result = AnalysisResult.model_validate(doc["analysis"])
    draft = draft_from_result(result)
    try:
        rel = deps.score_fn(question, draft)
        new_result = merge_relevance(draft, rel, question)
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError(f"relevance scoring failed for '{paper_id}': {e}") from e
    doc["analysis"] = new_result.model_dump(mode="json")
    _write_json(loc_path, doc)
    with project_lock(project_dir.resolve().name):
        project = load_project(project_dir)
        for p in project.papers:
            if p.paper_id == paper_id:
                p.question_version = question_version
        project.updated_at = utc_now_now()
        save_project(project_dir, project)
    levels = {"high": 0, "related": 0, "context": 0}
    for o in new_result.objects:
        levels[o.research_relevance_level] += 1
    return {"paper_id": paper_id, "levels": levels, "legacy": True}
