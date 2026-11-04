#!/usr/bin/env python3
"""ResearchOS Milestone 7 tests: workspace, jobs, pipeline, and server.

Deterministic fakes throughout (no API keys, no network): fake model clients
for the pipeline, an ephemeral projects root, and a live HTTP server on an
ephemeral port. One integration test runs the REAL sample EOT PDF bytes
through validate/inventory/cache-reuse with fake model calls only where a
model would be consulted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROTO = Path(__file__).resolve().parent
REPO = PROTO.parent

import jobs as jobs_mod  # noqa: E402
import paper_pipeline as pipe_mod  # noqa: E402
import workspace as ws_mod  # noqa: E402
import workspace_server as srv_mod  # noqa: E402
from analyze_paper import (  # noqa: E402
    AnalysisDraft,
    AnalysisResult,
    RelevanceList,
    RelevanceScore,
    SciObjectDraft,
    draft_from_result,
    merge_relevance,
)
from jobs import MAX_ATTEMPTS  # noqa: E402
from localize_objects import (  # noqa: E402
    BoundingBox,
    LocalizedOutput,
    ObjectLocalization,
)
from paper_pipeline import (  # noqa: E402
    DuplicatePDFError,
    PipelineDeps,
    PipelineError,
)
from project import (  # noqa: E402
    ContributionBody,
    Finding,
    NewConnection,
    NewFinding,
    NewNoveltyRisk,
    NewOpenQuestion,
    PaperEntry,
    Project,
    ProjectDelta,
    VerificationEvidence,
    apply_delta,
    load_project,
    save_project,
)

QUESTION = "Can alternative transport costs improve non-rigid shape matching?"
QUESTION_2 = "How do efficiency and stability differ between sliced and entropic OT?"


def make_draft(n_extra: int = 0) -> AnalysisDraft:
    objs = [
        SciObjectDraft(id="eq-1", type="equation", page=1, label="Equation 1",
                       title="Transport cost", what_it_is="cost", why_it_is_here="method",
                       claim_supported="c", limitations="none", paper_importance=0.9,
                       paper_importance_reason="core", confidence=0.9, evidence="p.1"),
        SciObjectDraft(id="fig-1", type="figure", page=1, label="Figure 1",
                       title="Overview", what_it_is="fig", why_it_is_here="arch",
                       claim_supported="c", limitations="none", paper_importance=0.5,
                       paper_importance_reason="context", confidence=0.9, evidence="p.1"),
        SciObjectDraft(id="table-1", type="table", page=2, label="Table 1",
                       title="Results", what_it_is="tab", why_it_is_here="eval",
                       claim_supported="c", limitations="none", paper_importance=0.8,
                       paper_importance_reason="eval", confidence=0.9, evidence="p.2"),
        SciObjectDraft(id="claim-1", type="claim", page=2, label=None,
                       title="Main claim", what_it_is="claim", why_it_is_here="result",
                       claim_supported="c", limitations="none", paper_importance=0.9,
                       paper_importance_reason="core", confidence=0.8, evidence="p.2"),
    ]
    for i in range(n_extra):
        objs.append(SciObjectDraft(
            id=f"eq-x{i}", type="equation", page=1, label=f"Equation {10 + i}",
            title="Extra", what_it_is="x", why_it_is_here="x", claim_supported="c",
            limitations="none", paper_importance=0.2,
            paper_importance_reason="bg", confidence=0.9, evidence="p.1"))
    return AnalysisDraft(paper_title="Fake Paper", objects=objs)


def fake_score(question: str, draft: AnalysisDraft) -> RelevanceList:
    scores = []
    for i, o in enumerate(draft.objects):
        s = 0.9 if i == 0 else (0.5 if i % 2 else 0.2)
        scores.append(RelevanceScore(id=o.id, research_relevance="rel",
                                     research_relevance_score=s,
                                     research_relevance_reason="fake"))
    return RelevanceList(scores=scores)


def make_deps(calls: dict | None = None, investigate=None) -> PipelineDeps:
    calls = calls if calls is not None else {}

    def analyze(pdf_bytes: bytes, filename: str, checklist: str) -> AnalysisDraft:
        calls["analyze"] = calls.get("analyze", 0) + 1
        return make_draft()

    def score(question: str, draft: AnalysisDraft) -> RelevanceList:
        calls["score"] = calls.get("score", 0) + 1
        return fake_score(question, draft)

    def localize(pdf_path: Path, analysis: AnalysisResult, cache: Path):
        calls["localize"] = calls.get("localize", 0) + 1
        locs = [
            ObjectLocalization(object_id=o.id, page=o.page or 1,
                               bbox=BoundingBox(x_min=10, y_min=10, x_max=90, y_max=90),
                               confidence=0.9, localization_method="pdf_native")
            for o in analysis.objects if o.type in ("equation", "figure", "table") and o.page
        ]
        return LocalizedOutput(analysis=analysis, localizations=locs), {
            "targets": len(locs), "cache_hits": 0, "found": len(locs)}

    def invest(project_dir, paper_id, pdf, analysis, localized,
               sha256="", question_version=1):
        calls["investigate"] = calls.get("investigate", 0) + 1
        if investigate is not None:
            return investigate(project_dir, paper_id, pdf, analysis, localized,
                               sha256=sha256, question_version=question_version)
        return fake_investigate(project_dir, paper_id, pdf, analysis, localized,
                                sha256=sha256, question_version=question_version)

    return PipelineDeps(analyze_fn=analyze, score_fn=score,
                        localize_fn=localize, investigate_fn=invest)


def fake_investigate(project_dir, paper_id, pdf, analysis, localized,
                     sha256="", question_version=1, n_findings: int = 2) -> int:
    """Mimic cmd_investigate's mutation via the validated M4 apply_delta."""
    from agent import _paper_entry, _repo_root
    import argparse as _ap

    project = load_project(Path(project_dir))
    analysis_path = Path(analysis)
    result = AnalysisResult.model_validate_json(analysis_path.read_text())
    oid = result.objects[0].id
    delta = ProjectDelta(
        findings=[NewFinding(statement=f"finding {i} from {paper_id}",
                             evidence="Table 1, p.2", page=2, object_ids=[oid],
                             confidence=0.8)
                  for i in range(n_findings)],
        connections=([NewConnection(from_ref="new[0]", to_ref="f1",
                                    type="extends", reason="builds on it",
                                    confidence=0.7)]
                     if project.papers else []),
        contribution=ContributionBody(what_it_adds="x", stance_toward_prior="y",
                                      overlap_with_direction="z"),
        open_questions=[NewOpenQuestion(text="what next?", finding_refs=["new[0]"])],
        novelty_risks=[] if project.papers else [],
    )
    ns = _ap.Namespace(paper_id=paper_id, pdf=pdf, analysis=analysis,
                       localized=localized, sha256=sha256,
                       question_version=question_version)
    paper = _paper_entry(ns, result, len(project.papers) + 1)
    if not paper.pdf_path or Path(paper.pdf_path).is_absolute():
        # Keep test-outside-repo paths absolute; _rel handles repo paths.
        pass
    project = apply_delta(project, paper, result, delta)
    save_project(Path(project_dir), project)
    return 0


