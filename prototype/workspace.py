#!/usr/bin/env python3
"""ResearchOS Milestone 7: multi-project workspace registry.

The agent server used to be bound to ONE project directory. This module makes
the backend manage MULTIPLE research projects under one workspace root
(default: prototype/projects/):

    prototype/projects/
        ot-correspondence/      # existing M4-M6.1 demo project (untouched)
        transport-geometry/     # created via API by a new researcher
        ...

Each project keeps the file-backed scientific state it already had
(project.json + agent/ checkpoints, runs, telemetry); this module adds only
the application layer around it:

- stable unique project ids (URL-safe slugs, server-generated)
- title + research question + timestamps + question versioning
- safe path resolution (no traversal, no symlink escape)
- per-project write locks (single-writer per project)
- question-change semantics: snapshot old conclusions into versioned history,
  never silently display them as current

Project scientific state stays file-backed; job metadata lives in SQLite
(see jobs.py). No new infrastructure.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project import Project, QuestionVersionRecord, load_project, save_project

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_TITLE_LEN = 200
MAX_QUESTION_LEN = 2000

DEFAULT_PROJECT_ID = "ot-correspondence"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_projects_root() -> Path:
    return Path(__file__).resolve().parent / "projects"


def project_lock(project_id: str) -> threading.Lock:
    """Per-project single-writer lock (process-local; server is one process)."""
    with _locks_guard:
        lock = _locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _locks[project_id] = lock
        return lock


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ValueError(f"invalid project id '{project_id}'")
    return project_id


def resolve_project_dir(projects_root: Path, project_id: str) -> Path:
    """Resolve a project dir safely: rejects traversal and symlink escape."""
    validate_project_id(project_id)
    root = projects_root.resolve()
    candidate = (root / project_id).resolve()
    if candidate != root / project_id or root not in candidate.parents:
        raise ValueError(f"invalid project id '{project_id}'")
    return candidate


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:48] or "project"


def generate_project_id(projects_root: Path, title: str) -> str:
    base = slugify(title)
    candidate = base
    for _ in range(100):
        if not (projects_root / candidate).exists():
            return validate_project_id(candidate)
        candidate = f"{base}-{uuid.uuid4().hex[:6]}"
    raise RuntimeError("could not allocate a project id")


def list_projects(projects_root: Path) -> list[dict]:
    """Summaries for every directory holding a valid project.json."""
    out: list[dict] = []
    if not projects_root.is_dir():
        return out
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not PROJECT_ID_RE.match(child.name):
            continue
        try:
            project = load_project(child)
        except Exception:
            continue  # not a project (or corrupt) — never crash the list
        out.append(project_summary(child.name, project, child))
    return out


def project_summary(project_id: str, project: Project, project_dir: Path) -> dict:
    return {
        "project_id": project_id,
        "title": project.title or project_id,
        "research_question": project.research_question,
        "question_version": project.question_version or 1,
        "n_papers": len(project.papers),
        "n_findings": len(project.findings),
        "n_connections": len(project.connections),
        "n_verifications": len(project.verifications),
        "n_audits": len(project.audits),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def get_project(projects_root: Path, project_id: str) -> tuple[Path, Project]:
    project_dir = resolve_project_dir(projects_root, project_id)
    if not (project_dir / "project.json").is_file():
        raise FileNotFoundError(f"unknown project '{project_id}'")
    return project_dir, load_project(project_dir)


def create_project(
    projects_root: Path,
    title: str,
    research_question: str,
    project_id: Optional[str] = None,
) -> tuple[str, Path, Project]:
    """Create a persistent empty project. Never requires an initial PDF."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("project title is required")
    if not isinstance(research_question, str) or not research_question.strip():
        raise ValueError("research question is required")
    if len(title.strip()) > MAX_TITLE_LEN:
        raise ValueError(f"title exceeds {MAX_TITLE_LEN} characters")
    if len(research_question.strip()) > MAX_QUESTION_LEN:
        raise ValueError(f"research question exceeds {MAX_QUESTION_LEN} characters")
    projects_root.mkdir(parents=True, exist_ok=True)
    if project_id is not None:
        validate_project_id(project_id)
        project_dir = resolve_project_dir(projects_root, project_id)
        if (project_dir / "project.json").exists():
            raise ValueError(f"project '{project_id}' already exists")
    else:
        project_id = generate_project_id(projects_root, title.strip())
        project_dir = resolve_project_dir(projects_root, project_id)
    now = utc_now_iso()
    project = Project(
        research_question=research_question.strip(),
        title=title.strip(),
        question_version=1,
        created_at=now,
        updated_at=now,
        question_history=[
            QuestionVersionRecord(
                version=1,
                question=research_question.strip(),
                created_at=now,
                status="current",
            )
        ],
    )
    with project_lock(project_id):
        save_project(project_dir, project)
    return project_id, project_dir, project


