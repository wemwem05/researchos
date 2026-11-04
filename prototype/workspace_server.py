#!/usr/bin/env python3
"""ResearchOS Milestone 7: multi-project workspace API server.

Extends the Milestone 5 single-project agent server into a local multi-project
service. The browser talks to this backend for everything; backend state is
authoritative and static fixtures remain legacy/demo fallbacks only.

New API (JSON envelope {"ok": true, "data": ...} / {"ok": false, "error": ...}):

  GET    /api/health
  GET    /api/projects
  POST   /api/projects                       {title, research_question}
  GET    /api/projects/<id>
  PATCH  /api/projects/<id>                  {title}
  GET    /api/projects/<id>/papers
  POST   /api/projects/<id>/papers           multipart file (+paper_id?)
  GET    /api/projects/<id>/papers/<pid>
  DELETE /api/projects/<id>/papers/<pid>   (staged/failed papers only;
                                            attached papers are kept)
  GET    /api/projects/<id>/papers/<pid>/file   (raw PDF bytes)
  GET    /api/projects/<id>/papers/<pid>/data   (raw localized JSON)
  GET    /api/projects/<id>/jobs
  GET    /api/jobs/<job>
  POST   /api/jobs/<job>/retry
  POST   /api/jobs/<job>/cancel
  GET    /api/projects/<id>/question/preview
  POST   /api/projects/<id>/question         {research_question, confirm?}
  POST   /api/projects/<id>/rebuild          (continue an incomplete rebuild)
  GET    /api/projects/<id>/runs
  GET    /api/projects/<id>/runs/<thread>
  POST   /api/projects/<id>/runs/<thread>/review
  GET    /api/projects/<id>/telemetry
  GET    /api/projects/<id>/verifications
  POST   /api/projects/<id>/verifications/start
  GET    /api/projects/<id>/verifications/<t>/files/<n>
  GET    /api/projects/<id>/audits
  POST   /api/projects/<id>/audits/start
  GET    /api/projects/<id>/discoveries
  POST   /api/projects/<id>/discoveries/start   {focus?, budget?, request_key?}
  GET    /api/projects/<id>/discoveries/<run>
  POST   /api/projects/<id>/discoveries/<run>/decide   {decisions: [...]}
  POST   /api/projects/<id>/discoveries/<run>/import   {candidate_ids: [...]}

Legacy M5/M6/M6.1 routes (/api/agent/...) keep their exact shapes and map to
the default project (ot-correspondence). No arbitrary filesystem paths are
ever exposed; every operation is scoped to its project id.

Local development only: binds 127.0.0.1, explicit localhost CORS allowlist,
no auth. Production deployment requires authentication, authorization,
durable hosted storage, and job isolation (see docs/WORKSPACE_ARCHITECTURE.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jobs as jobs_mod
import paper_pipeline as pipeline_mod
import workspace as ws
from paper_pipeline import DuplicatePDFError, PipelineDeps, PipelineError

PROJECTS_ROOT: Path = ws.default_projects_root()
DEFAULT_PORT = 8765

# API revision served in /api/health. dev.sh refuses to reuse a backend whose
# version differs (stale code fails cryptically, e.g. CORS-blocked methods).
# Bump on ANY route/shape/CORS change, and keep dev.sh in sync.
SERVER_VERSION = "8.0"

# Explicit CORS allowlist for local development (never "*").
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}

MAX_JSON_BODY = 1024 * 1024  # 1 MB; uploads use the PDF limit instead

# Injected pipeline clients (tests set fakes; production leaves None = real).
PIPELINE_DEPS: PipelineDeps | None = None


@dataclass
class DiscoveryDeps:
    """Injected discovery clients. None = real (Spark Open via Baseten,
    live OpenAlex, real PDF fetch). Tests inject fakes."""

    spark: Any = None
    openalex: Any = None
    fetch_pdf: Any = None


DISCOVERY_DEPS: DiscoveryDeps | None = None

_dispatcher_stop = threading.Event()
_dispatcher_thread: threading.Thread | None = None


def deps() -> PipelineDeps:
    return PIPELINE_DEPS or PipelineDeps()


def discovery_deps() -> DiscoveryDeps:
    return DISCOVERY_DEPS or DiscoveryDeps()


# Hard ceilings for researcher-supplied discovery budgets (spec defaults
# are lower; researchers may lower budgets but never exceed these).
DISCOVERY_BUDGET_CAPS = {
    "max_rounds": 3,
    "max_queries_per_round": 5,
    "max_records": 100,
    "max_shortlist": 12,
    "max_recommendations": 8,
    "max_expansions": 4,
    "max_deep_inspections": 5,
}

MAX_DISCOVERY_FOCUS = 500
REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def validated_discovery_budget(raw: Any) -> dict[str, int]:
    """Validate a researcher budget override; unknown keys reject."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("budget must be an object")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if key not in DISCOVERY_BUDGET_CAPS:
            raise ValueError(f"unknown budget key '{key}'")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"budget '{key}' must be an integer")
        if value < 1:
            raise ValueError(f"budget '{key}' must be >= 1")
        out[key] = min(value, DISCOVERY_BUDGET_CAPS[key])
    return out


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _cors_headers(handler: BaseHTTPRequestHandler) -> list[tuple[str, str]]:
    origin = handler.headers.get("Origin", "")
    headers = [("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"),
               ("Access-Control-Allow-Headers", "Content-Type")]
    if origin in ALLOWED_ORIGINS:
        headers.append(("Access-Control-Allow-Origin", origin))
        headers.append(("Vary", "Origin"))
    return headers