def tiny_pdf_bytes(pages: int = 2) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Fake paper page {i + 1}\nFigure 1. Overview.\n(1)")
    out = doc.tobytes()
    doc.close()
    return out


def plain_pdf_bytes(pages: int = 2) -> bytes:
    """A PDF with no labeled scientific objects (empty label checklist)."""
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Quarterly business update, page {i + 1}.\n"
                                   f"Sales grew steadily this quarter.")
    out = doc.tobytes()
    doc.close()
    return out


class TempRootMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m7-test-")
        self.root = Path(self.tmp.name) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.tmp.cleanup)

    def make_project(self, title="P", question=QUESTION, pid="p1"):
        return ws_mod.create_project(self.root, title, question, project_id=pid)


# ---------------------------------------------------------------------------
# Workspace registry
# ---------------------------------------------------------------------------


class WorkspaceTest(TempRootMixin, unittest.TestCase):
    def test_create_list_get_empty_project(self):
        pid, _, _ = self.make_project("Transport Geometry", QUESTION, "tgeo")
        self.assertEqual(pid, "tgeo")
        projects = ws_mod.list_projects(self.root)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["title"], "Transport Geometry")
        self.assertEqual(projects[0]["n_papers"], 0)
        _, project = ws_mod.get_project(self.root, "tgeo")
        self.assertEqual(project.research_question, QUESTION)
        self.assertEqual(project.question_version, 1)
        self.assertEqual(len(project.question_history), 1)
        self.assertEqual(project.question_history[0].status, "current")

    def test_create_validates_inputs(self):
        with self.assertRaises(ValueError):
            ws_mod.create_project(self.root, "", QUESTION)
        with self.assertRaises(ValueError):
            ws_mod.create_project(self.root, "T", "  ")
        with self.assertRaises(ValueError):
            ws_mod.create_project(self.root, "x" * 201, QUESTION)

    def test_duplicate_and_bad_ids_rejected(self):
        self.make_project(pid="p1")
        with self.assertRaises(ValueError):
            ws_mod.create_project(self.root, "T", QUESTION, project_id="p1")
        for bad in ["../x", "a/b", "A", "", "x" * 70, ".", ".."]:
            with self.assertRaises(ValueError, msg=bad):
                ws_mod.validate_project_id(bad)
        with self.assertRaises(ValueError):
            ws_mod.resolve_project_dir(self.root, "../escape")

    def test_symlinked_dirs_ignored(self):
        self.make_project(pid="p1")
        (self.root / "evil").symlink_to(self.root / "p1", target_is_directory=True)
        self.assertEqual([p["project_id"] for p in ws_mod.list_projects(self.root)], ["p1"])

    def test_title_edit(self):
        self.make_project(pid="p1")
        p = ws_mod.update_project_meta(self.root, "p1", title="New Title")
        self.assertEqual(p.title, "New Title")
        with self.assertRaises(ValueError):
            ws_mod.update_project_meta(self.root, "p1", title="  ")

    def test_old_project_json_loads_with_defaults(self):
        d = self.root / "legacy"
        d.mkdir()
        (d / "project.json").write_text(json.dumps({
            "schema_version": "m4-1", "research_question": "old?",
            "papers": [], "findings": [], "connections": [], "contributions": [],
            "open_questions": [], "novelty_risks": [],
        }), encoding="utf-8")
        _, project = ws_mod.get_project(self.root, "legacy")
        self.assertEqual(project.question_version, 1)
        self.assertEqual(project.title, "")
        self.assertEqual(project.question_history, [])

    def test_question_change_archives_and_bumps(self):
        _, pdir, _ = self.make_project(pid="p1")
        # Seed one paper + conclusions.
        pdir_papers = pdir / "papers" / "a"
        pdir_papers.mkdir(parents=True)
        (pdir_papers / "source.pdf").write_bytes(tiny_pdf_bytes())
        project = load_project(pdir)
        paper = PaperEntry(paper_id="a", title="A", filename="a.pdf",
                           added_order=1, n_objects=4, n_localized=3,
                           pdf_path="x", localized_path="y", sha256="s",
                           question_version=1)
        delta = ProjectDelta(
            findings=[NewFinding(statement="s", evidence="e", confidence=0.5)],
            connections=[],
            contribution=ContributionBody(what_it_adds="a", stance_toward_prior="b",
                                          overlap_with_direction="c"))
        project = apply_delta(project, paper, make_draft_result(), delta)
        project.verifications.append(VerificationEvidence(
            id="v1", thread_id="t", paper_id="a", claim_id="claim-1",
            status="inconclusive"))
        save_project(pdir, project)
        preview = ws_mod.question_change_preview(self.root, "p1")
        self.assertEqual(preview["from_version"], 1)
        self.assertEqual(preview["to_version"], 2)
        self.assertEqual(preview["conclusions_to_archive"]["findings"], 1)

        project, archived = ws_mod.apply_question_change(self.root, "p1", QUESTION_2)
        self.assertEqual(project.question_version, 2)
        self.assertEqual(project.research_question, QUESTION_2)
        self.assertEqual(project.findings, [])  # never stale-as-current
        self.assertEqual(len(project.papers), 1)  # papers preserved
        self.assertEqual(len(project.verifications), 1)  # evidence preserved
        self.assertEqual(archived.version, 1)
        self.assertEqual(len(archived.findings), 1)
        self.assertEqual(archived.findings[0].status, "historical")
        # Exactly one record per version (no duplicate v1 keys).
        self.assertEqual([h.version for h in project.question_history], [1, 2])
        stored = next(h for h in project.question_history if h.version == 1)
        self.assertEqual(len(stored.findings), 1)
        with self.assertRaises(ValueError):
            ws_mod.apply_question_change(self.root, "p1", QUESTION_2)  # identical
        # A second change keeps exactly one record per version.
        project, _ = ws_mod.apply_question_change(self.root, "p1", "third question?")
        self.assertEqual([h.version for h in project.question_history], [1, 2, 3])
        self.assertEqual(
            [h.status for h in project.question_history],
            ["superseded", "superseded", "current"])

    def test_multiple_projects_independent(self):
        self.make_project("A", "q-a?", "pa")
        self.make_project("B", "q-b?", "pb")
        ws_mod.update_project_meta(self.root, "pa", title="A2")
        _, b = ws_mod.get_project(self.root, "pb")
        self.assertEqual(b.title, "B")
        self.assertEqual(b.research_question, "q-b?")

    def test_apply_delta_preserves_evidence_and_stamps_version(self):
        # Regression: apply_delta used to drop verifications/audits.
        _, pdir, _ = self.make_project(pid="p1")
        project = load_project(pdir)
        paper = PaperEntry(paper_id="a", title="A", filename="a.pdf",
                           added_order=1, n_objects=4, n_localized=3)
        delta = ProjectDelta(
            findings=[NewFinding(statement="s", evidence="e", confidence=0.5)],
            connections=[],
            contribution=ContributionBody(what_it_adds="a", stance_toward_prior="b",
                                          overlap_with_direction="c"))
        project = apply_delta(project, paper, make_draft_result(), delta)
        project.verifications.append(VerificationEvidence(
            id="v1", thread_id="t", paper_id="a", claim_id="claim-1",
            status="inconclusive", question_version=1))
        paper2 = PaperEntry(paper_id="b", title="B", filename="b.pdf",
                            added_order=2, n_objects=4, n_localized=3)
        delta2 = ProjectDelta(
            findings=[NewFinding(statement="s2", evidence="e2", confidence=0.5)],
            connections=[NewConnection(from_ref="new[0]", to_ref="f1",
                                       type="supports", reason="r",
                                       confidence=0.5)],
            contribution=ContributionBody(what_it_adds="a", stance_toward_prior="b",
                                          overlap_with_direction="c"))
        merged = apply_delta(project, paper2, make_draft_result(), delta2)
        self.assertEqual(len(merged.verifications), 1)
        self.assertEqual(merged.findings[-1].question_version, 1)
        self.assertEqual(merged.findings[-1].status, "current")
        self.assertEqual(merged.connections[-1].question_version, 1)