def update_project_meta(
    projects_root: Path,
    project_id: str,
    title: Optional[str] = None,
) -> Project:
    """Edit the project title (metadata-only; no recomputation needed)."""
    project_dir = resolve_project_dir(projects_root, project_id)
    with project_lock(project_id):
        project = load_project(project_dir)
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                raise ValueError("project title must be non-empty")
            if len(title.strip()) > MAX_TITLE_LEN:
                raise ValueError(f"title exceeds {MAX_TITLE_LEN} characters")
            project.title = title.strip()
        project.updated_at = utc_now_iso()
        save_project(project_dir, project)
        return project


def question_change_preview(projects_root: Path, project_id: str) -> dict:
    """Describe the expected work of a question change (shown pre-confirm)."""
    _, project = get_project(projects_root, project_id)
    stale = [p.paper_id for p in project.papers]
    return {
        "project_id": project_id,
        "from_version": project.question_version or 1,
        "to_version": (project.question_version or 1) + 1,
        "papers_to_rescore": stale,
        "n_papers": len(stale),
        "conclusions_to_archive": {
            "findings": len(project.findings),
            "connections": len(project.connections),
            "open_questions": len(project.open_questions),
            "novelty_risks": len(project.novelty_risks),
        },
        "preserved": [
            "original PDF files",
            "paper-native inventory and explanations",
            "localization caches",
            "prior audit reports and verification results",
            "agent run history",
        ],
        "recomputed": [
            "per-object question relevance (cheap text scoring)",
            "project findings, connections, open questions, novelty risks",
        ],
    }


def apply_question_change(
    projects_root: Path, project_id: str, new_question: str
) -> tuple[Project, QuestionVersionRecord]:
    """Supersede the current question.

    Archives the current question-dependent conclusions into a versioned
    history record and clears the live arrays so stale conclusions can never
    be displayed as current. Paper-native state (PDFs, inventory, stored
    analyses, localizations) is untouched. Audits, verifications, and agent
    runs are preserved with their originating versions.
    """
    if not isinstance(new_question, str) or not new_question.strip():
        raise ValueError("research question must be non-empty")
    if len(new_question.strip()) > MAX_QUESTION_LEN:
        raise ValueError(f"research question exceeds {MAX_QUESTION_LEN} characters")
    project_dir = resolve_project_dir(projects_root, project_id)
    with project_lock(project_id):
        project = load_project(project_dir)
        new_question = new_question.strip()
        if new_question == project.research_question:
            raise ValueError("new question is identical to the current question")
        old_version = project.question_version or 1
        now = utc_now_iso()
        archived = QuestionVersionRecord(
            version=old_version,
            question=project.research_question,
            created_at=now,
            status="superseded",
            findings=[f.model_copy(update={"status": "historical"}) for f in project.findings],
            connections=[c.model_copy(update={"status": "historical"}) for c in project.connections],
            open_questions=[q.model_copy(update={"status": "historical"}) for q in project.open_questions],
            novelty_risks=[r.model_copy(update={"status": "historical"}) for r in project.novelty_risks],
        )
        for record in project.question_history:
            record.status = "superseded"
        # Exactly one record per version: reuse the existing record for the
        # old version when creation already made one (avoids duplicate keys).
        existing = next((r for r in project.question_history
                         if r.version == old_version), None)
        if existing is not None:
            existing.question = archived.question
            existing.created_at = archived.created_at or existing.created_at
            existing.status = "superseded"
            existing.findings = archived.findings
            existing.connections = archived.connections
            existing.open_questions = archived.open_questions
            existing.novelty_risks = archived.novelty_risks
        else:
            project.question_history.append(archived)
        project.question_history.append(
            QuestionVersionRecord(
                version=old_version + 1,
                question=new_question,
                created_at=now,
                status="current",
            )
        )
        project.research_question = new_question
        project.question_version = old_version + 1
        project.findings = []
        project.connections = []
        project.contributions = []
        project.open_questions = []
        project.novelty_risks = []
        project.updated_at = now
        save_project(project_dir, project)
        return project, archived


def papers_dir(project_dir: Path) -> Path:
    return project_dir / "papers"


def paper_dir(project_dir: Path, paper_id: str) -> Path:
    """Server-generated paper storage; paper ids are validated slugs."""
    from project import PaperEntry  # noqa: F401 - validates the shared pattern

    if not isinstance(paper_id, str) or not PROJECT_ID_RE.match(paper_id):
        raise ValueError(f"invalid paper id '{paper_id}'")
    root = project_dir.resolve()
    candidate = (root / "papers" / paper_id).resolve()
    # Allow the dir to not exist yet: compare lexically after resolving root.
    expected_parent = (root / "papers")
    if candidate.parent != expected_parent and candidate.parent != expected_parent.resolve():
        raise ValueError(f"invalid paper id '{paper_id}'")
    return project_dir / "papers" / paper_id


def repo_relative(path: Path) -> str:
    """Repo-relative posix path for PaperEntry/RunRecord (repo-root aware)."""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return os.environ.get("RESEARCHOS_ABS_PATHS", "") and str(path) or str(path)
