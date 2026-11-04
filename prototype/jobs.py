#!/usr/bin/env python3
"""ResearchOS Milestone 7: durable local job mechanism.

PDF analysis takes minutes, so the browser never waits on one long HTTP
request. Jobs persist in SQLite (prototype/projects/workspace.sqlite):

- states: QUEUED / RUNNING / COMPLETED / FAILED / CANCELLED
- meaningful pipeline stages tracked separately from coarse state
- bounded retries, idempotency keys, last error, run/thread refs

Restart semantics: COMPLETED stays completed; RUNNING jobs are reconciled to
QUEUED (attempts incremented) unless retries are exhausted; QUEUED jobs are
picked up by the dispatcher. Completed expensive stages are reused via
per-paper stage files (see paper_pipeline.py), never re-run blindly.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

STATES = ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")
TERMINAL_STATES = ("COMPLETED", "FAILED", "CANCELLED")

JOB_TYPES = ("ingest_paper", "rescore_question", "rebuild_findings")

# Researcher-facing stage labels for the ingest pipeline (real backend
# stages, never time-derived percentages).
INGEST_STAGES = (
    "uploading",
    "validating",
    "reading_document",
    "identifying_objects",
    "analyzing_content",
    "ranking_relevance",
    "localizing",
    "comparing_with_project",
    "finalizing",
)

RESCORE_STAGES = ("preparing", "ranking_relevance", "rebuilding_findings", "finalizing")

MAX_ATTEMPTS = 3

_db_guard = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def workspace_db_path(projects_root: Path) -> Path:
    return projects_root / "workspace.sqlite"


def connect(projects_root: Path) -> sqlite3.Connection:
    projects_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(workspace_db_path(projects_root)), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            paper_id TEXT NOT NULL DEFAULT '',
            job_type TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'QUEUED',
            stage TEXT NOT NULL DEFAULT '',
            stage_detail TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            last_error TEXT NOT NULL DEFAULT '',
            run_ids TEXT NOT NULL DEFAULT '[]',
            params TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, updated_at)"
    )
    # Partial index: only non-empty idempotency keys deduplicate. Jobs
    # without a key ("" default) must never collide with each other.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem "
        "ON jobs(project_id, idempotency_key) WHERE idempotency_key != ''"
    )
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("run_ids", "params", "result"):
        try:
            raw = d.get(key)
            d[key] = json.loads(raw or ("[]" if key == "run_ids" else "{}"))
        except (json.JSONDecodeError, TypeError):
            d[key] = [] if key == "run_ids" else {}
    return d


def create_job(
    projects_root: Path,
    project_id: str,
    job_type: str,
    paper_id: str = "",
    idempotency_key: str = "",
    stage: str = "",
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a job; on idempotency-key conflict return the existing job."""
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type '{job_type}'")
    now = utc_now_iso()
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    with _db_guard:
        conn = connect(projects_root)
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE project_id = ? AND idempotency_key = ?",
                    (project_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    return _row_to_dict(row)
            try:
                try:
                    conn.execute(
                        """INSERT INTO jobs (job_id, project_id, paper_id, job_type, state,
                           stage, idempotency_key, attempts, max_attempts, params,
                           created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, 0, ?, ?, ?, ?)""",
                        (job_id, project_id, paper_id, job_type, stage, idempotency_key,
                         MAX_ATTEMPTS, json.dumps(params or {}), now, now),
                    )
                except sqlite3.OperationalError:
                    # Pre-params database (dev only): fall back to the old shape.
                    conn.execute(
                        """INSERT INTO jobs (job_id, project_id, paper_id, job_type, state,
                           stage, idempotency_key, attempts, max_attempts,
                           created_at, updated_at)
                           VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, 0, ?, ?, ?)""",
                        (job_id, project_id, paper_id, job_type, stage, idempotency_key,
                         MAX_ATTEMPTS, now, now),
                    )
                conn.commit()
            except sqlite3.IntegrityError:
                # Lost a concurrent insert race on the idempotency key:
                # return the winner's job instead of failing.
                conn.rollback()
                row = conn.execute(
                    "SELECT * FROM jobs WHERE project_id = ? AND idempotency_key = ?",
                    (project_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    return _row_to_dict(row)
                raise
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return _row_to_dict(row)
        finally:
            conn.close()


def get_job(projects_root: Path, job_id: str) -> dict[str, Any]:
    with _db_guard:
        conn = connect(projects_root)
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        raise FileNotFoundError(f"unknown job '{job_id}'")
    return _row_to_dict(row)


def list_jobs(
    projects_root: Path, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    with _db_guard:
        conn = connect(projects_root)
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE project_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (project_id, max(1, min(limit, 200))),
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]


def update_job(projects_root: Path, job_id: str, **fields: Any) -> dict[str, Any]:
    allowed = {
        "state", "stage", "stage_detail", "last_error", "attempts",
        "run_ids", "result",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown job fields: {sorted(unknown)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"unknown job state '{fields['state']}'")
    payload: dict[str, Any] = dict(fields)
    for key in ("run_ids", "result"):
        if key in payload and not isinstance(payload[key], str):
            payload[key] = json.dumps(payload[key])
    payload["updated_at"] = utc_now_iso()
    sets = ", ".join(f"{k} = ?" for k in payload)
    with _db_guard:
        conn = connect(projects_root)
        try:
            cur = conn.execute(
                f"UPDATE jobs SET {sets} WHERE job_id = ?",
                (*payload.values(), job_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise FileNotFoundError(f"unknown job '{job_id}'")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return _row_to_dict(row)
        finally:
            conn.close()


def claim_next_queued(projects_root: Path) -> Optional[dict[str, Any]]:
    """Atomically move the oldest QUEUED job to RUNNING (single dispatcher)."""
    with _db_guard:
        conn = connect(projects_root)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE state = 'QUEUED' "
                    "ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return None
                now = utc_now_iso()
                conn.execute(
                    "UPDATE jobs SET state = 'RUNNING', attempts = attempts + 1, "
                    "updated_at = ? WHERE job_id = ? AND state = 'QUEUED'",
                    (now, row["job_id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            assert row is not None
            return _row_to_dict(row)
        finally:
            conn.close()


def retry_job(projects_root: Path, job_id: str) -> dict[str, Any]:
    job = get_job(projects_root, job_id)
    if job["state"] not in ("FAILED", "CANCELLED"):
        raise ValueError(f"job {job_id} is '{job['state']}'; only failed/cancelled jobs retry")
    if job["attempts"] >= job["max_attempts"]:
        raise ValueError(f"job {job_id} exhausted its {job['max_attempts']} attempts")
    return update_job(projects_root, job_id, state="QUEUED", last_error="")


def cancel_job(projects_root: Path, job_id: str) -> dict[str, Any]:
    job = get_job(projects_root, job_id)
    if job["state"] in TERMINAL_STATES:
        raise ValueError(f"job {job_id} is already '{job['state']}'")
    # RUNNING jobs observe the CANCELLED flag between stages and stop.
    return update_job(projects_root, job_id, state="CANCELLED")


def reconcile_after_restart(projects_root: Path) -> dict[str, int]:
    """Crash/restart recovery: RUNNING -> QUEUED (or FAILED if exhausted)."""
    requeued = 0
    failed = 0
    with _db_guard:
        conn = connect(projects_root)
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'RUNNING'"
            ).fetchall()
            now = utc_now_iso()
            for row in rows:
                if row["attempts"] >= row["max_attempts"]:
                    conn.execute(
                        "UPDATE jobs SET state = 'FAILED', last_error = ?, "
                        "updated_at = ? WHERE job_id = ?",
                        ("interrupted by process restart; retry budget exhausted",
                         now, row["job_id"]),
                    )
                    failed += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET state = 'QUEUED', "
                        "last_error = ?, updated_at = ? WHERE job_id = ?",
                        ("interrupted by process restart; will resume", now, row["job_id"]),
                    )
                    requeued += 1
            conn.commit()
        finally:
            conn.close()
    return {"requeued": requeued, "failed": failed}