def make_draft_result() -> AnalysisResult:
    draft = make_draft()
    return merge_relevance(draft, fake_score("q", draft), "q")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class JobsTest(TempRootMixin, unittest.TestCase):
    def test_lifecycle_and_idempotency(self):
        j = jobs_mod.create_job(self.root, "p1", "ingest_paper", paper_id="a",
                                idempotency_key="k1", params={"x": 1})
        self.assertEqual(j["state"], "QUEUED")
        self.assertEqual(j["params"], {"x": 1})
        again = jobs_mod.create_job(self.root, "p1", "ingest_paper",
                                    paper_id="a", idempotency_key="k1")
        self.assertEqual(again["job_id"], j["job_id"])  # idempotent
        other = jobs_mod.create_job(self.root, "p1", "ingest_paper",
                                    paper_id="a", idempotency_key="k2")
        self.assertNotEqual(other["job_id"], j["job_id"])
        with self.assertRaises(ValueError):
            jobs_mod.create_job(self.root, "p1", "nope")

    def test_claim_orders_oldest_first(self):
        a = jobs_mod.create_job(self.root, "p1", "ingest_paper", paper_id="a")
        b = jobs_mod.create_job(self.root, "p1", "ingest_paper", paper_id="b")
        first = jobs_mod.claim_next_queued(self.root)
        assert first is not None
        self.assertEqual(first["job_id"], a["job_id"])
        self.assertEqual(first["state"], "RUNNING")
        self.assertEqual(first["attempts"], 1)
        second = jobs_mod.claim_next_queued(self.root)
        assert second is not None
        self.assertEqual(second["job_id"], b["job_id"])
        self.assertIsNone(jobs_mod.claim_next_queued(self.root))

    def test_update_rejects_bad_state_and_fields(self):
        j = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        with self.assertRaises(ValueError):
            jobs_mod.update_job(self.root, j["job_id"], state="NOPE")
        with self.assertRaises(ValueError):
            jobs_mod.update_job(self.root, j["job_id"], bogus=1)
        with self.assertRaises(FileNotFoundError):
            jobs_mod.get_job(self.root, "job-missing")

    def test_retry_and_cancel_rules(self):
        j = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, j["job_id"], state="FAILED",
                            last_error="boom")
        r = jobs_mod.retry_job(self.root, j["job_id"])
        self.assertEqual(r["state"], "QUEUED")
        self.assertEqual(r["last_error"], "")
        jobs_mod.update_job(self.root, j["job_id"], state="COMPLETED")
        with self.assertRaises(ValueError):
            jobs_mod.retry_job(self.root, j["job_id"])
        with self.assertRaises(ValueError):
            jobs_mod.cancel_job(self.root, j["job_id"])
        k = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, k["job_id"], state="FAILED",
                            attempts=MAX_ATTEMPTS)
        with self.assertRaises(ValueError):
            jobs_mod.retry_job(self.root, k["job_id"])
        c = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, c["job_id"], state="RUNNING")
        self.assertEqual(jobs_mod.cancel_job(self.root, c["job_id"])["state"],
                         "CANCELLED")

    def test_restart_recovery(self):
        running = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, running["job_id"], state="RUNNING")
        doomed = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, doomed["job_id"], state="RUNNING",
                            attempts=MAX_ATTEMPTS)
        done = jobs_mod.create_job(self.root, "p1", "ingest_paper")
        jobs_mod.update_job(self.root, done["job_id"], state="COMPLETED")
        rec = jobs_mod.reconcile_after_restart(self.root)
        self.assertEqual(rec, {"requeued": 1, "failed": 1})
        self.assertEqual(jobs_mod.get_job(self.root, running["job_id"])["state"],
                         "QUEUED")
        self.assertEqual(jobs_mod.get_job(self.root, doomed["job_id"])["state"],
                         "FAILED")
        self.assertEqual(jobs_mod.get_job(self.root, done["job_id"])["state"],
                         "COMPLETED")

    def test_persistence_across_connections(self):
        j = jobs_mod.create_job(self.root, "p1", "ingest_paper", paper_id="a")
        # Fresh connection (simulates process restart): job survives.
        conn = sqlite3.connect(str(self.root / "workspace.sqlite"))
        row = conn.execute("SELECT state FROM jobs WHERE job_id = ?",
                           (j["job_id"],)).fetchone()
        conn.close()
        self.assertEqual(row[0], "QUEUED")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class PipelineTest(TempRootMixin, unittest.TestCase):
    def test_validate_pdf_accepts_real_sample(self):
        sample = REPO / "samples" / "eot_shape_correspondence.pdf"
        if not sample.is_file():
            self.skipTest("sample PDF missing")
        info = pipe_mod.validate_pdf(sample.read_bytes(), "eot.pdf")
        self.assertEqual(info["n_pages"], 11)
        self.assertEqual(len(info["sha256"]), 64)

    def test_validate_pdf_rejects_garbage_limits_and_display(self):
        with self.assertRaises(PipelineError):
            pipe_mod.validate_pdf(b"not a pdf", "x.pdf")
        with mock.patch.dict(os.environ, {"RESEARCHOS_MAX_PDF_MB": "0"}):
            with self.assertRaises(PipelineError):
                pipe_mod.validate_pdf(b"%PDF-1.4" + b"0" * 100, "x.pdf")
        with mock.patch.dict(os.environ, {"RESEARCHOS_MAX_PDF_PAGES": "1"}):
            with self.assertRaises(PipelineError):
                pipe_mod.validate_pdf(tiny_pdf_bytes(2), "x.pdf")
        self.assertNotIn("/", pipe_mod.safe_display_name("../../etc/passwd"))
        self.assertNotIn("..", pipe_mod.safe_display_name("../../etc/passwd"))

    def test_legacy_cache_hit_by_content_hash(self):
        sample = REPO / "samples" / "eot_shape_correspondence.pdf"
        if not (sample.is_file() and (PROTO / "output.json").is_file()
                and (PROTO / "localized_output.json").is_file()):
            self.skipTest("legacy analysis files missing")
        sha = hashlib.sha256(sample.read_bytes()).hexdigest()
        hit = pipe_mod.find_reusable_analysis(self.root, sha)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["kind"], "legacy-analysis")

    def test_run_ingest_full_fake_flow(self):
        self.make_project(pid="p1")
        calls: dict = {}
        out = pipe_mod.run_ingest(self.root, "p1", "paper-a",
                                  tiny_pdf_bytes(), "Paper A.pdf",
                                  deps=make_deps(calls))
        self.assertEqual(out["paper_id"], "paper-a")
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(len(project.papers), 1)
        self.assertEqual(len(project.findings), 2)
        paper = project.papers[0]
        self.assertTrue(paper.sha256)
        self.assertEqual(paper.question_version, 1)
        pdir = self.root / "p1" / "papers" / "paper-a"
        for name in ("source.pdf", "draft.json", "analysis.json",
                     "localized.json", "pipeline.json"):
            self.assertTrue((pdir / name).is_file(), name)
        # Duplicate upload of identical bytes is rejected.
        with self.assertRaises(DuplicatePDFError):
            pipe_mod.run_ingest(self.root, "p1", "paper-b",
                                (pdir / "source.pdf").read_bytes(),
                                "copy.pdf", deps=make_deps({}))

    def test_run_ingest_real_pdf_with_cache_reuse(self):
        sample = REPO / "samples" / "eot_shape_correspondence.pdf"
        if not (sample.is_file() and (PROTO / "output.json").is_file()
                and (PROTO / "localized_output.json").is_file()):
            self.skipTest("legacy analysis files missing")
        self.make_project("T", "fresh question?", "p1")
        calls: dict = {}
        out = pipe_mod.run_ingest(self.root, "p1", "eot",
                                  sample.read_bytes(), "eot.pdf",
                                  deps=make_deps(calls))
        # Stage 1 skipped via verified cache; relevance + investigate ran.
        self.assertEqual(calls.get("analyze", 0), 0)
        self.assertEqual(calls.get("score", 0), 1)
        self.assertEqual(calls.get("investigate", 0), 1)
        self.assertIn("reused_from", out["stats"])
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(len(project.papers), 1)
        loc = json.loads((self.root / "p1" / "papers" / "eot"
                          / "localized.json").read_text())
        self.assertEqual(loc["analysis"]["research_question"], "fresh question?")
        self.assertTrue(any(m["bbox"] for m in loc["localizations"]))

    def test_retry_reuses_completed_stages_no_dupe_findings(self):
        self.make_project(pid="p1")
        pdf = tiny_pdf_bytes()
        calls: dict = {}
        state = {"fail": True}

        def flaky(*a, **k):
            if state["fail"]:
                return 1
            return fake_investigate(*a, **k)

        with self.assertRaises(PipelineError):
            pipe_mod.run_ingest(self.root, "p1", "paper-a", pdf, "a.pdf",
                                deps=make_deps(calls, investigate=flaky))
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(project.papers, [])  # failed attach left no paper
        state["fail"] = False
        pipe_mod.run_ingest(self.root, "p1", "paper-a", pdf, "a.pdf",
                            deps=make_deps(calls, investigate=flaky))
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(len(project.papers), 1)
        self.assertEqual(len(project.findings), 2)  # attached exactly once
        self.assertEqual(calls.get("analyze", 0), 1)  # expensive stage reused

    def test_ingest_reports_awaiting_when_unattached(self):
        # The investigator returns 0 for completed AND awaiting-review runs:
        # attachment (not the return code) decides the outcome.
        from agent_graph import RunRecord, save_run_record

        self.make_project(pid="p1")

        def awaiting_investigate(project_dir, paper_id, pdf, analysis,
                                 localized, sha256="", question_version=1):
            save_run_record(Path(project_dir), RunRecord(
                thread_id="t-await", project_id="p1", paper_id=paper_id,
                research_question="q", status="awaiting_human",
                created_at="x", updated_at="y"))
            return 0

        out = pipe_mod.run_ingest(self.root, "p1", "paper-a", tiny_pdf_bytes(),
                                  "a.pdf",
                                  deps=make_deps({}, investigate=awaiting_investigate))
        self.assertEqual(out["awaiting_review"], "t-await")
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(project.papers, [])  # attaches only after review

    def test_ingest_fails_when_unattached_without_review(self):
        self.make_project(pid="p1")

        def silent_investigate(*a, **k):
            return 0  # claims success but attaches nothing, no run record

        with self.assertRaises(PipelineError):
            pipe_mod.run_ingest(self.root, "p1", "paper-a", tiny_pdf_bytes(),
                                "a.pdf",
                                deps=make_deps({}, investigate=silent_investigate))

    def test_snapshot_excludes_failed_ingests(self):
        from workspace_server import rebuild_status

        _, pdir, _ = self.make_project(pid="p1")
        pipe_mod.run_ingest(self.root, "p1", "paper-a", tiny_pdf_bytes(),
                            "a.pdf", deps=make_deps({}))
        # A failed ingest left only a staged PDF behind.
        ghost = pdir / "papers" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "source.pdf").write_bytes(tiny_pdf_bytes())
        _, project = ws_mod.get_project(self.root, "p1")
        st = rebuild_status(pdir, project)
        self.assertFalse(st["needed"])  # ghost needs ingest retry, not rebuild

    def test_non_paper_fails_honestly_without_retry(self):
        from analyze_paper import InvalidAnalysisContent

        self.make_project(pid="p1")

        def bad_analyze(pdf_bytes, filename, checklist):
            assert checklist.strip() == ""  # nothing labeled in this PDF
            raise InvalidAnalysisContent("missing: ['equation', 'figure', 'table']")

        deps = make_deps({})
        deps.analyze_fn = bad_analyze
        with self.assertRaises(PipelineError) as ctx:
            pipe_mod.run_ingest(self.root, "p1", "report", plain_pdf_bytes(),
                                "Report.pdf", deps=deps)
        self.assertIn("doesn't appear to be a scientific paper", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable)

    def test_model_miss_with_labels_stays_retryable(self):
        from analyze_paper import InvalidAnalysisContent

        self.make_project(pid="p1")

        def bad_analyze(pdf_bytes, filename, checklist):
            assert checklist.strip() != ""
            raise InvalidAnalysisContent("missing: ['equation']")

        deps = make_deps({})
        deps.analyze_fn = bad_analyze
        with self.assertRaises(PipelineError) as ctx:
            pipe_mod.run_ingest(self.root, "p1", "paper-a", tiny_pdf_bytes(),
                                "a.pdf", deps=deps)
        self.assertIn("scientific analysis failed", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)

    def test_rescore_refreshes_relevance_keeps_boxes(self):
        self.make_project(pid="p1")
        pipe_mod.run_ingest(self.root, "p1", "paper-a", tiny_pdf_bytes(),
                            "a.pdf", deps=make_deps({}))
        before = json.loads((self.root / "p1" / "papers" / "paper-a"
                             / "localized.json").read_text())
        boxes_before = [m["bbox"] for m in before["localizations"]]
        out = pipe_mod.rescore_paper(self.root, "p1", "paper-a", QUESTION_2, 2,
                                     deps=make_deps({}))
        self.assertEqual(out["paper_id"], "paper-a")
        after = json.loads((self.root / "p1" / "papers" / "paper-a"
                            / "localized.json").read_text())
        self.assertEqual(after["analysis"]["research_question"], QUESTION_2)
        self.assertEqual([m["bbox"] for m in after["localizations"]],
                         boxes_before)
        _, project = ws_mod.get_project(self.root, "p1")
        self.assertEqual(project.papers[0].question_version, 2)