def _send(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in _cors_headers(handler):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _ok(handler: BaseHTTPRequestHandler, data: object, status: int = 200) -> None:
    _send(handler, status, {"ok": True, "data": data})


def _err(handler: BaseHTTPRequestHandler, status: int, error: str,
         details: object = None) -> None:
    payload: dict = {"ok": False, "error": error}
    if details is not None:
        payload["details"] = details
    _send(handler, status, payload)


def _send_bytes(handler: BaseHTTPRequestHandler, status: int, body: bytes,
                content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in _cors_headers(handler):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler, limit: int) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        length = 0
    if length < 0 or length > limit:
        raise ValueError(f"request body exceeds {limit} bytes")
    return handler.rfile.read(length) if length else b""


def _read_json(handler: BaseHTTPRequestHandler) -> tuple[dict | None, str | None]:
    try:
        raw = _read_body(handler, MAX_JSON_BODY)
    except ValueError as e:
        return None, str(e)
    if not raw:
        return {}, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid JSON body"
    if not isinstance(payload, dict):
        return None, "JSON body must be an object"
    return payload, None


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Minimal binary-safe multipart/form-data parser.

    Returns (fields, files) where files maps field name -> (filename, bytes).
    """
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip().strip('"')
    if not boundary:
        raise ValueError("multipart body is missing its boundary")
    delimiter = ("--" + boundary).encode("latin-1")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(delimiter):
        if not chunk:
            continue
        # Each part is framed as CRLF + headers + CRLFCRLF + data + CRLF.
        # Strip exactly one framing CRLF on each side — never more, so
        # significant trailing bytes of the payload (e.g. a PDF's final
        # newline) survive byte-identical.
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if not chunk or chunk == b"--":
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in head.decode("latin-1").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        params: dict[str, str] = {}
        for item in disp.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                params[k.strip()] = v.strip().strip('"')
        name = params.get("name", "")
        if not name:
            continue
        if "filename" in params:
            files[name] = (params.get("filename", ""), data)
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


# ---------------------------------------------------------------------------
# Project views
# ---------------------------------------------------------------------------


def paper_status(project_id: str, project) -> dict[str, dict]:
    """Per-paper live status: ready vs real job state (never invented)."""
    from workspace import resolve_project_dir

    status: dict[str, dict] = {}
    project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
    attached = {p.paper_id: p for p in project.papers}
    qv = project.question_version or 1
    for pid, p in attached.items():
        status[pid] = {"status": "ready",
                       "relevance_stale": (p.question_version or 1) != qv,
                       "job_id": None}
    jobs = jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=200)
    latest: dict[str, dict] = {}
    for j in jobs:
        if j["job_type"] == "ingest_paper" and j["paper_id"] and j["paper_id"] not in latest:
            latest[j["paper_id"]] = j
    # Rescore/rebuild jobs are project-wide: surface their per-paper state
    # (awaiting review / still to rebuild) on the affected papers.
    for j in jobs:
        if j["job_type"] not in ("rescore_question", "rebuild_findings"):
            continue
        res = j.get("result") or {}
        if j["state"] in ("QUEUED", "RUNNING"):
            for pid in (res.get("remaining") or []) if isinstance(res, dict) else []:
                if pid not in attached and pid not in latest:
                    latest[pid] = {**j, "paper_id": pid}
        if isinstance(res, dict) and res.get("awaiting_paper"):
            pid = res["awaiting_paper"]
            if pid not in attached:
                latest[pid] = {"job_id": j["job_id"], "job_type": j["job_type"],
                               "state": "COMPLETED", "stage": "awaiting_review",
                               "stage_detail": j.get("stage_detail", ""),
                               "last_error": "", "result": res}
        if isinstance(res, dict) and j["state"] == "COMPLETED":
            for pid in res.get("remaining") or []:
                if pid not in attached and pid not in latest:
                    latest[pid] = {"job_id": j["job_id"], "job_type": j["job_type"],
                                   "state": "COMPLETED", "stage": "awaiting_review",
                                   "stage_detail": "Rebuild paused for review; continue it from the project view",
                                   "last_error": "", "result": res}
    for pid, j in latest.items():
        if pid in attached:
            continue
        state = j["state"]
        if state == "COMPLETED" and (j["result"] or {}).get("awaiting_review"):
            st = "awaiting_review"
        elif state in ("QUEUED", "RUNNING"):
            st = "processing"
        elif state == "FAILED":
            st = "failed"
        elif state == "CANCELLED":
            st = "cancelled"
        else:
            st = "ready"
        # Removed papers stay removed: a terminal job row without a paper dir
        # (or attachment) is history, not a live paper — the jobs list keeps
        # the record. Active jobs are always shown, even mid-flight.
        if st not in ("processing", "awaiting_review"):
            try:
                from workspace import paper_dir as _paper_dir
                if not _paper_dir(project_dir, pid).is_dir():
                    continue
            except ValueError:
                continue
        status[pid] = {"status": st, "job_id": j["job_id"],
                       "stage": j["stage"], "stage_detail": j["stage_detail"],
                       "last_error": j["last_error"],
                       "result": j["result"]}
    # Paper dirs with staged uploads but no job record yet (crash between
    # write and insert is impossible — insert precedes dispatch — but a
    # deleted-jobs DB should still surface honestly).
    papers_root = project_dir / "papers"
    if papers_root.is_dir():
        for child in papers_root.iterdir():
            if child.is_dir() and child.name not in status and child.name not in attached:
                status[child.name] = {"status": "incomplete", "job_id": None,
                                      "last_error": "staged but never processed"}
    return status


def project_view(project_id: str) -> dict:
    project_dir, project = ws.get_project(PROJECTS_ROOT, project_id)
    return {"project": project.model_dump(mode="json"),
            "paper_status": paper_status(project_id, project),
            "rebuild": rebuild_status(project_dir, project)}


# ---------------------------------------------------------------------------
# Background dispatcher
# ---------------------------------------------------------------------------


def _job_cancelled(job_id: str) -> bool:
    try:
        return jobs_mod.get_job(PROJECTS_ROOT, job_id)["state"] == "CANCELLED"
    except FileNotFoundError:
        return True


def run_ingest_job(job: dict) -> None:
    project_id, paper_id = job["project_id"], job["paper_id"]
    job_id = job["job_id"]
    params = job.get("params") or {}
    sha256 = params.get("sha256", "")

    def on_progress(stage: str, detail: str = "") -> None:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, stage=stage, stage_detail=detail)

    def should_stop() -> bool:
        return _job_cancelled(job_id)

    from workspace import paper_dir, resolve_project_dir

    project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
    src = paper_dir(project_dir, paper_id) / "source.pdf"
    if not src.is_file():
        raise PipelineError("staged PDF is missing; re-upload the paper")
    pdf_bytes = src.read_bytes()
    if sha256 and hashlib.sha256(pdf_bytes).hexdigest() != sha256:
        raise PipelineError("staged PDF changed unexpectedly; re-upload the paper")
    on_progress("uploading", "Upload received")
    result = pipeline_mod.run_ingest(
        PROJECTS_ROOT, project_id, paper_id, pdf_bytes,
        params.get("filename", "paper.pdf"),
        on_progress=on_progress, deps=deps(), should_stop=should_stop,
    )
    jobs_mod.update_job(PROJECTS_ROOT, job_id, state="COMPLETED",
                        stage="ready", stage_detail="Paper ready",
                        result=result)


def snapshot_papers(project_dir: Path, project) -> list[dict]:
    """Rebuild candidates: attached papers (in order) plus complete but
    unattached paper dirs (a rebuild interrupted by human review detaches
    pending papers; continuations must see them). Failed ingests without a
    finished analysis are excluded — they need ingest retry, not rebuild."""
    snaps = [p.model_dump()
             for p in sorted(project.papers, key=lambda p: p.added_order)]
    attached = {p.paper_id for p in project.papers}
    papers_root = project_dir / "papers"
    if papers_root.is_dir():
        for child in sorted(papers_root.iterdir()):
            if not child.is_dir() or child.name in attached:
                continue
            if not ((child / "analysis.json").is_file()
                    and (child / "localized.json").is_file()
                    and (child / "source.pdf").is_file()):
                continue
            sha256 = ""
            pipe = child / "pipeline.json"
            if pipe.is_file():
                try:
                    sha256 = json.loads(pipe.read_text(encoding="utf-8")).get("sha256", "")
                except (json.JSONDecodeError, OSError):
                    sha256 = ""
            snaps.append({"paper_id": child.name, "sha256": sha256,
                          "added_order": 10_000 + len(snaps)})
    return snaps


def rebuild_status(project_dir: Path, project) -> dict:
    """Whether a rebuild continuation is needed (partial post-review state)."""
    interpreted = {c.paper_id for c in project.contributions}
    snaps = snapshot_papers(project_dir, project)
    missing = [s["paper_id"] for s in snaps if s["paper_id"] not in interpreted]
    if not missing:
        return {"needed": False, "reason": "", "papers": []}
    return {"needed": True,
            "reason": f"{len(missing)} paper(s) lack conclusions for the current question",
            "papers": missing}


def run_rescore_job(job: dict) -> None:
    """Question-version recomputation: relevance for all papers, then a
    findings rebuild in paper order. Idempotent: attached papers are skipped,
    so retries and post-review continuations never duplicate conclusions."""
    from project import load_project, save_project
    from workspace import paper_dir, project_lock, resolve_project_dir

    project_id = job["project_id"]
    job_id = job["job_id"]
    params = job.get("params") or {}
    question = params.get("question", "")
    qv = int(params.get("question_version", 0) or 0)
    if not question or not qv:
        raise PipelineError("rescore job is missing its question parameters")
    project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)

    def progress(stage: str, detail: str = "") -> None:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, stage=stage, stage_detail=detail)
        if _job_cancelled(job_id):
            raise PipelineError("cancelled by researcher")

    progress("preparing", "Preparing recomputation")
    with project_lock(project_id):
        project = load_project(project_dir)
        if (project.question_version or 1) != qv:
            raise PipelineError(
                "project moved to a newer question; this recomputation is obsolete"
            )
        snapshots = snapshot_papers(project_dir, project)
    rescored = []
    d = deps()
    for snap in snapshots:
        pid = snap["paper_id"]
        progress("ranking_relevance", f"Ranking relevance: {pid}")
        rescored.append(pipeline_mod.rescore_paper(
            PROJECTS_ROOT, project_id, pid, question, qv, deps=d))
    # Rebuild findings via the real agent graph, in original paper order.
    # A paper needs (re)interpretation when it has no contribution record for
    # the current version (the question change archived conclusions and
    # cleared contributions). Papers already interpreted — e.g. on retry or
    # post-review continuation — are skipped, never duplicated.
    progress("rebuilding_findings", "Rebuilding project findings")
    with project_lock(project_id):
        project = load_project(project_dir)
        if (project.question_version or 1) != qv:
            raise PipelineError("project moved to a newer question during rescore")
        interpreted = {c.paper_id for c in project.contributions}
        remaining = [s for s in snapshots if s["paper_id"] not in interpreted]
        if not remaining:
            progress("finalizing", "Findings already rebuilt")
            jobs_mod.update_job(PROJECTS_ROOT, job_id, state="COMPLETED",
                                stage="ready", stage_detail="Recomputation complete",
                                result={"rescored": rescored, "rebuilt": 0,
                                        "skipped_attached": True})
            return
        # Detach pending papers so the investigator re-adds each one through
        # the validated M5 path; interpreted papers (and their findings) stay.
        pending_ids = {s["paper_id"] for s in remaining}
        project.papers = [p for p in project.papers
                          if p.paper_id not in pending_ids]
        save_project(project_dir, project)
    rebuilt = 0
    remaining_ids = [s["paper_id"] for s in remaining]
    for index, snap in enumerate(remaining):
        pid = snap["paper_id"]
        progress("rebuilding_findings", f"Interpreting {pid} for the new question")
        with project_lock(project_id):
            project = load_project(project_dir)
            if any(p.paper_id == pid for p in project.papers):
                continue
        _, pdf_p, analysis_p, loc_p = _rebuild_paths(project_dir, pid, snap)
        code = d.investigate_fn(
            project_dir, pid, pdf_p, analysis_p, loc_p,
            sha256=snap.get("sha256", "") or "",
            question_version=qv,
        )
        # The investigator returns 0 for completed AND awaiting-review runs;
        # attachment (not the return code) decides the outcome.
        from agent_graph import list_run_records

        with project_lock(project_id):
            attached_now = any(
                p.paper_id == pid
                for p in load_project(project_dir).papers)
        if not attached_now:
            runs = [r for r in list_run_records(project_dir) if r.paper_id == pid]
            awaiting = [r for r in runs if r.status == "awaiting_human"]
            if awaiting:
                jobs_mod.update_job(
                    PROJECTS_ROOT, job_id, state="COMPLETED",
                    stage="awaiting_review",
                    stage_detail=f"Waiting on your review for {pid}",
                    result={"rescored": rescored, "rebuilt": rebuilt,
                            "awaiting_review": awaiting[0].thread_id,
                            "awaiting_paper": pid,
                            "remaining": remaining_ids[index + 1:]})
                return
            raise PipelineError(
                f"rebuilding findings for '{pid}' failed; completed papers are "
                "kept and retry resumes with the next paper")
        if code != 0:
            raise PipelineError(
                f"rebuilding '{pid}' reported failure after attaching; "
                "state kept, retry is safe")
        rebuilt += 1
    progress("finalizing", "Finalizing recomputation")
    with project_lock(project_id):
        project = load_project(project_dir)
        project.updated_at = ws.utc_now_iso()
        save_project(project_dir, project)
    jobs_mod.update_job(PROJECTS_ROOT, job_id, state="COMPLETED",
                        stage="ready", stage_detail="Recomputation complete",
                        result={"rescored": rescored, "rebuilt": rebuilt})


def _rebuild_paths(project_dir: Path, paper_id: str, snap: dict) -> tuple[int, str, str, str]:
    """Absolute (pdf, analysis, localized) paths for a rebuild investigation.

    Workspace papers use their per-paper dir; legacy CLI papers reuse their
    stored fixture paths (analysis extracted from the localized wrapper).
    """
    import json as _json

    pdir = project_dir / "papers" / paper_id
    if (pdir / "analysis.json").is_file() and (pdir / "localized.json").is_file():
        return 0, str((pdir / "source.pdf").resolve()), str((pdir / "analysis.json").resolve()), \
            str((pdir / "localized.json").resolve())
    # Legacy paper: materialize a plain analysis file from the wrapper.
    root = Path(__file__).resolve().parent.parent
    loc_rel = snap.get("localized_path") or ""
    pdf_rel = snap.get("pdf_path") or ""
    loc_path = (root / loc_rel) if loc_rel else None
    pdf_path = (root / pdf_rel) if pdf_rel else None
    if loc_path is None or not loc_path.is_file():
        raise PipelineError(f"stored analysis missing for '{paper_id}'")
    if pdf_path is None or not pdf_path.is_file():
        raise PipelineError(f"stored PDF missing for '{paper_id}'")
    doc = _json.loads(loc_path.read_text(encoding="utf-8"))
    pdir.mkdir(parents=True, exist_ok=True)
    analysis_out = pdir / "analysis.json"
    if not analysis_out.is_file():
        analysis_out.write_text(_json.dumps(doc["analysis"], indent=2) + "\n",
                                encoding="utf-8")
    return 0, str(pdf_path.resolve()), str(analysis_out.resolve()), str(loc_path.resolve())


def run_discovery_job(job: dict) -> None:
    """Milestone 8: run the agentic literature-discovery graph once.

    Each attempt uses its own thread id (retries never resume a failed
    graph mid-state); only the successful attempt persists a DiscoveryRun.
    """
    from discovery_graph import run_discovery
    from discovery_models import DiscoveryBudget, DiscoveryFailure
    from project import load_project
    from workspace import resolve_project_dir

    project_id = job["project_id"]
    job_id = job["job_id"]
    params = job.get("params") or {}
    focus = str(params.get("focus", "") or "")
    attempt = int(job.get("attempts", 1) or 1)
    suffix = "" if attempt <= 1 else f"-r{attempt}"
    thread_id = f"discover-{job_id}{suffix}"
    budget = DiscoveryBudget(**(params.get("budget") or {}))
    project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)

    def progress(stage: str, detail: str = "") -> None:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, stage=stage,
                            stage_detail=detail)
        if _job_cancelled(job_id):
            raise PipelineError("cancelled by researcher")

    progress("searching", "Planning search strategy")
    d = discovery_deps()
    try:
        run_discovery(project_dir, thread_id, focus=focus, budget=budget,
                      spark=d.spark, openalex=d.openalex)
    except DiscoveryFailure as e:
        raise PipelineError(f"literature discovery failed: {e}",
                            retryable=True) from e
    progress("finalizing", "Saving recommendations")
    project = load_project(project_dir)
    run = next((r for r in (project.discoveries or [])
                if r.run_id == thread_id), None)
    counts = dict(run.counts) if run is not None else {}
    jobs_mod.update_job(
        PROJECTS_ROOT, job_id, state="COMPLETED", stage="ready",
        stage_detail=(f"Discovery complete: {counts.get('recommended', 0)} "
                      "recommended"),
        result={"run_id": thread_id, "thread_id": thread_id, "counts": counts},
        run_ids=[thread_id])


def _import_paper_id(run_id: str, candidate_id: str) -> str:
    """Deterministic server-generated paper slug per (run, candidate)."""
    slug = re.sub(r"[^a-z0-9]+", "-", run_id.lower()).strip("-")
    slug = slug.replace("discover-job-", "") or slug
    return f"disc-{slug[-6:]}-{candidate_id.lower()}"


def _record_import_provenance(project_dir: Path, paper_id: str, run_id: str,
                              candidate_id: str, cand: Any) -> None:
    """Reverse link paper -> discovery record (feeds future project dedup
    with stable identifiers). Best-effort: the candidate record already
    holds paper_id, so a failure here never fails the import."""
    import tempfile as _tempfile

    from workspace import paper_dir

    pipe = paper_dir(project_dir, paper_id) / "pipeline.json"
    try:
        doc = json.loads(pipe.read_text(encoding="utf-8")) \
            if pipe.is_file() else {}
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc["discovered_from"] = {
        "run_id": run_id, "candidate_id": candidate_id,
        "doi": getattr(cand, "doi", ""), "openalex_id": getattr(cand, "openalex_id", ""),
        "arxiv_id": getattr(cand, "arxiv_id", ""), "title": getattr(cand, "title", ""),
    }
    try:
        pipe.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(dir=str(pipe.parent), prefix="pipeline-",
                                    suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, pipe)
    except OSError:
        pass


def _another_import_active(project_id: str, job_id: str) -> bool:
    for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=50):
        if j["job_id"] != job_id and j["job_type"] == "import_discovery" \
                and j["state"] in ("QUEUED", "RUNNING"):
            return True
    return False


def _import_one_candidate(
    project_dir: Path,
    project_id: str,
    run_id: str,
    candidate_id: str,
    on_stage: Any,
    should_stop: Any,
    job_id: str = "",
) -> dict[str, Any]:
    """Download + ingest one approved candidate. Returns a per-item result;
    raises PipelineError only for job-level aborts (cancel/crash)."""
    import shutil

    from acquisition import AcquisitionError, fetch_pdf, stage_downloaded_pdf
    from discovery_graph import update_candidate_status
    from discovery_models import normalize_title
    from project import load_project
    from workspace import paper_dir, project_lock

    item: dict[str, Any] = {"candidate_id": candidate_id, "ok": False}
    with project_lock(project_id):
        project = load_project(project_dir)
        run = next((r for r in (project.discoveries or [])
                    if r.run_id == run_id), None)
        if run is None:
            raise PipelineError(f"unknown discovery run '{run_id}'",
                                retryable=False)
        cand = next((c for c in run.candidates
                     if c.candidate_id == candidate_id), None)
        if cand is None:
            item["error"] = f"unknown candidate '{candidate_id}'"
            item["retryable"] = False
            return item
        status, pdf_url = cand.status, cand.pdf_url
        paper_titles = {normalize_title(p.title): p.paper_id
                        for p in project.papers}
        attached_ids = {p.paper_id for p in project.papers}
        title_key = normalize_title(cand.title)
        importing_paper_id = cand.project_paper_id
    if should_stop():
        raise PipelineError("cancelled by researcher")
    if status == "imported":
        item.update(ok=True, paper_id=cand.project_paper_id,
                    note="already imported; skipped")
        return item
    if status == "importing":
        # Self-heal: the paper attached since (researcher answered the
        # investigator's review) — finalize instead of re-importing.
        if importing_paper_id and importing_paper_id in attached_ids:
            update_candidate_status(
                project_dir, run_id, candidate_id, "imported",
                reason="attached after researcher review",
                evidence_access_level="full_text_analyzed")
            item.update(ok=True, paper_id=importing_paper_id,
                        note="attached after review; finalized")
            return item
        if _another_import_active(project_id, job_id):
            item.update(ok=True, note="another import is already handling "
                                      "this candidate; skipped")
            return item
        # Stale (previous import crashed): fall through and re-run; the
        # deterministic paper id + resumable ingest make this safe.
    if status == "dismissed":
        item["error"] = "candidate was dismissed; approve it first to import"
        item["retryable"] = False
        return item
    if status == "pending":
        update_candidate_status(project_dir, run_id, candidate_id, "approved",
                                reason="approved via import")
    if title_key and title_key in paper_titles:
        dup = paper_titles[title_key]
        item["error"] = (f"a paper with the same title is already in the "
                         f"project as '{dup}'")
        item["duplicate_of"] = dup
        item["retryable"] = False
        return item
    if not pdf_url:
        update_candidate_status(project_dir, run_id, candidate_id,
                                "full_text_unavailable",
                                reason="provider advertises no PDF URL")
        item["error"] = ("full text unavailable: the provider lists no "
                         "open-access PDF for this record")
        item["unavailable"] = True
        item["retryable"] = False
        return item
    paper_id = _import_paper_id(run_id, candidate_id)
    item["paper_id"] = paper_id
    on_stage("downloading", f"Downloading full text: {cand.title[:80]}")
    fetcher = discovery_deps().fetch_pdf or fetch_pdf
    try:
        fetched = fetcher(pdf_url)
    except AcquisitionError as e:
        update_candidate_status(project_dir, run_id, candidate_id,
                                "import_failed",
                                reason=f"download failed: {e}")
        item["error"] = f"full-text download failed: {e}"
        item["retryable"] = e.retryable
        return item
    if should_stop():
        raise PipelineError("cancelled by researcher")
    stage_downloaded_pdf(project_dir, paper_id, fetched.pdf_bytes)
    update_candidate_status(project_dir, run_id, candidate_id, "importing",
                            reason=f"downloaded {fetched.size} bytes",
                            project_paper_id=paper_id, sha256=fetched.sha256)
    on_stage("analyzing", f"Analyzing imported paper: {cand.title[:80]}")

    def _sub(stage: str, detail: str = "") -> None:
        on_stage(stage, detail)

    try:
        result = pipeline_mod.run_ingest(
            PROJECTS_ROOT, project_id, paper_id, fetched.pdf_bytes,
            f"{paper_id}.pdf", on_progress=_sub, deps=deps(),
            should_stop=should_stop)
    except DuplicatePDFError as e:
        try:
            shutil.rmtree(paper_dir(project_dir, paper_id))
        except OSError:
            pass
        update_candidate_status(project_dir, run_id, candidate_id, "imported",
                                reason="identical PDF already in project as "
                                       f"'{e.paper_id}'",
                                project_paper_id=e.paper_id,
                                sha256=fetched.sha256,
                                evidence_access_level="full_text_analyzed")
        item.update(ok=True, paper_id=e.paper_id, duplicate_of=e.paper_id,
                    note="identical PDF already in project; linked instead")
        return item
    except PipelineError as e:
        update_candidate_status(project_dir, run_id, candidate_id,
                                "import_failed", reason=str(e))
        item["error"] = str(e)
        item["retryable"] = e.retryable
        return item
    if result.get("awaiting_review"):
        # Analysis is done; findings attach when the researcher answers.
        # The candidate stays "importing" and finalizes on the next
        # import call (self-heal above) once the paper attaches.
        update_candidate_status(
            project_dir, run_id, candidate_id, "importing",
            reason="analysis complete; waiting on your review "
                   f"({result['awaiting_review']}) to attach findings")
        item.update(ok=True, awaiting_review=result["awaiting_review"],
                    note="analysis complete; answer the review to attach "
                         "findings, then re-import to finalize")
        return item
    update_candidate_status(project_dir, run_id, candidate_id, "imported",
                            reason="ingested through the M7 pipeline",
                            sha256=fetched.sha256,
                            evidence_access_level="full_text_analyzed")
    _record_import_provenance(project_dir, paper_id, run_id, candidate_id, cand)
    item.update(ok=True, ingest=result)
    return item


def run_import_job(job: dict) -> None:
    """Milestone 8: import approved discovery candidates through M7 ingest.

    Per-candidate results (never a mid-list abort): the job COMPLETES once
    every candidate reached a terminal state; failures are explicit,
    per-item, and re-importable.
    """
    from workspace import resolve_project_dir

    project_id = job["project_id"]
    job_id = job["job_id"]
    params = job.get("params") or {}
    run_id = str(params.get("run_id", ""))
    candidate_ids = [str(c) for c in (params.get("candidate_ids") or [])]
    if not run_id or not candidate_ids:
        raise PipelineError("import job is missing its run parameters",
                            retryable=False)
    project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)

    def on_stage(stage: str, detail: str = "") -> None:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, stage=stage,
                            stage_detail=detail)

    def should_stop() -> bool:
        return _job_cancelled(job_id)

    on_stage("preparing", f"Importing {len(candidate_ids)} candidate(s)")
    items = [_import_one_candidate(project_dir, project_id, run_id, cid,
                                   on_stage, should_stop, job_id)
             for cid in candidate_ids]
    if _job_cancelled(job_id):
        raise PipelineError("cancelled by researcher")
    ok_count = sum(1 for i in items if i.get("ok"))
    jobs_mod.update_job(
        PROJECTS_ROOT, job_id, state="COMPLETED", stage="ready",
        stage_detail=(f"Import finished: {ok_count}/{len(items)} imported"),
        result={"run_id": run_id, "items": items,
                "imported": ok_count, "total": len(items)})


def _dispatch_once() -> bool:
    job = jobs_mod.claim_next_queued(PROJECTS_ROOT)
    if job is None:
        return False
    job_id = job["job_id"]
    try:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, stage_detail="Job started")
        if job["job_type"] == "ingest_paper":
            run_ingest_job(jobs_mod.get_job(PROJECTS_ROOT, job_id))
        elif job["job_type"] in ("rescore_question", "rebuild_findings"):
            run_rescore_job(jobs_mod.get_job(PROJECTS_ROOT, job_id))
        elif job["job_type"] == "discover_papers":
            run_discovery_job(jobs_mod.get_job(PROJECTS_ROOT, job_id))
        elif job["job_type"] == "import_discovery":
            run_import_job(jobs_mod.get_job(PROJECTS_ROOT, job_id))
        else:
            raise PipelineError(f"unknown job type '{job['job_type']}'")
    except DuplicatePDFError as e:
        jobs_mod.update_job(PROJECTS_ROOT, job_id, state="FAILED",
                            last_error=str(e),
                            result={"duplicate_of": e.paper_id,
                                    "retryable": False})
    except PipelineError as e:
        if str(e) == "cancelled by researcher" and _job_cancelled(job_id):
            pass  # CANCELLED already recorded; leave it.
        else:
            try:
                jobs_mod.update_job(PROJECTS_ROOT, job_id, state="FAILED",
                                    last_error=str(e),
                                    result={"retryable": e.retryable})
            except FileNotFoundError:
                pass
    except Exception as e:  # noqa: BLE001 - dispatcher must survive job bugs
        traceback.print_exc()
        try:
            jobs_mod.update_job(PROJECTS_ROOT, job_id, state="FAILED",
                                last_error=f"{type(e).__name__}: {e}")
        except FileNotFoundError:
            pass
    return True


def dispatcher_loop(poll_s: float = 1.0) -> None:
    while not _dispatcher_stop.is_set():
        try:
            worked = _dispatch_once()
        except Exception:  # noqa: BLE001 - never kill the dispatcher
            traceback.print_exc()
            worked = False
        _dispatcher_stop.wait(0.1 if worked else poll_s)


def start_dispatcher() -> None:
    global _dispatcher_thread
    if _dispatcher_thread and _dispatcher_thread.is_alive():
        return
    _dispatcher_stop.clear()
    _dispatcher_thread = threading.Thread(target=dispatcher_loop, daemon=True,
                                          name="workspace-dispatcher")
    _dispatcher_thread.start()


def stop_dispatcher() -> None:
    _dispatcher_stop.set()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchOSWorkspace/7.0"

    def log_message(self, *args):  # noqa: ANN002 - stdlib signature
        sys.stderr.write("[workspace-server] %s\n" % args[1])

    def do_OPTIONS(self):  # noqa: N802 - stdlib name
        # Proper empty 204: a body on 204 violates the spec and risks
        # picky preflight handling, so headers are written by hand.
        self.send_response(204)
        for k, v in _cors_headers(self):
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing ---------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib name
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            seg = [s for s in path.split("/") if s]
            if path == "/api/health":
                _ok(self, {"status": "ok", "time": ws.utc_now_iso(),
                           "default_project": ws.DEFAULT_PROJECT_ID,
                           "server_version": SERVER_VERSION})
            elif seg[:2] == ["api", "projects"] and len(seg) == 2:
                _ok(self, {"projects": ws.list_projects(PROJECTS_ROOT)})
            elif seg[:2] == ["api", "projects"] and len(seg) == 3:
                self._get_project(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "papers":
                self._list_papers(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "papers":
                self._get_paper(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 6 and seg[3] == "papers" \
                    and seg[5] == "file":
                self._get_paper_file(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 6 and seg[3] == "papers" \
                    and seg[5] == "data":
                self._get_paper_data(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "jobs":
                self._get_or_404(seg[2], lambda pid: {"jobs": jobs_mod.list_jobs(PROJECTS_ROOT, pid)})
            elif seg[:2] == ["api", "jobs"] and len(seg) == 3:
                self._get_job(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "question" \
                    and seg[4] == "preview":
                self._get_or_404(seg[2], lambda pid: ws.question_change_preview(PROJECTS_ROOT, pid))
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "runs":
                self._get_runs(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "runs":
                self._get_run(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "telemetry":
                self._get_telemetry(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "verifications":
                self._get_verifications(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 7 and seg[3] == "verifications" \
                    and seg[5] == "files":
                self._serve_verify_file(seg[2], seg[4], seg[6])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "audits":
                self._get_audits(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "discoveries":
                self._list_discoveries(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "discoveries":
                self._get_discovery(seg[2], seg[4])
            elif seg[0:2] == ["api", "agent"]:
                self._legacy_get(seg[2:])
            else:
                _err(self, 404, "not found")
        except Exception as e:  # noqa: BLE001 - server must stay up
            traceback.print_exc()
            _err(self, 500, f"{type(e).__name__}: {e}")

    def do_POST(self):  # noqa: N802 - stdlib name
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            seg = [s for s in path.split("/") if s]
            if seg[:2] == ["api", "projects"] and len(seg) == 2:
                self._create_project()
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "papers":
                self._upload_paper(seg[2])
            elif seg[:2] == ["api", "jobs"] and len(seg) == 4 and seg[3] == "retry":
                self._retry_job(seg[2])
            elif seg[:2] == ["api", "jobs"] and len(seg) == 4 and seg[3] == "cancel":
                self._cancel_job(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "question":
                self._change_question(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 4 and seg[3] == "rebuild":
                self._rebuild_continue(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 6 and seg[3] == "runs" \
                    and seg[5] == "review":
                self._submit_review(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "verifications" \
                    and seg[4] == "start":
                self._start_verification(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "audits" \
                    and seg[4] == "start":
                self._start_audit(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "discoveries" \
                    and seg[4] == "start":
                self._start_discovery(seg[2])
            elif seg[:2] == ["api", "projects"] and len(seg) == 6 and seg[3] == "discoveries" \
                    and seg[5] == "decide":
                self._decide_candidates(seg[2], seg[4])
            elif seg[:2] == ["api", "projects"] and len(seg) == 6 and seg[3] == "discoveries" \
                    and seg[5] == "import":
                self._import_candidates(seg[2], seg[4])
            elif seg[0:2] == ["api", "agent"]:
                self._legacy_post(seg[2:])
            else:
                _err(self, 404, "not found")
        except Exception as e:  # noqa: BLE001 - server must stay up
            traceback.print_exc()
            _err(self, 500, f"{type(e).__name__}: {e}")

    def do_PATCH(self):  # noqa: N802 - stdlib name
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            seg = [s for s in path.split("/") if s]
            if seg[:2] == ["api", "projects"] and len(seg) == 3:
                payload, err = _read_json(self)
                if err:
                    _err(self, 400, err)
                    return
                assert payload is not None
                try:
                    project = ws.update_project_meta(
                        PROJECTS_ROOT, seg[2], title=payload.get("title"))
                except FileNotFoundError:
                    _err(self, 404, f"unknown project '{seg[2]}'")
                    return
                except ValueError as e:
                    _err(self, 400, str(e))
                    return
                _ok(self, ws.project_summary(seg[2], project,
                                             ws.resolve_project_dir(PROJECTS_ROOT, seg[2])))
            else:
                _err(self, 404, "not found")
        except Exception as e:  # noqa: BLE001 - server must stay up
            traceback.print_exc()
            _err(self, 500, f"{type(e).__name__}: {e}")

    def do_DELETE(self):  # noqa: N802 - stdlib name
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            seg = [s for s in path.split("/") if s]
            if seg[:2] == ["api", "projects"] and len(seg) == 5 and seg[3] == "papers":
                self._delete_paper(seg[2], seg[4])
            else:
                _err(self, 404, "not found")
        except Exception as e:  # noqa: BLE001 - server must stay up
            traceback.print_exc()
            _err(self, 500, f"{type(e).__name__}: {e}")

    # -- helpers ----------------------------------------------------------

    def _get_or_404(self, project_id: str, fn) -> None:
        try:
            ws.validate_project_id(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        try:
            _ok(self, fn(project_id))
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
        except ValueError as e:
            _err(self, 400, str(e))

    def _get_project(self, project_id: str) -> None:
        try:
            ws.validate_project_id(project_id)
            _ok(self, project_view(project_id))
        except ValueError as e:
            _err(self, 400, str(e))
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")

    def _create_project(self) -> None:
        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            project_id, project_dir, project = ws.create_project(
                PROJECTS_ROOT, str(payload.get("title", "")),
                str(payload.get("research_question", "")))
        except ValueError as e:
            _err(self, 400, str(e))
            return
        _ok(self, {"project_id": project_id,
                   **ws.project_summary(project_id, project, project_dir)},
            status=201)

    def _list_papers(self, project_id: str) -> None:
        def view(pid: str) -> dict:
            _, project = ws.get_project(PROJECTS_ROOT, pid)
            return {"papers": [p.model_dump(mode="json") for p in project.papers],
                    "paper_status": paper_status(pid, project)}
        self._get_or_404(project_id, view)

    def _get_paper(self, project_id: str, paper_id: str) -> None:
        try:
            ws.validate_project_id(project_id)
            _, project = ws.get_project(PROJECTS_ROOT, project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        paper = next((p for p in project.papers if p.paper_id == paper_id), None)
        if paper is None:
            # Still processing (or failed): report the live job state.
            st = paper_status(project_id, project).get(paper_id)
            if st is None:
                _err(self, 404, f"unknown paper '{paper_id}'")
                return
            _ok(self, {"paper": None, "status": st})
            return
        st = paper_status(project_id, project).get(paper_id, {"status": "ready"})
        _ok(self, {"paper": paper.model_dump(mode="json"), "status": st})

    def _delete_paper(self, project_id: str, paper_id: str) -> None:
        """Remove a staged/failed paper's files. Attached papers (which may
        back findings, audits, or verifications) are never deleted here.
        Idempotent: removing an already-removed paper succeeds, so stale
        UI state and double-clicks can't produce a confusing 404."""
        import shutil

        from workspace import paper_dir, resolve_project_dir

        try:
            ws.validate_project_id(project_id)
            project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
            if not (project_dir / "project.json").is_file():
                _err(self, 404, f"unknown project '{project_id}'")
                return
            pdir = paper_dir(project_dir, paper_id)
        except ValueError:
            _err(self, 404, "not found")
            return
        try:
            from project import load_project
            project = load_project(project_dir)
        except Exception as e:
            _err(self, 500, f"project state unreadable: {e}")
            return
        if any(p.paper_id == paper_id for p in project.papers):
            _err(self, 409, f"paper '{paper_id}' is part of the project and "
                            "may back findings or evidence; removal is not "
                            "supported")
            return
        for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=200):
            if j["paper_id"] == paper_id and j["state"] in ("QUEUED", "RUNNING"):
                _err(self, 409, f"paper '{paper_id}' is still processing; "
                                "cancel its job first")
                return
        from agent_graph import list_run_records
        for r in list_run_records(project_dir):
            if r.paper_id == paper_id and r.status == "awaiting_human":
                _err(self, 409, f"paper '{paper_id}' has a pending human "
                                "review; resolve or discard it first")
                return
        if not pdir.is_dir():
            _ok(self, {"removed": paper_id, "already_gone": True})
            return
        try:
            shutil.rmtree(pdir)
        except OSError as e:
            _err(self, 500, f"could not remove staged paper: {e}")
            return
        _ok(self, {"removed": paper_id})

    # -- upload ------------------------------------------------------------

    def _upload_paper(self, project_id: str) -> None:
        from workspace import paper_dir, resolve_project_dir

        try:
            ws.validate_project_id(project_id)
            project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
            if not (project_dir / "project.json").is_file():
                _err(self, 404, f"unknown project '{project_id}'")
                return
        except ValueError as e:
            _err(self, 400, str(e))
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            _err(self, 400, "expected multipart/form-data with a 'file' part")
            return
        try:
            raw = _read_body(self, pipeline_mod.max_pdf_bytes() + 2 * 1024 * 1024)
        except ValueError:
            _err(self, 413, "upload exceeds the size limit")
            return
        try:
            fields, files = parse_multipart(raw, ctype)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        if "file" not in files:
            _err(self, 400, "multipart body has no 'file' part")
            return
        filename, pdf_bytes = files["file"]
        try:
            info = pipeline_mod.validate_pdf(pdf_bytes, filename or "paper.pdf")
        except PipelineError as e:
            msg = str(e)
            _err(self, 413 if "limit is" in msg and "MB" in msg else 400, msg)
            return
        sha256 = info["sha256"]
        requested = (fields.get("paper_id") or "").strip()
        try:
            from project import load_project
            project = load_project(project_dir)
        except Exception as e:
            _err(self, 500, f"project state unreadable: {e}")
            return
        for p in project.papers:
            if p.sha256 == sha256:
                _err(self, 409, f"this PDF was already added as paper '{p.paper_id}'",
                     {"duplicate_of": p.paper_id})
                return
        if requested:
            try:
                paper_id = ws.validate_project_id(requested)
            except ValueError as e:
                _err(self, 400, str(e).replace("project id", "paper id"))
                return
        else:
            paper_id = ws.slugify(Path(info["display_name"]).stem or "paper")
            base = paper_id
            taken = {p.paper_id for p in project.papers}
            i = 2
            while paper_id in taken or (project_dir / "papers" / paper_id).exists():
                paper_id = f"{base}-{i}"
                i += 1
        if any(p.paper_id == paper_id for p in project.papers):
            _err(self, 409, f"paper id '{paper_id}' is already taken")
            return
        try:
            pdir = paper_dir(project_dir, paper_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        # Idempotent re-submit: same paper id + same bytes returns the job.
        src = pdir / "source.pdf"
        if src.is_file():
            try:
                same = hashlib.sha256(src.read_bytes()).hexdigest() == sha256
            except OSError:
                same = False
            if not same:
                _err(self, 409, f"paper id '{paper_id}' is already taken")
                return
        else:
            pdir.mkdir(parents=True, exist_ok=True)
            try:
                src.write_bytes(pdf_bytes)
                (pdir / "original_name.txt").write_text(info["display_name"],
                                                       encoding="utf-8")
            except OSError as e:
                _err(self, 500, f"could not store upload: {e}")
                return
        idem = f"ingest:{paper_id}:{sha256}"
        for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=200):
            if j["job_type"] == "ingest_paper" and j["paper_id"] == paper_id \
                    and (j.get("params") or {}).get("sha256") == sha256 \
                    and j["state"] in ("QUEUED", "RUNNING"):
                _ok(self, {"job": j, "paper_id": paper_id, "deduped": True},
                    status=202)
                return
        job = jobs_mod.create_job(
            PROJECTS_ROOT, project_id, "ingest_paper", paper_id=paper_id,
            idempotency_key=idem, stage="uploading",
            params={"sha256": sha256, "filename": info["display_name"],
                    "size_bytes": info["size_bytes"], "n_pages": info["n_pages"]})
        _ok(self, {"job": job, "paper_id": paper_id}, status=202)

    def _get_paper_file(self, project_id: str, paper_id: str) -> None:
        from workspace import paper_dir, resolve_project_dir

        try:
            ws.validate_project_id(project_id)
            project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
            pdir = paper_dir(project_dir, paper_id)
        except (ValueError, FileNotFoundError):
            _err(self, 404, "not found")
            return
        src = pdir / "source.pdf"
        if src.is_file():
            try:
                _send_bytes(self, 200, src.read_bytes(), "application/pdf")
            except OSError:
                _err(self, 404, "paper file is inaccessible")
            return
        # Legacy CLI papers keep their PDF outside papers/: resolve the stored
        # (server-side only) path — the client never supplies a path.
        try:
            from project import load_project
            project = load_project(project_dir)
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        paper = next((p for p in project.papers if p.paper_id == paper_id), None)
        if paper is None or not paper.pdf_path:
            _err(self, 404, f"unknown paper '{paper_id}'")
            return
        target = (Path(__file__).resolve().parent.parent / paper.pdf_path)
        if not target.is_file():
            _err(self, 404, "paper file is inaccessible")
            return
        try:
            _send_bytes(self, 200, target.read_bytes(), "application/pdf")
        except OSError:
            _err(self, 404, "paper file is inaccessible")

    def _get_paper_data(self, project_id: str, paper_id: str) -> None:
        """Raw localized JSON (Reader-compatible; intentionally unenveloped)."""
        from workspace import paper_dir, resolve_project_dir

        try:
            ws.validate_project_id(project_id)
            project_dir = resolve_project_dir(PROJECTS_ROOT, project_id)
            pdir = paper_dir(project_dir, paper_id)
        except (ValueError, FileNotFoundError):
            _err(self, 404, "not found")
            return
        loc = pdir / "localized.json"
        if loc.is_file():
            try:
                _send_bytes(self, 200, loc.read_bytes(), "application/json")
            except OSError:
                _err(self, 404, "analysis is inaccessible")
            return
        try:
            from project import load_project
            project = load_project(project_dir)
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        paper = next((p for p in project.papers if p.paper_id == paper_id), None)
        if paper is not None and paper.localized_path:
            target = Path(__file__).resolve().parent.parent / paper.localized_path
            if target.is_file():
                try:
                    _send_bytes(self, 200, target.read_bytes(), "application/json")
                    return
                except OSError:
                    pass
        st = paper_status(project_id, project).get(paper_id)
        if st is not None and st.get("status") != "ready":
            _err(self, 409, f"paper '{paper_id}' is {st.get('status')}",
                 {"status": st})
            return
        _err(self, 404, f"unknown paper '{paper_id}'")

    # -- jobs ---------------------------------------------------------------

    def _get_job(self, job_id: str) -> None:
        try:
            _ok(self, {"job": jobs_mod.get_job(PROJECTS_ROOT, job_id)})
        except FileNotFoundError:
            _err(self, 404, f"unknown job '{job_id}'")

    def _retry_job(self, job_id: str) -> None:
        try:
            job = jobs_mod.get_job(PROJECTS_ROOT, job_id)
        except FileNotFoundError:
            _err(self, 404, f"unknown job '{job_id}'")
            return
        if job["job_type"] == "ingest_paper" and job["paper_id"]:
            # Retrying a removed paper would only fail on the missing stage;
            # refuse early with the honest remedy (re-upload).
            try:
                from workspace import paper_dir, resolve_project_dir
                project_dir = resolve_project_dir(PROJECTS_ROOT, job["project_id"])
                staged = paper_dir(project_dir, job["paper_id"]) / "source.pdf"
                if not staged.is_file():
                    _err(self, 409, f"paper '{job['paper_id']}' was removed; "
                                    "re-upload it to try again")
                    return
            except ValueError:
                pass
        try:
            _ok(self, {"job": jobs_mod.retry_job(PROJECTS_ROOT, job_id)})
        except FileNotFoundError:
            _err(self, 404, f"unknown job '{job_id}'")
        except ValueError as e:
            _err(self, 409, str(e))

    def _cancel_job(self, job_id: str) -> None:
        try:
            _ok(self, {"job": jobs_mod.cancel_job(PROJECTS_ROOT, job_id)})
        except FileNotFoundError:
            _err(self, 404, f"unknown job '{job_id}'")
        except ValueError as e:
            _err(self, 409, str(e))

    # -- question ------------------------------------------------------------

    def _change_question(self, project_id: str) -> None:
        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            ws.validate_project_id(project_id)
            ws.get_project(PROJECTS_ROOT, project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        new_question = payload.get("research_question", "")
        if not isinstance(new_question, str) or not new_question.strip():
            _err(self, 400, "research_question is required")
            return
        preview = ws.question_change_preview(PROJECTS_ROOT, project_id)
        if not payload.get("confirm"):
            _ok(self, {"needs_confirmation": True, "preview": preview})
            return
        for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=50):
            if j["job_type"] in ("rescore_question", "rebuild_findings") \
                    and j["state"] in ("QUEUED", "RUNNING"):
                _err(self, 409, "a recomputation is already running",
                     {"job": j})
                return
        try:
            project, _ = ws.apply_question_change(PROJECTS_ROOT, project_id,
                                                  new_question.strip())
        except ValueError as e:
            _err(self, 400, str(e))
            return
        qv = project.question_version or 1
        job = jobs_mod.create_job(
            PROJECTS_ROOT, project_id, "rescore_question",
            idempotency_key=f"rescore:qv{qv}", stage="preparing",
            params={"question": project.research_question,
                    "question_version": qv})
        _ok(self, {"project": project.model_dump(mode="json"),
                   "preview": preview, "job": job}, status=202)

    def _rebuild_continue(self, project_id: str) -> None:
        """Continue an incomplete rebuild (e.g. after resolving the human
        review that paused it). Each call is an explicit researcher action;
        a unique job is created unless a recomputation is already running."""
        import uuid as _uuid

        try:
            ws.validate_project_id(project_id)
            project_dir = ws.resolve_project_dir(PROJECTS_ROOT, project_id)
            if not (project_dir / "project.json").is_file():
                _err(self, 404, f"unknown project '{project_id}'")
                return
        except ValueError as e:
            _err(self, 400, str(e))
            return
        for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=50):
            if j["job_type"] in ("rescore_question", "rebuild_findings") \
                    and j["state"] in ("QUEUED", "RUNNING"):
                _err(self, 409, "a recomputation is already running",
                     {"job": j})
                return
        from project import load_project
        try:
            project = load_project(project_dir)
        except Exception as e:
            _err(self, 500, f"project state unreadable: {e}")
            return
        status = rebuild_status(project_dir, project)
        if not status["needed"]:
            _err(self, 409, "nothing to rebuild: every paper already has "
                            "conclusions for the current question")
            return
        qv = project.question_version or 1
        job = jobs_mod.create_job(
            PROJECTS_ROOT, project_id, "rescore_question",
            idempotency_key=f"rebuild:qv{qv}:{_uuid.uuid4().hex[:8]}",
            stage="preparing",
            params={"question": project.research_question,
                    "question_version": qv})
        _ok(self, {"job": job, "rebuild": status}, status=202)

    # -- agent workflows (project-scoped) -------------------------------------

    def _scoped_dir(self, project_id: str):
        ws.validate_project_id(project_id)
        project_dir = ws.resolve_project_dir(PROJECTS_ROOT, project_id)
        if not (project_dir / "project.json").is_file():
            raise FileNotFoundError(project_id)
        return project_dir

    def _get_runs(self, project_id: str) -> None:
        from agent_graph import list_run_records

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        runs = [
            {"thread_id": r.thread_id, "kind": r.kind, "paper_id": r.paper_id,
             "claim_id": r.claim_id, "status": r.status,
             "updated_at": r.updated_at, "synthetic": r.synthetic}
            for r in list_run_records(project_dir)
        ]
        _ok(self, {"runs": runs})

    def _get_run(self, project_id: str, thread_id: str) -> None:
        from agent_graph import load_run_record

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        try:
            record = load_run_record(project_dir, thread_id)
        except FileNotFoundError:
            _err(self, 404, f"unknown thread {thread_id}")
            return
        _ok(self, record.model_dump(mode="json"))

    def _submit_review(self, project_id: str, thread_id: str) -> None:
        from agent import (resume_audit, resume_investigation,
                           resume_verification)
        from agent_graph import load_run_record as _load

        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        choice = payload.get("choice", "")
        if not isinstance(choice, str) or not choice.strip():
            _err(self, 400, "choice is required")
            return
        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        try:
            kind = _load(project_dir, thread_id).kind or "investigate"
        except FileNotFoundError:
            _err(self, 404, f"unknown thread {thread_id}")
            return
        resume = {"verify": resume_verification, "audit": resume_audit}.get(
            kind, resume_investigation)
        try:
            record = resume(project_dir, thread_id, choice,
                            str(payload.get("free_text", "") or ""))
        except FileNotFoundError:
            _err(self, 404, f"unknown thread {thread_id}")
            return
        except ValueError as e:
            _err(self, 409, str(e))
            return
        _ok(self, record.model_dump(mode="json"))

    def _get_telemetry(self, project_id: str) -> None:
        from agent_graph import read_telemetry, telemetry_summary

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        rows = read_telemetry(project_dir)
        _ok(self, {"summary": telemetry_summary(project_dir),
                   "recent": rows[-20:]})

    def _get_verifications(self, project_id: str) -> None:
        from project import load_project

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        project = load_project(project_dir)
        _ok(self, {"verifications": [v.model_dump(mode="json")
                                     for v in project.verifications]})

    def _get_audits(self, project_id: str) -> None:
        from project import load_project

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        project = load_project(project_dir)
        _ok(self, {"audits": [a.model_dump(mode="json") for a in project.audits]})

    def _serve_verify_file(self, project_id: str, thread_id: str, name: str) -> None:
        from verify_graph import verify_thread_dir

        ALLOWED = {
            "experiment.py": "text/plain; charset=utf-8",
            "results.csv": "text/csv; charset=utf-8",
            "scaling.png": "image/png",
            "stdout.txt": "text/plain; charset=utf-8",
            "stderr.txt": "text/plain; charset=utf-8",
            "evidence.json": "application/json",
        }
        if name not in ALLOWED:
            _err(self, 404, "not found")
            return
        try:
            project_dir = self._scoped_dir(project_id)
        except (ValueError, FileNotFoundError):
            _err(self, 404, "not found")
            return
        # Thread ids are server-generated; still reject path separators.
        if "/" in thread_id or thread_id in ("", ".", ".."):
            _err(self, 404, "not found")
            return
        tdir = verify_thread_dir(project_dir, thread_id)
        if name == "evidence.json":
            target = tdir / name
        else:
            attempts = sorted(tdir.glob("attempt-*")) if tdir.is_dir() else []
            target = attempts[-1] / name if attempts else tdir / name
        if not target.is_file():
            _err(self, 404, "artifact not available yet")
            return
        try:
            _send_bytes(self, 200, target.read_bytes(), ALLOWED[name])
        except OSError:
            _err(self, 404, "artifact not available yet")

    def _baseten_ready(self) -> str | None:
        from dotenv import load_dotenv

        load_dotenv()
        if not os.environ.get("BASETEN_API_KEY"):
            return ("Baseten API key (BASETEN_API_KEY) is not configured; "
                    "agentic workflows need it")
        return None

    def _start_verification(self, project_id: str) -> None:
        from agent import validate_claim_ref
        from verify_graph import new_verify_thread_id

        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        paper_id = payload.get("paper_id", "")
        claim_id = payload.get("claim_id", "")
        if not isinstance(paper_id, str) or not isinstance(claim_id, str):
            _err(self, 400, "paper_id and claim_id must be strings")
            return
        try:
            validate_claim_ref(project_dir, paper_id, claim_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        missing = self._baseten_ready()
        if missing:
            _err(self, 503, missing)
            return
        try:
            from modal_exec import ModalExec, ModalError
            ModalExec().verify_auth()
        except Exception as e:  # noqa: BLE001 - auth probe; report honestly
            _err(self, 503, f"Modal is unavailable: {e}")
            return
        thread_id = new_verify_thread_id(project_dir.resolve().name, paper_id,
                                         claim_id)
        thread = threading.Thread(target=_run_verification,
                                  args=(project_dir, paper_id, claim_id,
                                        thread_id), daemon=True)
        thread.start()
        _ok(self, {"status": "started", "thread_id": thread_id}, status=202)

    def _start_audit(self, project_id: str) -> None:
        from agent import validate_claim_ref
        from audit_graph import new_audit_thread_id

        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        paper_id = payload.get("paper_id", "")
        claim_id = payload.get("claim_id", "")
        external = payload.get("external", False)
        if not isinstance(paper_id, str) or not isinstance(claim_id, str):
            _err(self, 400, "paper_id and claim_id must be strings")
            return
        if not isinstance(external, bool):
            _err(self, 400, "external must be a boolean")
            return
        try:
            validate_claim_ref(project_dir, paper_id, claim_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        missing = self._baseten_ready()
        if missing:
            _err(self, 503, missing)
            return
        thread_id = new_audit_thread_id(project_dir.resolve().name, paper_id,
                                        claim_id)
        thread = threading.Thread(target=_run_audit,
                                  args=(project_dir, paper_id, claim_id,
                                        external, thread_id), daemon=True)
        thread.start()
        _ok(self, {"status": "started", "thread_id": thread_id}, status=202)

    # -- Milestone 8: literature discovery -----------------------------------

    def _list_discoveries(self, project_id: str) -> None:
        from project import load_project

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        project = load_project(project_dir)
        runs = [r.model_dump(mode="json") for r in (project.discoveries or [])]
        active = None
        for j in jobs_mod.list_jobs(PROJECTS_ROOT, project_id, limit=50):
            if j["job_type"] in ("discover_papers", "import_discovery") \
                    and j["state"] in ("QUEUED", "RUNNING"):
                active = j
                break
        _ok(self, {"discoveries": runs, "active_job": active,
                   "question_version": project.question_version or 1})

    def _get_discovery(self, project_id: str, run_id: str) -> None:
        import json as _json

        from agent_graph import load_run_record
        from discovery_graph import discovery_dir
        from project import load_project

        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        project = load_project(project_dir)
        run = next((r for r in (project.discoveries or [])
                    if r.run_id == run_id), None)
        if run is None:
            _err(self, 404, f"unknown discovery run '{run_id}'")
            return
        # Paths are built from the STORED ids (validated above), never the
        # raw URL segment, so no traversal is possible here.
        log = None
        log_path = discovery_dir(project_dir) / f"{run.run_id}.json"
        if log_path.is_file():
            try:
                log = _json.loads(log_path.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                log = None
        record = None
        try:
            rec = load_run_record(project_dir, run.thread_id)
            record = {"status": rec.status, "decisions": rec.decisions,
                      "telemetry": rec.telemetry}
        except FileNotFoundError:
            record = None
        _ok(self, {"run": run.model_dump(mode="json"),
                   "retrieval_log": log, "record": record})

    def _start_discovery(self, project_id: str) -> None:
        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        focus = payload.get("focus", "")
        if not isinstance(focus, str):
            _err(self, 400, "focus must be a string")
            return
        focus = focus.strip()
        if len(focus) > MAX_DISCOVERY_FOCUS:
            _err(self, 400, f"focus exceeds {MAX_DISCOVERY_FOCUS} characters")
            return
        try:
            budget = validated_discovery_budget(payload.get("budget"))
        except ValueError as e:
            _err(self, 400, str(e))
            return
        request_key = payload.get("request_key", "")
        if request_key:
            if not isinstance(request_key, str) \
                    or not REQUEST_KEY_RE.match(request_key):
                _err(self, 400, "request_key must match [A-Za-z0-9-]{1,64}")
                return
            idem = f"discover:{request_key}"
        else:
            idem = ""
        job = jobs_mod.create_job(PROJECTS_ROOT, project_id, "discover_papers",
                                  idempotency_key=idem, stage="searching",
                                  params={"focus": focus, "budget": budget})
        _ok(self, {"job": job}, status=202)

    def _decide_candidates(self, project_id: str, run_id: str) -> None:
        from discovery_graph import update_candidate_status
        from project import load_project

        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        decisions = payload.get("decisions")
        if not isinstance(decisions, list) or not decisions \
                or len(decisions) > 50:
            _err(self, 400, "decisions must be a non-empty list (<=50)")
            return
        project = load_project(project_dir)
        if not any(r.run_id == run_id for r in (project.discoveries or [])):
            _err(self, 404, f"unknown discovery run '{run_id}'")
            return
        items = []
        for entry in decisions:
            item: dict = {"ok": False}
            if not isinstance(entry, dict):
                item["error"] = "decision must be an object"
                items.append(item)
                continue
            cid = entry.get("candidate_id", "")
            action = entry.get("action", "")
            reason = entry.get("reason", "")
            item["candidate_id"] = cid
            if action == "approve":
                status = "approved"
            elif action == "dismiss":
                status = "dismissed"
            else:
                item["error"] = "action must be 'approve' or 'dismiss'"
                items.append(item)
                continue
            if not isinstance(cid, str) or not cid:
                item["error"] = "candidate_id must be a non-empty string"
                items.append(item)
                continue
            if not isinstance(reason, str):
                item["error"] = "reason must be a string"
                items.append(item)
                continue
            try:
                cand = update_candidate_status(project_dir, run_id, cid,
                                               status, reason=reason)
            except FileNotFoundError:
                item["error"] = f"unknown candidate '{cid}'"
            except ValueError as e:
                item["error"] = str(e)
            else:
                item["ok"] = True
                item["status"] = cand.status
            items.append(item)
        _ok(self, {"run_id": run_id, "items": items})

    def _import_candidates(self, project_id: str, run_id: str) -> None:
        from project import load_project

        payload, err = _read_json(self)
        if err:
            _err(self, 400, err)
            return
        assert payload is not None
        try:
            project_dir = self._scoped_dir(project_id)
        except ValueError as e:
            _err(self, 400, str(e))
            return
        except FileNotFoundError:
            _err(self, 404, f"unknown project '{project_id}'")
            return
        raw_ids = payload.get("candidate_ids")
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 8:
            _err(self, 400, "candidate_ids must be a non-empty list (<=8)")
            return
        if any(not isinstance(c, str) or not c for c in raw_ids):
            _err(self, 400, "candidate_ids must be non-empty strings")
            return
        candidate_ids = list(dict.fromkeys(raw_ids))
        project = load_project(project_dir)
        run = next((r for r in (project.discoveries or [])
                    if r.run_id == run_id), None)
        if run is None:
            _err(self, 404, f"unknown discovery run '{run_id}'")
            return
        known = {c.candidate_id: c for c in run.candidates}
        unknown = [c for c in candidate_ids if c not in known]
        if unknown:
            _err(self, 400, f"unknown candidate(s): {', '.join(unknown)}")
            return
        dismissed = [c for c in candidate_ids
                     if known[c].status == "dismissed"]
        if dismissed:
            _err(self, 400, f"dismissed candidate(s) cannot be imported "
                            f"(approve first): {', '.join(dismissed)}")
            return
        job = jobs_mod.create_job(PROJECTS_ROOT, project_id, "import_discovery",
                                  stage="preparing",
                                  params={"run_id": run_id,
                                          "candidate_ids": candidate_ids})
        _ok(self, {"job": job}, status=202)

    # -- legacy M5/M6/M6.1 routes (exact shapes, default project) -------------

    def _legacy_project_dir(self):
        try:
            project_dir = ws.resolve_project_dir(PROJECTS_ROOT,
                                                 ws.DEFAULT_PROJECT_ID)
        except ValueError:
            return None
        if not (project_dir / "project.json").is_file():
            return None
        return project_dir

    def _legacy_get(self, seg: list[str]) -> None:
        from agent_graph import (list_run_records, load_run_record,
                                 read_telemetry, telemetry_summary)

        project_dir = self._legacy_project_dir()
        if project_dir is None:
            _send(self, 404, {"error": "no project"})
            return
        if seg == ["runs"]:
            runs = [
                {"thread_id": r.thread_id, "kind": r.kind,
                 "paper_id": r.paper_id, "claim_id": r.claim_id,
                 "status": r.status, "updated_at": r.updated_at,
                 "synthetic": r.synthetic}
                for r in list_run_records(project_dir)
            ]
            _send(self, 200, {"runs": runs})
        elif seg == ["verifications"]:
            from project import load_project
            project = load_project(project_dir)
            _send(self, 200, {"verifications": [v.model_dump(mode="json")
                                                for v in project.verifications]})
        elif seg == ["audits"]:
            from project import load_project
            project = load_project(project_dir)
            _send(self, 200, {"audits": [a.model_dump(mode="json")
                                         for a in project.audits]})
        elif len(seg) == 4 and seg[0] == "verifications" and seg[2] == "files":
            self._serve_verify_file(ws.DEFAULT_PROJECT_ID, seg[1], seg[3])
        elif len(seg) == 2 and seg[0] == "runs":
            try:
                record = load_run_record(project_dir, seg[1])
            except FileNotFoundError:
                _send(self, 404, {"error": f"unknown thread {seg[1]}"})
                return
            _send(self, 200, record.model_dump(mode="json"))
        elif seg == ["telemetry"]:
            rows = read_telemetry(project_dir)
            _send(self, 200, {"summary": telemetry_summary(project_dir),
                              "recent": rows[-20:]})
        else:
            _send(self, 404, {"error": "not found"})

    def _legacy_post(self, seg: list[str]) -> None:
        from agent import (resume_audit, resume_investigation,
                           resume_verification)
        from agent_graph import load_run_record as _load_record

        project_dir = self._legacy_project_dir()
        if project_dir is None:
            _send(self, 404, {"error": "no project"})
            return
        try:
            raw = _read_body(self, MAX_JSON_BODY)
        except ValueError:
            _send(self, 413, {"error": "body too large"})
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            _send(self, 400, {"error": "invalid JSON body"})
            return
        if len(seg) == 3 and seg[0] == "runs" and seg[2] == "review":
            thread_id = seg[1]
            choice = payload.get("choice", "")
            if not isinstance(choice, str) or not choice.strip():
                _send(self, 400, {"error": "choice is required"})
                return
            try:
                kind = _load_record(project_dir, thread_id).kind or "investigate"
            except FileNotFoundError:
                _send(self, 404, {"error": f"unknown thread {thread_id}"})
                return
            resume = {"verify": resume_verification,
                      "audit": resume_audit}.get(kind, resume_investigation)
            try:
                record = resume(project_dir, thread_id, choice,
                                str(payload.get("free_text", "") or ""))
            except FileNotFoundError:
                _send(self, 404, {"error": f"unknown thread {thread_id}"})
                return
            except ValueError as e:
                _send(self, 409, {"error": str(e)})
                return
            _send(self, 200, record.model_dump(mode="json"))
        elif seg == ["investigate"]:
            for key in ("paper_id", "pdf", "analysis", "localized"):
                if not payload.get(key):
                    _send(self, 400, {"error": f"{key} is required"})
                    return
            thread = threading.Thread(target=_run_investigation,
                                      args=(project_dir, dict(payload),),
                                      daemon=True)
            thread.start()
            _send(self, 202, {"status": "started"})
        elif seg == ["verifications", "start"]:
            self._legacy_start(project_dir, payload, kind="verify")
        elif seg == ["audits", "start"]:
            self._legacy_start(project_dir, payload, kind="audit")
        else:
            _send(self, 404, {"error": "not found"})

    def _legacy_start(self, project_dir: Path, payload: dict, kind: str) -> None:
        from agent import validate_claim_ref

        paper_id = payload.get("paper_id", "")
        claim_id = payload.get("claim_id", "")
        if not isinstance(paper_id, str) or not isinstance(claim_id, str):
            _send(self, 400, {"error": "paper_id and claim_id must be strings"})
            return
        try:
            validate_claim_ref(project_dir, paper_id, claim_id)
        except ValueError as e:
            _send(self, 400, {"error": str(e)})
            return
        if kind == "verify":
            from verify_graph import new_verify_thread_id
            missing = self._baseten_ready()
            if missing:
                _send(self, 503, {"error": missing})
                return
            try:
                from modal_exec import ModalExec
                ModalExec().verify_auth()
            except Exception as e:  # noqa: BLE001 - auth probe
                _send(self, 503, {"error": f"Modal is unavailable: {e}"})
                return
            thread_id = new_verify_thread_id(project_dir.resolve().name,
                                             paper_id, claim_id)
            thread = threading.Thread(target=_run_verification,
                                      args=(project_dir, paper_id, claim_id,
                                            thread_id), daemon=True)
        else:
            from audit_graph import new_audit_thread_id
            external = payload.get("external", False)
            if not isinstance(external, bool):
                _send(self, 400, {"error": "external must be a boolean"})
                return
            missing = self._baseten_ready()
            if missing:
                _send(self, 503, {"error": missing})
                return
            thread_id = new_audit_thread_id(project_dir.resolve().name,
                                            paper_id, claim_id)
            thread = threading.Thread(target=_run_audit,
                                      args=(project_dir, paper_id, claim_id,
                                            external, thread_id), daemon=True)
        thread.start()
        _send(self, 202, {"status": "started", "thread_id": thread_id})


def _run_investigation(project_dir: Path, payload: dict) -> None:
    import argparse as _ap

    from agent import cmd_investigate

    args = _ap.Namespace(
        project_dir=str(project_dir),
        paper_id=payload["paper_id"],
        pdf=payload["pdf"],
        analysis=payload["analysis"],
        localized=payload["localized"],
        thread_id=None,
    )
    try:
        cmd_investigate(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def _run_verification(project_dir: Path, paper_id: str, claim_id: str,
                      thread_id: str) -> None:
    import argparse as _ap

    from agent import cmd_verify_claim

    args = _ap.Namespace(
        project_dir=str(project_dir), paper_id=paper_id, claim_id=claim_id,
        thread_id=thread_id,
    )
    try:
        cmd_verify_claim(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def _run_audit(project_dir: Path, paper_id: str, claim_id: str, external: bool,
               thread_id: str) -> None:
    import argparse as _ap

    from agent import cmd_audit_claim

    args = _ap.Namespace(
        project_dir=str(project_dir), paper_id=paper_id, claim_id=claim_id,
        thread_id=thread_id, external=external,
    )
    try:
        cmd_audit_claim(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def serve(projects_root: Path, port: int,
          run_dispatcher: bool = True) -> ThreadingHTTPServer:
    global PROJECTS_ROOT
    PROJECTS_ROOT = projects_root
    recovery = jobs_mod.reconcile_after_restart(projects_root)
    if recovery["requeued"] or recovery["failed"]:
        print(f"[workspace-server] restart recovery: {recovery}",
              file=sys.stderr)
    if run_dispatcher:
        start_dispatcher()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[workspace-server] serving {projects_root} on "
          f"http://127.0.0.1:{port}", file=sys.stderr)
    return server


def main() -> int:
    p = argparse.ArgumentParser(description="ResearchOS Milestone 7 workspace server.")
    p.add_argument("--projects-root", default=str(ws.default_projects_root()))
    p.add_argument("--port", default=str(DEFAULT_PORT))
    p.add_argument("--no-dispatcher", action="store_true",
                   help="Serve the API without the background job dispatcher.")
    ns = p.parse_args()
    server = serve(Path(ns.projects_root), int(ns.port),
                   run_dispatcher=not ns.no_dispatcher)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_dispatcher()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