# ---------------------------------------------------------------------------
# Live HTTP server
# ---------------------------------------------------------------------------


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]):
    boundary = "----m7testboundary"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    for k, (fname, data, ctype) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                 f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class _FakeSocket:
    """In-memory socket: runs the REAL BaseHTTPRequestHandler stack with no
    port binding (sandboxes may forbid binds; routing/parsing stay genuine)."""

    def __init__(self, request_bytes: bytes):
        self._r = request_bytes
        self.out = b""

    def makefile(self, mode, *args, **kwargs):
        import io

        if "r" in mode:
            return io.BytesIO(self._r)
        buf = io.BytesIO()

        class _Cap(io.BytesIO):
            def close(inner_self):
                # Capture output instead of discarding it.
                self.out = inner_self.getvalue()

        cap = _Cap()
        return cap

    def sendall(self, data: bytes):
        self.out += data

    def getsockname(self):
        return ("127.0.0.1", 0)

    def getpeername(self):
        return ("127.0.0.1", 0)

    def settimeout(self, *a):
        pass


class ServerTest(TempRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.calls: dict = {}
        self._old_root = srv_mod.PROJECTS_ROOT
        self._old_deps = srv_mod.PIPELINE_DEPS
        srv_mod.PROJECTS_ROOT = self.root
        srv_mod.PIPELINE_DEPS = make_deps(self.calls)
        self.addCleanup(self.tearDownServer)

    def tearDownServer(self):
        srv_mod.PROJECTS_ROOT = self._old_root
        srv_mod.PIPELINE_DEPS = self._old_deps

    def req(self, method, path, body=None, headers=None):
        body = body or b""
        lines = [f"{method} {path} HTTP/1.1", "Host: test",
                 "Connection: close", f"Content-Length: {len(body)}"]
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        sock = _FakeSocket(raw)
        srv_mod.Handler(sock, ("127.0.0.1", 0), object())
        head, _, resp_body = sock.out.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split()[1])
        resp_headers = {}
        for line in header_lines:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip()] = v.strip()
        return status, resp_body, resp_headers

    def get_json(self, path, headers=None):
        code, body, _ = self.req("GET", path, headers=headers)
        return code, json.loads(body.decode())

    def post_json(self, path, payload):
        return self.req("POST", path, json.dumps(payload).encode(),
                        {"Content-Type": "application/json"})

    def wait_job(self, job_id, timeout=60.0):
        # Pump the real dispatcher synchronously (no background thread).
        end = time.time() + timeout
        while time.time() < end:
            srv_mod._dispatch_once()
            code, data = self.get_json(f"/api/jobs/{job_id}")
            self.assertEqual(code, 200)
            if data["data"]["job"]["state"] in ("COMPLETED", "FAILED",
                                                "CANCELLED"):
                return data["data"]["job"]
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish in {timeout}s")

    def test_health_reports_server_version(self):
        code, data = self.get_json("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["server_version"], srv_mod.SERVER_VERSION)

    def test_dev_sh_expects_current_server_version(self):
        # dev.sh refuses stale backends; its expectation must track the server.
        text = (REPO / "dev.sh").read_text(encoding="utf-8")
        m = re.search(r'^EXPECTED_SERVER_VERSION="([^"]+)"', text, re.M)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.group(1), srv_mod.SERVER_VERSION)

    def test_options_preflight_is_empty_and_allows_delete(self):
        code, body, headers = self.req(
            "OPTIONS", "/api/projects/x/papers",
            headers={"Origin": "http://localhost:3000",
                     "Access-Control-Request-Method": "DELETE"})
        self.assertEqual(code, 204)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "http://localhost:3000")
        self.assertIn("DELETE", headers.get("Access-Control-Allow-Methods", ""))

    def test_health_and_project_crud(self):
        code, data = self.get_json("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])
        code, body, _ = self.post_json("/api/projects",
                                       {"title": "T", "research_question": QUESTION})
        self.assertEqual(code, 201)
        pid = json.loads(body.decode())["data"]["project_id"]
        code, data = self.get_json(f"/api/projects/{pid}")
        self.assertEqual(code, 200)
        self.assertEqual(data["data"]["project"]["research_question"], QUESTION)
        code, body, _ = self.req("PATCH", f"/api/projects/{pid}",
                                 json.dumps({"title": "T2"}).encode(),
                                 {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body.decode())["data"]["title"], "T2")
        code, _ = self.get_json("/api/projects/nope")
        self.assertEqual(code, 404)
        code, body, _ = self.post_json("/api/projects",
                                       {"title": "", "research_question": "q"})
        self.assertEqual(code, 400)

    def test_upload_ingest_and_reader_endpoints(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({}, {"file": ("paper.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                 {"Content-Type": ctype})
        self.assertEqual(code, 202)
        payload = json.loads(body.decode())["data"]
        job = self.wait_job(payload["job"]["job_id"])
        self.assertEqual(job["state"], "COMPLETED")
        paper_id = payload["paper_id"]
        code, data = self.get_json(f"/api/projects/{pid}/papers/{paper_id}/data")
        self.assertEqual(code, 200)  # raw localized JSON for the Reader
        code, raw, headers = self.req(
            "GET", f"/api/projects/{pid}/papers/{paper_id}/file")
        self.assertEqual(code, 200)
        self.assertEqual(raw, pdf)
        code, data = self.get_json(f"/api/projects/{pid}")
        self.assertEqual(len(data["data"]["project"]["papers"]), 1)
        self.assertEqual(len(data["data"]["project"]["findings"]), 2)
        # Duplicate upload of identical bytes -> 409 with the original ref.
        mp2, ctype2 = _multipart({}, {"file": ("copy.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp2,
                                 {"Content-Type": ctype2})
        self.assertEqual(code, 409)
        self.assertIn("duplicate_of", json.loads(body.decode())["details"])

    def test_upload_rejects_bad_inputs(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        mp, ctype = _multipart({}, {"file": ("x.pdf", b"nope", "application/pdf")})
        code, _, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                              {"Content-Type": ctype})
        self.assertEqual(code, 400)
        mp, ctype = _multipart({"paper_id": "../evil"},
                               {"file": ("a.pdf", tiny_pdf_bytes(), "application/pdf")})
        code, _, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                              {"Content-Type": ctype})
        self.assertEqual(code, 400)
        code, _, _ = self.req("POST", f"/api/projects/{pid}/papers", b"{}",
                              {"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        code, _, _ = self.req("POST", "/api/projects/nope/papers", b"x",
                              {"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(code, 404)

    def test_upload_enforces_size_limit(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        with mock.patch.dict(os.environ, {"RESEARCHOS_MAX_PDF_MB": "0"}):
            pdf = tiny_pdf_bytes()
            mp, ctype = _multipart({}, {"file": ("a.pdf", pdf, "application/pdf")})
            code, _, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                  {"Content-Type": ctype})
            self.assertEqual(code, 413)

    def test_jobs_retry_cancel_and_isolation(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "A", "research_question": "qa?"})
        pa = json.loads(body.decode())["data"]["project_id"]
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "B", "research_question": "qb?"})
        pb = json.loads(body.decode())["data"]["project_id"]
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({"paper_id": "only-a"},
                               {"file": ("a.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pa}/papers", mp,
                                 {"Content-Type": ctype})
        job_id = json.loads(body.decode())["data"]["job"]["job_id"]
        self.wait_job(job_id)
        code, data = self.get_json(f"/api/projects/{pb}/jobs")
        self.assertEqual(data["data"]["jobs"], [])  # project B sees nothing
        code, data = self.get_json(f"/api/projects/{pb}/papers/only-a")
        self.assertEqual(code, 404)  # no cross-project paper access
        # Retry a completed job is rejected; cancel unknown job is 404.
        code, _, _ = self.req("POST", f"/api/jobs/{job_id}/retry")
        self.assertEqual(code, 409)
        code, _, _ = self.req("POST", "/api/jobs/job-missing/cancel")
        self.assertEqual(code, 404)

    def test_question_preview_confirm_flow(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({"paper_id": "a"},
                               {"file": ("a.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                 {"Content-Type": ctype})
        self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        code, data = self.get_json(f"/api/projects/{pid}/question/preview")
        self.assertEqual(code, 200)
        self.assertEqual(data["data"]["to_version"], 2)
        code, body, _ = self.post_json(f"/api/projects/{pid}/question",
                                       {"research_question": QUESTION_2})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body.decode())["data"]["needs_confirmation"])
        # Not applied until confirmed.
        _, project = ws_mod.get_project(self.root, pid)
        self.assertEqual(project.question_version, 1)
        code, body, _ = self.post_json(f"/api/projects/{pid}/question",
                                       {"research_question": QUESTION_2,
                                        "confirm": True})
        self.assertEqual(code, 202)
        payload = json.loads(body.decode())["data"]
        job = self.wait_job(payload["job"]["job_id"])
        self.assertEqual(job["state"], "COMPLETED")
        _, project = ws_mod.get_project(self.root, pid)
        self.assertEqual(project.question_version, 2)
        self.assertEqual([h.version for h in project.question_history], [1, 2])
        self.assertTrue(all(f.question_version == 2 for f in project.findings))
        self.assertEqual(len(project.findings), 2)  # rebuilt, not duplicated
        archived = [h for h in project.question_history if h.version == 1
                    and h.findings]
        self.assertEqual(len(archived), 1)  # v1 conclusions preserved

    def test_rebuild_endpoint_continues_partial_state(self):
        from project import load_project, save_project

        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({"paper_id": "a"},
                               {"file": ("a.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                 {"Content-Type": ctype})
        self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        code, body, _ = self.post_json(f"/api/projects/{pid}/question",
                                       {"research_question": QUESTION_2,
                                        "confirm": True})
        self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        # Simulate a review-paused rebuild: conclusions archived (as the
        # question change does), paper detached, analysis files complete.
        pdir, project = ws_mod.get_project(self.root, pid)
        project.papers = []
        project.findings = []
        project.connections = []
        project.contributions = []
        project.open_questions = []
        project.novelty_risks = []
        save_project(pdir, project)
        code, data = self.get_json(f"/api/projects/{pid}")
        self.assertTrue(data["data"]["rebuild"]["needed"])
        self.assertEqual(data["data"]["rebuild"]["papers"], ["a"])
        code, body, _ = self.req("POST", f"/api/projects/{pid}/rebuild")
        self.assertEqual(code, 202)
        job = self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        self.assertEqual(job["state"], "COMPLETED")
        _, project = ws_mod.get_project(self.root, pid)
        self.assertEqual(len(project.papers), 1)
        self.assertEqual(len(project.findings), 2)  # rebuilt once, not duped
        code, _, _ = self.req("POST", f"/api/projects/{pid}/rebuild")
        self.assertEqual(code, 409)  # nothing left to rebuild

    def test_non_paper_job_reports_no_retry_over_http(self):
        from analyze_paper import InvalidAnalysisContent

        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]

        def bad_analyze(pdf_bytes, filename, checklist):
            raise InvalidAnalysisContent("missing everything")

        deps = make_deps({})
        deps.analyze_fn = bad_analyze
        old = srv_mod.PIPELINE_DEPS
        srv_mod.PIPELINE_DEPS = deps
        try:
            pdf = plain_pdf_bytes()
            mp, ctype = _multipart({"paper_id": "report"},
                                   {"file": ("Report.pdf", pdf, "application/pdf")})
            code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                     {"Content-Type": ctype})
            job = self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        finally:
            srv_mod.PIPELINE_DEPS = old
        self.assertEqual(job["state"], "FAILED")
        self.assertIn("doesn't appear to be a scientific paper", job["last_error"])
        self.assertFalse(job["result"].get("retryable", True))

    def test_delete_paper_removes_staged_only(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({"paper_id": "a"},
                               {"file": ("a.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                 {"Content-Type": ctype})
        self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        # Attached papers are protected.
        code, _, _ = self.req("DELETE", f"/api/projects/{pid}/papers/a")
        self.assertEqual(code, 409)
        # Staged-only dirs can be removed.
        ghost = self.root / pid / "papers" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "source.pdf").write_bytes(pdf)
        code, body, _ = self.req("DELETE", f"/api/projects/{pid}/papers/ghost")
        self.assertEqual(code, 200)
        self.assertFalse(ghost.exists())
        # Idempotent: removing twice succeeds (stale UI / double-click safe).
        code, body, _ = self.req("DELETE", f"/api/projects/{pid}/papers/ghost")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body.decode())["data"].get("already_gone"))
        code, _, _ = self.req("DELETE", "/api/projects/nope/papers/ghost")
        self.assertEqual(code, 404)
        code, _, _ = self.req("DELETE", f"/api/projects/{pid}/papers/..%2Fevil")
        self.assertEqual(code, 404)

    def test_removed_paper_stays_removed(self):
        # A terminal job row must not resurrect a deleted paper's card, and
        # retrying its job refuses early with the honest remedy.
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        ghost = self.root / pid / "papers" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "source.pdf").write_bytes(tiny_pdf_bytes())
        job = jobs_mod.create_job(self.root, pid, "ingest_paper",
                                  paper_id="ghost")
        jobs_mod.update_job(self.root, job["job_id"], state="FAILED",
                            last_error="boom")
        code, data = self.get_json(f"/api/projects/{pid}")
        self.assertIn("ghost", data["data"]["paper_status"])
        code, _, _ = self.req("DELETE", f"/api/projects/{pid}/papers/ghost")
        self.assertEqual(code, 200)
        code, data = self.get_json(f"/api/projects/{pid}")
        self.assertNotIn("ghost", data["data"]["paper_status"])
        code, body, _ = self.req("POST", f"/api/jobs/{job['job_id']}/retry")
        self.assertEqual(code, 409)
        self.assertIn("was removed", json.loads(body.decode())["error"])

    def test_delete_refuses_pending_review(self):
        from agent_graph import RunRecord, save_run_record

        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        ghost = self.root / pid / "papers" / "ghost"
        ghost.mkdir(parents=True)
        (ghost / "source.pdf").write_bytes(tiny_pdf_bytes())
        pdir, _ = ws_mod.get_project(self.root, pid)
        save_run_record(pdir, RunRecord(thread_id="t1", project_id=pid,
                                        paper_id="ghost", research_question="q",
                                        status="awaiting_human",
                                        created_at="x", updated_at="y"))
        code, body, _ = self.req("DELETE", f"/api/projects/{pid}/papers/ghost")
        self.assertEqual(code, 409)
        self.assertIn("pending human review", json.loads(body.decode())["error"])
        self.assertTrue(ghost.is_dir())  # nothing deleted

    def test_review_routing_and_agent_scopes(self):
        from agent_graph import RunRecord, save_run_record

        _, body, _ = self.post_json("/api/projects",
                                    {"title": "A", "research_question": "qa?"})
        pa = json.loads(body.decode())["data"]["project_id"]
        pdir, _ = ws_mod.get_project(self.root, pa)
        save_run_record(pdir, RunRecord(thread_id="t1", project_id=pa,
                                        paper_id="a", research_question="qa?",
                                        status="completed",
                                        created_at="x", updated_at="y"))
        code, data = self.get_json(f"/api/projects/{pa}/runs")
        self.assertEqual(len(data["data"]["runs"]), 1)
        code, _, _ = self.post_json(f"/api/projects/{pa}/runs/t1/review",
                                    {"choice": "x"})
        self.assertEqual(code, 409)  # not awaiting review
        code, _, _ = self.post_json(f"/api/projects/{pa}/runs/nope/review",
                                    {"choice": "x"})
        self.assertEqual(code, 404)
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "B", "research_question": "qb?"})
        pb = json.loads(body.decode())["data"]["project_id"]
        code, _, _ = self.post_json(f"/api/projects/{pb}/runs/t1/review",
                                    {"choice": "x"})
        self.assertEqual(code, 404)  # thread t1 is not visible in project B

    def test_verify_audit_start_fail_closed_without_backends(self):
        _, body, _ = self.post_json("/api/projects",
                                    {"title": "T", "research_question": QUESTION})
        pid = json.loads(body.decode())["data"]["project_id"]
        # Unknown claim refs are rejected before any backend probe.
        code, _, _ = self.post_json(
            f"/api/projects/{pid}/verifications/start",
            {"paper_id": "nope", "claim_id": "claim-1"})
        self.assertEqual(code, 400)
        pdf = tiny_pdf_bytes()
        mp, ctype = _multipart({"paper_id": "a"},
                               {"file": ("a.pdf", pdf, "application/pdf")})
        code, body, _ = self.req("POST", f"/api/projects/{pid}/papers", mp,
                                 {"Content-Type": ctype})
        self.wait_job(json.loads(body.decode())["data"]["job"]["job_id"])
        with mock.patch.dict(os.environ, {"BASETEN_API_KEY": ""}, clear=False):
            code, body, _ = self.post_json(
                f"/api/projects/{pid}/audits/start",
                {"paper_id": "a", "claim_id": "claim-1"})
            self.assertEqual(code, 503)
            code, body, _ = self.post_json(
                f"/api/projects/{pid}/verifications/start",
                {"paper_id": "a", "claim_id": "claim-1"})
            # Baseten is probed first; Modal absence would also be a 503.
            self.assertEqual(code, 503)

    def test_cors_allowlist(self):
        code, _, headers = self.req("GET", "/api/health",
                                    headers={"Origin": "http://localhost:3000"})
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "http://localhost:3000")
        code, _, headers = self.req("GET", "/api/health",
                                    headers={"Origin": "http://evil.example"})
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_legacy_routes_map_to_default_project(self):
        from agent_graph import RunRecord, save_run_record

        # Without a default project, legacy routes report honestly.
        code, body = self.get_json("/api/agent/runs")
        self.assertEqual(code, 404)
        ws_mod.create_project(self.root, "Demo", QUESTION,
                              project_id="ot-correspondence")
        pdir, _ = ws_mod.get_project(self.root, "ot-correspondence")
        save_run_record(pdir, RunRecord(thread_id="t1",
                                        project_id="ot-correspondence",
                                        paper_id="a", research_question="q",
                                        status="completed",
                                        created_at="x", updated_at="y"))
        code, body = self.get_json("/api/agent/runs")
        self.assertEqual(code, 200)
        self.assertIn("runs", body)  # legacy unenveloped shape
        self.assertEqual(len(body["runs"]), 1)

    def test_old_project_compatible_over_http(self):
        d = self.root / "legacy"
        d.mkdir()
        (d / "project.json").write_text(json.dumps({
            "schema_version": "m4-1", "research_question": "old?",
            "papers": [{"paper_id": "a", "title": "A", "filename": "a.pdf",
                        "added_order": 1, "n_objects": 0, "n_localized": 0}],
            "findings": [], "connections": [], "contributions": [],
            "open_questions": [], "novelty_risks": [],
        }), encoding="utf-8")
        code, data = self.get_json("/api/projects/legacy")
        self.assertEqual(code, 200)
        self.assertEqual(data["data"]["project"]["question_version"], 1)
        code, data = self.get_json("/api/projects")
        self.assertIn("legacy", [p["project_id"] for p in data["data"]["projects"]])


if __name__ == "__main__":
    unittest.main()
