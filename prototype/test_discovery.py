#!/usr/bin/env python3
"""ResearchOS Milestone 8 tests: literature discovery.

Deterministic fakes throughout (no API keys, no network): scripted Spark
Open responses per graph node, scripted OpenAlex records, and a fake PDF
transport for acquisition. Server tests reuse the M7 in-process harness.
"""

from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import acquisition as acq_mod
import discovery_graph as dg_mod
import discovery_models as dm_mod
import workspace_server as srv_mod
import test_workspace as tw
from agent_graph import load_run_record
from discovery_graph import DiscoveryContext, DiscoveryBudget  # noqa: F401
from discovery_models import (
    DiscoveryBudget as Budget,
    DiscoveryCandidate,
    DiscoveryFailure,
    DiscoveryRun,
    clamp_novelty_wording,
    candidate_from_record,
    dedupe_records,
    normalize_arxiv_id,
    normalize_doi,
    normalize_openalex_id,
    normalize_title,
)
from openalex import FakeOpenAlex, OpenAlexError, fake_record
from project import load_project, save_project, Project, PaperEntry
from spark_open import SparkOpenError, TelemetryRecord
from workspace_server import DiscoveryDeps, validated_discovery_budget

QUESTION = "Can alternative optimal transport costs improve non-rigid shape matching?"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSpark:
    """Per-node scripted Spark Open double (mirrors the M5/M6 pattern)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def complete_json(self, messages, schema, *, node, thread_id, **kwargs):
        self.calls.append(node)
        if node not in self.script or not self.script[node]:
            raise AssertionError(f"unexpected spark call in node '{node}'")
        item = self.script[node].pop(0)
        if isinstance(item, Exception):
            raise item
        tel = TelemetryRecord(
            timestamp="2026-11-04T00:00:00+00:00", thread_id=thread_id,
            node=node, latency_ms=5, success=True)
        return item, tel


def _plan(*queries, notes="test strategy"):
    return dg_mod.SearchPlan.model_validate({
        "strategy_notes": notes,
        "queries": [{"query": q, "strategy": "A", "rationale": "r"}
                    for q in queries]})


def _eval(cid, verdict="relevant", **over):
    base = {"candidate_id": cid, "verdict": verdict,
            "why_relevant": "matches the question", "may_contribute": "method",
            "may_overlap_with": "", "still_unknown": "full results",
            "why_read_next": "promising", "novelty_overlap": False,
            "novelty_note": ""}
    base.update(over)
    return base


def _evals(*items):
    return dg_mod.EvaluationResult.model_validate({"evaluations": list(items)})


def _reflect(decision="stop", queries=(), explanation="done"):
    return dg_mod.ReflectionResult.model_validate({
        "decision": decision, "explanation": explanation,
        "round2_queries": [{"query": q, "strategy": "B", "rationale": "r"}
                           for q in queries],
        "expansions": [], "coverage_notes": ""})


def _critic(verdict="SUPPORTED", **over):
    base = {"verdict": verdict, "summary": "looks grounded", "concerns": [],
            "drop_candidate_ids": [], "required_fixes": []}
    base.update(over)
    return dg_mod.DiscoveryCriticVerdict.model_validate(base)


def _revision(*corrected, notes="fixed"):
    return dg_mod.DiscoveryRevision.model_validate(
        {"corrected_evaluations": list(corrected), "notes": notes})


def _rec(work="W1", **over):
    base = {"title": f"Paper {work}", "authors": ["A. Uthor"],
            "publication_year": 2024, "doi": f"https://doi.org/10.0000/{work.lower()}",
            "openalex_id": f"https://openalex.org/{work}",
            "arxiv_id": "", "abstract": f"Abstract of {work}.",
            "pdf_url": "", "landing_page_url": f"https://example.org/{work}",
            "venue": "Venue", "citation_count": 3}
    base.update(over)
    return base


def make_graph_project(root: Path, pid="disc-proj", question=QUESTION,
                       papers=()):
    import workspace as ws_mod

    _, project_dir, _ = ws_mod.create_project(root, "T", question,
                                              project_id=pid)
    if papers:
        project = load_project(project_dir)
        for i, paper in enumerate(papers, start=1):
            project.papers.append(paper)
        save_project(project_dir, project)
    return project_dir


# ---------------------------------------------------------------------------
# Models: normalization
# ---------------------------------------------------------------------------


class NormalizationTest(unittest.TestCase):
    def test_normalize_doi_variants(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1000/ABC"), "10.1000/abc")
        self.assertEqual(normalize_doi("doi:10.1000/abc"), "10.1000/abc")
        self.assertEqual(normalize_doi("  10.1000/abc  "), "10.1000/abc")
        self.assertEqual(normalize_doi(""), "")

    def test_normalize_openalex_id(self):
        self.assertEqual(normalize_openalex_id("https://openalex.org/W123"),
                         "W123")
        self.assertEqual(normalize_openalex_id("w123"), "W123")
        self.assertEqual(normalize_openalex_id(""), "")

    def test_normalize_arxiv_id_and_base(self):
        self.assertEqual(normalize_arxiv_id("https://arxiv.org/abs/2401.00001v2"),
                         "2401.00001v2")
        self.assertEqual(dm_mod.arxiv_base("2401.00001v2"), "2401.00001")
        self.assertEqual(dm_mod.arxiv_base("2401.00001"), "2401.00001")

    def test_normalize_title(self):
        self.assertEqual(normalize_title("  Sliced-Wasserstein  Distances! "),
                         "sliced wasserstein distances")
        self.assertEqual(normalize_title(""), "")


# ---------------------------------------------------------------------------
# Models: dedup
# ---------------------------------------------------------------------------


class DedupTest(unittest.TestCase):
    def test_stable_id_match_across_forms(self):
        records = [_rec("W1"),
                   _rec("W2", title="Totally different title here",
                        doi="https://doi.org/10.0000/w1")]
        unique, dups = dedupe_records(records, [], [])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(dups), 1)
        self.assertIn("doi:", dups[0]["duplicate_reason"])

    def test_arxiv_version_pair_merges(self):
        a = _rec("W1", doi="", openalex_id="", arxiv_id="2401.00001v1")
        b = _rec("W2", doi="", openalex_id="", arxiv_id="2401.00001v2",
                 title="A revised long title for testing merge")
        unique, dups = dedupe_records([a, b], [], [])
        self.assertEqual(len(unique), 1)
        self.assertEqual(len(dups), 1)
        self.assertIn("ax:2401.00001", dups[0]["duplicate_reason"])

    def test_title_fallback_requires_year_or_author(self):
        # Same normalized title, different year AND different authors: new.
        a = _rec("W1", doi="", openalex_id="",
                 title="On optimal transport costs for shapes",
                 publication_year=2020, authors=["X. First"])
        b = _rec("W2", doi="", openalex_id="",
                 title="On optimal transport costs for shapes!",
                 publication_year=2024, authors=["Y. Second"])
        unique, dups = dedupe_records([a, b], [], [])
        self.assertEqual(len(unique), 2)
        # Same title + same year: duplicate.
        c = _rec("W3", doi="", openalex_id="",
                 title="On optimal transport costs for shapes",
                 publication_year=2020, authors=["Z. Third"])
        unique, dups = dedupe_records([a, c], [], [])
        self.assertEqual(len(unique), 1)
        self.assertIn("title match", dups[0]["duplicate_reason"])

    def test_exact_title_counts_for_bare_project_paper(self):
        # Uploaded papers carry title only; an exact title match still
        # counts against the project (surfaced, never silently dropped).
        project_items = [{"paper_id": "u1",
                          "title": "Deep shells for shape matching",
                          "authors": [], "publication_year": None}]
        records = [_rec("W1", title="Deep shells for shape matching",
                        publication_year=2021, authors=["New Author"])]
        unique, dups = dedupe_records(records, project_items, [])
        self.assertEqual(unique, [])
        self.assertTrue(dups[0]["already_in_project"])

    def test_similar_titles_never_merge(self):
        a = _rec("W1", doi="", openalex_id="",
                 title="Sliced Wasserstein distances for shape matching",
                 publication_year=2023, authors=["X. First"])
        b = _rec("W2", doi="", openalex_id="",
                 title="Sliced Wasserstein distances for image retrieval",
                 publication_year=2023, authors=["X. First"])
        unique, dups = dedupe_records([a, b], [], [])
        self.assertEqual(len(unique), 2)
        self.assertEqual(dups, [])

    def test_project_and_prior_matches_labeled(self):
        project_items = [{"paper_id": "eot", "title": "Entropic optimal transport",
                          "authors": ["Cuturi"], "publication_year": 2013,
                          "doi": "https://doi.org/10.0000/eot"}]
        prior_items = [(_rec("W9", title="A prior discovery candidate paper"),
                        "run-old")]
        records = [_rec("W1", title="Entropic optimal TRANSPORT",
                        doi="10.0000/eot"),
                   _rec("W9", title="A prior discovery candidate paper"),
                   _rec("WNEW", title="A brand new candidate paper")]
        unique, dups = dedupe_records(records, project_items, prior_items)
        self.assertEqual([r["openalex_id"] for r in unique],
                         ["https://openalex.org/WNEW"])
        by_title = {d["title"]: d for d in dups}
        proj = by_title["Entropic optimal TRANSPORT"]
        self.assertEqual(proj["duplicate_of"], "project")
        self.assertTrue(proj["already_in_project"])
        prior = by_title["A prior discovery candidate paper"]
        self.assertEqual(prior["duplicate_of"], "prior discovery")
        self.assertEqual(prior["prior_label"], "run-old")


# ---------------------------------------------------------------------------
# Models: candidates + novelty clamp
# ---------------------------------------------------------------------------


class CandidateModelTest(unittest.TestCase):
    def test_evidence_levels(self):
        pdf = candidate_from_record(_rec("W1", pdf_url="https://x.org/a.pdf"),
                                    "dc1", 1, "2026-11-04T00:00:00+00:00")
        self.assertEqual(pdf.evidence_access_level, "full_text_available")
        abs_only = candidate_from_record(_rec("W2"), "dc2", 1,
                                         "2026-11-04T00:00:00+00:00")
        self.assertEqual(abs_only.evidence_access_level, "title_and_abstract")
        meta = candidate_from_record(_rec("W3", abstract=""), "dc3", 1,
                                     "2026-11-04T00:00:00+00:00")
        self.assertEqual(meta.evidence_access_level, "metadata_only")

    def test_missing_metadata_stays_missing(self):
        cand = candidate_from_record(
            {"title": "Sparse record", "openalex_id": "https://openalex.org/WX"},
            "dc1", 2, "2026-11-04T00:00:00+00:00")
        self.assertEqual(cand.authors, [])
        self.assertIsNone(cand.publication_year)
        self.assertEqual(cand.doi, "")
        self.assertEqual(cand.evidence_access_level, "metadata_only")
        self.assertEqual(cand.research_question_version, 2)
        self.assertEqual(cand.status, "pending")
        self.assertFalse(cand.recommended)

    def test_novelty_overclaims_clamped(self):
        fixed, hit = clamp_novelty_wording("No one has studied this problem.")
        self.assertTrue(hit)
        self.assertIn("coverage is limited", fixed)
        self.assertNotIn("No one has studied", fixed)
        fixed, hit = clamp_novelty_wording("This is entirely novel work.")
        self.assertTrue(hit)
        same, hit = clamp_novelty_wording(
            "This search found no direct match within the retrieved "
            "literature, but coverage is limited.")
        self.assertFalse(hit)
        self.assertEqual(same, same)


class BudgetTest(unittest.TestCase):
    def test_defaults_match_spec(self):
        budget = Budget()
        self.assertEqual(
            budget.model_dump(),
            {"max_rounds": 2, "max_queries_per_round": 3, "max_records": 50,
             "max_shortlist": 8, "max_recommendations": 5, "max_expansions": 2,
             "max_deep_inspections": 3, "max_spark_calls": 8})

    def test_validated_budget_caps_and_rejects(self):
        self.assertEqual(validated_discovery_budget(None), {})
        self.assertEqual(
            validated_discovery_budget({"max_rounds": 99}),
            {"max_rounds": 3})
        with self.assertRaises(ValueError):
            validated_discovery_budget({"nope": 1})
        with self.assertRaises(ValueError):
            validated_discovery_budget({"max_rounds": True})
        with self.assertRaises(ValueError):
            validated_discovery_budget({"max_rounds": 0})
        with self.assertRaises(ValueError):
            validated_discovery_budget(["max_rounds"])


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------


def _save_record(project_dir: Path, thread_id: str):
    from agent_graph import RunRecord, save_run_record

    record = RunRecord(
        thread_id=thread_id, project_id=project_dir.resolve().name,
        kind="discover", paper_id="", claim_id="",
        research_question=QUESTION, status="running",
        created_at="2026-11-04T00:00:00+00:00",
        updated_at="2026-11-04T00:00:00+00:00")
    save_run_record(project_dir, record)
    return record


def _node_ctx(project_dir: Path, thread_id: str = "t-node",
              spark=None, openalex=None):
    ctx = DiscoveryContext(project_dir=project_dir, spark=spark,
                           openalex=openalex)
    return {"configurable": {"thread_id": thread_id, "ctx": ctx}}


class PlanHygieneTest(unittest.TestCase):
    def test_drops_and_caps(self):
        budget = {"max_queries_per_round": 3}
        clean, notes = dg_mod._validate_plan_queries(
            [{"query": "sliced Wasserstein shape matching", "strategy": "A",
              "rationale": "r"},
             {"query": "transport", "strategy": "B", "rationale": "r"},
             {"query": "SLICED wasserstein SHAPE matching", "strategy": "C",
              "rationale": "r"},
             {"query": "entropic optimal transport benchmarks",
              "strategy": "ZZ", "rationale": "r"},
             {"query": "short", "strategy": "A", "rationale": "r"},
             {"query": "a third distinct query here", "strategy": "D",
              "rationale": "r"},
             {"query": "a fourth query over cap", "strategy": "E",
              "rationale": "r"}],
            budget, set())
        self.assertEqual([c["query"] for c in clean],
                         ["sliced Wasserstein shape matching",
                          "entropic optimal transport benchmarks",
                          "a third distinct query here"])
        self.assertEqual(clean[1]["strategy"], "?")
        self.assertTrue(any("single-term" in n for n in notes))
        self.assertTrue(any("duplicate" in n for n in notes))


class ShortlistTest(unittest.TestCase):
    def test_round_robin_prefers_abstracts_excludes_known(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            _save_record(project_dir, "t-node")
            config = _node_ctx(project_dir)

            def cand(cid, query, abstract=True, **over):
                base = {"candidate_id": cid, "title": f"Title {cid}",
                        "abstract": f"Abstract {cid}" if abstract else "",
                        "retrieval_query": query}
                base.update(over)
                return base

            state = {"thread_id": "t-node",
                     "budget": Budget(max_shortlist=3).model_dump(),
                     "counts": {},
                     "candidates": [
                         cand("dc1", "query beta"),
                         cand("dc2", "query beta", abstract=False),
                         cand("dc3", "query alpha"),
                         cand("dc4", "query beta",
                              already_in_project=True),
                         {"candidate_id": "dc5", "title": "",
                          "retrieval_query": "query alpha"},
                     ]}
            out = dg_mod.d_shortlist(state, config)
            # Abstract-bearing first, round-robin across queries (alpha
            # sorts before beta), cap 3, known/untitled excluded.
            self.assertEqual(out["shortlist"], ["dc3", "dc1", "dc2"])
            self.assertEqual(out["counts"]["shortlisted"], 3)

    def test_empty_pool_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            _save_record(project_dir, "t-node")
            out = dg_mod.d_shortlist(
                {"thread_id": "t-node", "budget": Budget().model_dump(),
                 "counts": {},
                 "candidates": [{"candidate_id": "dc1", "title": "",
                                 "retrieval_query": "q"}]},
                _node_ctx(project_dir))
            self.assertEqual(out["status"], "failed")


class PrepareTest(unittest.TestCase):
    def test_ranking_drops_offtopic_and_cap(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            _save_record(project_dir, "t-node")
            state = {
                "thread_id": "t-node",
                "budget": Budget(max_recommendations=2).model_dump(),
                "counts": {},
                "shortlist": ["dc1", "dc2", "dc3", "dc4"],
                "critic": {"drop_candidate_ids": ["dc4"]},
                "evaluated": {
                    "dc1": {"verdict": "tangential",
                            "novelty_note": "No one has studied this."},
                    "dc2": {"verdict": "relevant", "novelty_note": ""},
                    "dc3": {"verdict": "off_topic", "novelty_note": ""},
                    "dc4": {"verdict": "highly_relevant",
                            "novelty_note": ""},
                },
            }
            out = dg_mod.d_prepare(state, _node_ctx(project_dir))
            self.assertEqual(out["recommendations"], ["dc2", "dc1"])
            self.assertEqual(out["counts"]["recommended"], 2)
            self.assertIn("coverage is limited",
                          out["evaluated"]["dc1"]["novelty_note"])


class ReflectExpansionTest(unittest.TestCase):
    def test_only_real_openalex_ids_expand(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            _save_record(project_dir, "t-node")
            spark = FakeSpark({"reflect": [
                dg_mod.ReflectionResult.model_validate({
                    "decision": "continue",
                    "explanation": "check citations",
                    "round2_queries": [{"query": "gromov wasserstein shapes",
                                        "strategy": "C", "rationale": "r"}],
                    "expansions": [
                        {"work_openalex_id": "https://openalex.org/W1",
                         "direction": "citations", "rationale": "r"},
                        {"work_openalex_id": "dc1",
                         "direction": "references", "rationale": "r"},
                    ],
                    "coverage_notes": ""})]})
            state = {
                "thread_id": "t-node", "question": QUESTION, "round": 1,
                "budget": Budget().model_dump(),
                "candidates": [
                    {"candidate_id": "dc1", "title": "T1",
                     "openalex_id": "https://openalex.org/W1"}],
                "evaluated": {"dc1": {"verdict": "relevant"}},
                "shortlist": ["dc1"], "queries_done": [],
                "limitations": [],
            }
            out = dg_mod.d_reflect(
                state, _node_ctx(project_dir, spark=spark))
            self.assertEqual(out["reflection"]["decision"], "continue")
            self.assertEqual(
                [e["work_openalex_id"] for e in out["planned_expansions"]],
                ["W1"])
            self.assertTrue(any("DC1" in n for n in out["limitations"]))


class PersistStaleTest(unittest.TestCase):
    def test_question_change_marks_run_stale(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            project = load_project(project_dir)
            project.question_version = 2  # changed mid-search
            save_project(project_dir, project)
            _save_record(project_dir, "t-node")
            state = {
                "project_id": project_dir.resolve().name,
                "thread_id": "t-node", "question": QUESTION,
                "question_version": 1, "start_question_version": 1,
                "focus": "", "budget": Budget().model_dump(),
                "strategy_notes": "", "queries_done": [], "counts": {},
                "reflection": {"decision": "stop", "explanation": ""},
                "critic": {"verdict": "SUPPORTED", "summary": ""},
                "revision_used": False, "limitations": [],
                "candidates": [], "evaluated": {}, "recommendations": [],
                "retrieved": [], "duplicates_log": [], "usage": {},
            }
            out = dg_mod.d_persist(state, _node_ctx(project_dir))
            self.assertEqual(out["status"], "completed")
            run = load_project(project_dir).discoveries[0]
            self.assertTrue(run.stale)
            self.assertIn("mid-search", run.limitations)

    def test_persist_keeps_actionable_candidates_only(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            _save_record(project_dir, "t-node")

            def cand(cid, **over):
                base = {"candidate_id": cid, "title": f"Title {cid}",
                        "retrieval_query": "q", "retrieval_round": 1}
                base.update(over)
                return base

            state = {
                "project_id": project_dir.resolve().name,
                "thread_id": "t-node", "question": QUESTION,
                "question_version": 1, "start_question_version": 1,
                "focus": "", "budget": Budget().model_dump(),
                "strategy_notes": "", "queries_done": [], "counts": {},
                "reflection": {"decision": "stop", "explanation": ""},
                "critic": {"verdict": "SUPPORTED", "summary": ""},
                "revision_used": False, "limitations": [],
                "candidates": [
                    cand("dc1"),  # shortlisted
                    cand("dc2"),  # neither: dropped
                    cand("dc3", already_in_project=True),  # surfaced
                    cand("dc4"),  # evaluated only
                ],
                "shortlist": ["dc1"],
                "evaluated": {"dc4": {"verdict": "relevant"}},
                "recommendations": ["dc1"],
                "retrieved": [], "duplicates_log": [], "usage": {},
            }
            out = dg_mod.d_persist(state, _node_ctx(project_dir))
            self.assertEqual(out["status"], "completed")
            run = load_project(project_dir).discoveries[0]
            self.assertEqual(
                [c.candidate_id for c in run.candidates],
                ["dc1", "dc3", "dc4"])


# ---------------------------------------------------------------------------
# Full graph runs with fakes
# ---------------------------------------------------------------------------


def _oa_record(work, **over):
    base = {"title": f"Study of {work}",
            "doi": f"https://doi.org/10.0000/{work.lower()}",
            "authors": [f"Author {work}"],
            "abstract": f"Abstract text for {work} about transport costs.",
            "pdf_url": f"https://example.org/{work}.pdf"}
    base.update(over)
    return fake_record(f"https://openalex.org/{work}", **base)


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m8-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def _spark(self, evals, reflect=None, critic=None, plan=None):
        script = {
            "plan_search": [plan or _plan("entropic optimal transport shape matching")],
            "evaluate_relevance": [evals],
            "reflect": [reflect or _reflect("stop")],
            "discovery_critic": [critic or _critic()],
        }
        return FakeSpark(script)

    def test_full_run_stop_after_round1(self):
        project_dir = make_graph_project(self.root)
        spark = self._spark(
            _evals(_eval("dc1", "highly_relevant"),
                   _eval("dc2", "relevant")))
        oa = FakeOpenAlex({
            "entropic optimal transport shape matching": [
                _oa_record("W1"), _oa_record("W2")]})
        record = dg_mod.run_discovery(project_dir, "discover-g1", spark=spark,
                                      openalex=oa)
        self.assertEqual(record.status, "completed")
        self.assertEqual(spark.calls,
                         ["plan_search", "evaluate_relevance", "reflect",
                          "discovery_critic"])
        project = load_project(project_dir)
        self.assertEqual(len(project.discoveries), 1)
        run = project.discoveries[0]
        self.assertEqual(run.run_id, "discover-g1")
        self.assertEqual(run.status, "completed")
        self.assertFalse(run.stale)
        self.assertEqual(run.counts["retrieved"], 2)
        self.assertEqual(run.counts["recommended"], 2)
        self.assertEqual(run.usage["spark_calls"], 4)
        self.assertEqual([c.candidate_id for c in run.candidates],
                         ["dc1", "dc2"])
        self.assertTrue(all(c.recommended for c in run.candidates))
        self.assertTrue(
            (dg_mod.discovery_dir(project_dir) / "discover-g1.json").is_file())
        # No project papers were touched by a metadata-only run.
        self.assertEqual(project.papers, [])

    def test_second_round_on_continue(self):
        project_dir = make_graph_project(self.root)
        spark = self._spark(
            _evals(_eval("dc1", "relevant")),
            reflect=_reflect("continue", ["sliced Wasserstein correspondence"]),
            plan=_plan("entropic optimal transport shape matching"))
        # Round-2 evaluations scripted separately (evaluate runs each round).
        spark.script["evaluate_relevance"].append(
            _evals(_eval("dc1", "relevant"),
                   _eval("dc2", "highly_relevant")))
        spark.script["reflect"].append(_reflect("stop"))
        oa = FakeOpenAlex({
            "entropic optimal transport shape matching": [_oa_record("W1")],
            "sliced Wasserstein correspondence": [_oa_record("W2")]})
        record = dg_mod.run_discovery(
            project_dir, "discover-g2", spark=spark, openalex=oa)
        self.assertEqual(record.status, "completed")
        run = load_project(project_dir).discoveries[0]
        rounds = sorted({q.round for q in run.queries})
        self.assertEqual(rounds, [1, 2])
        self.assertEqual(run.counts["retrieved"], 2)
        self.assertEqual([c.candidate_id for c in run.candidates],
                         ["dc1", "dc2"])
        self.assertEqual(spark.calls.count("evaluate_relevance"), 2)

    def test_critic_revision_runs_once(self):
        project_dir = make_graph_project(self.root)
        spark = self._spark(
            _evals(_eval("dc1", "relevant",
                         novelty_note="No one has studied this.")),
            critic=_critic("NEEDS_REVISION", concerns=[{
                "candidate_id": "dc1", "issue": "novelty overclaim",
                "severity": "major"}],
                required_fixes=["scope the novelty note"]))
        spark.script["bounded_revision"] = [
            _revision(_eval("dc1", "relevant",
                            novelty_note="Limited coverage; needs checking."))]
        spark.script["discovery_critic"].append(_critic())
        oa = FakeOpenAlex({
            "entropic optimal transport shape matching": [_oa_record("W1")]})
        record = dg_mod.run_discovery(project_dir, "discover-g3", spark=spark,
                                      openalex=oa)
        self.assertEqual(record.status, "completed")
        run = load_project(project_dir).discoveries[0]
        self.assertTrue(run.revision_used)
        self.assertEqual(run.critic_verdict, "SUPPORTED")
        self.assertEqual(
            run.candidates[0].evaluation.novelty_note,
            "Limited coverage; needs checking.")
        self.assertEqual(spark.calls.count("bounded_revision"), 1)
        self.assertEqual(spark.calls.count("discovery_critic"), 2)

    def test_fail_closed_on_plan_failure(self):
        project_dir = make_graph_project(self.root)
        spark = FakeSpark({"plan_search": [
            SparkOpenError("Baseten is down")]})
        oa = FakeOpenAlex({})
        with self.assertRaises(DiscoveryFailure):
            dg_mod.run_discovery(project_dir, "discover-g4", spark=spark,
                                 openalex=oa)
        record = load_run_record(project_dir, "discover-g4")
        self.assertEqual(record.status, "failed")
        self.assertIn("Baseten", record.error)
        self.assertEqual(load_project(project_dir).discoveries, [])

    def test_fail_soft_on_query_error(self):
        project_dir = make_graph_project(self.root)
        spark = self._spark(_evals(_eval("dc1", "relevant")))
        spark.script["plan_search"] = [
            _plan("good query about transport costs",
                  "failing query about benchmarks")]
        oa = FakeOpenAlex({
            "good query about transport costs": [_oa_record("W1")],
            "failing query about benchmarks": OpenAlexError("boom")})
        record = dg_mod.run_discovery(project_dir, "discover-g5", spark=spark,
                                      openalex=oa)
        self.assertEqual(record.status, "completed")
        run = load_project(project_dir).discoveries[0]
        self.assertEqual(run.counts["retrieved"], 1)
        failed = [q for q in run.queries if q.error]
        self.assertEqual(len(failed), 1)

    def test_project_paper_surfaced_but_never_shortlisted(self):
        project = Project(research_question=QUESTION)
        paper = PaperEntry(paper_id="eot", title="Study of W1 transport costs",
                           filename="eot.pdf", added_order=1, n_objects=0,
                           n_localized=0)
        project_dir = make_graph_project(self.root, papers=[paper])
        # dc1 = unique W2 (shortlisted); dc2 = W1 surfaced as already-in-project.
        spark = self._spark(_evals(_eval("dc1", "relevant")))
        oa = FakeOpenAlex({
            "entropic optimal transport shape matching": [
                _oa_record("W2"),
                _oa_record("W1")]})
        # Align the fake record with the project paper for the title match.
        oa.script["entropic optimal transport shape matching"][1] = fake_record(
            "https://openalex.org/W1", title="Study of W1 transport costs",
            authors=["Someone Else"], year=2024, doi="",
            abstract="Abstract.", url="https://openalex.org/W1")
        record = dg_mod.run_discovery(project_dir, "discover-g6", spark=spark,
                                      openalex=oa)
        self.assertEqual(record.status, "completed")
        run = load_project(project_dir).discoveries[0]
        by_id = {c.candidate_id: c for c in run.candidates}
        self.assertTrue(by_id["dc1"].recommended)
        self.assertFalse(by_id["dc1"].already_in_project)
        self.assertTrue(by_id["dc2"].already_in_project)
        self.assertFalse(by_id["dc2"].recommended)
        self.assertEqual(run.counts["recommended"], 1)


# ---------------------------------------------------------------------------
# Candidate decisions
# ---------------------------------------------------------------------------


class CandidateStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="m8-test-")
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = make_graph_project(Path(self.tmp.name))
        project = load_project(self.project_dir)
        run = DiscoveryRun(
            run_id="run-1", thread_id="run-1",
            project_id=self.project_dir.resolve().name,
            research_question=QUESTION,
            candidates=[DiscoveryCandidate(candidate_id="dc1", title="T1"),
                        DiscoveryCandidate(candidate_id="dc2", title="T2")])
        project.discoveries = [run]
        save_project(self.project_dir, project)

    def test_decide_and_redecide(self):
        cand = dg_mod.update_candidate_status(
            self.project_dir, "run-1", "dc1", "approved", reason="looks good")
        self.assertEqual(cand.status, "approved")
        cand = dg_mod.update_candidate_status(
            self.project_dir, "run-1", "dc1", "dismissed")
        self.assertEqual(cand.status, "dismissed")
        cand = dg_mod.update_candidate_status(
            self.project_dir, "run-1", "dc1", "approved")
        self.assertEqual(cand.status, "approved")

    def test_import_lifecycle_locks_decisions(self):
        dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                       "importing")
        with self.assertRaises(ValueError):
            dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                           "approved")
        dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                       "imported")
        with self.assertRaises(ValueError):
            dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                           "dismissed")
        with self.assertRaises(ValueError):
            dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                           "import_failed")

    def test_unknown_ids_and_statuses(self):
        with self.assertRaises(FileNotFoundError):
            dg_mod.update_candidate_status(self.project_dir, "nope", "dc1",
                                           "approved")
        with self.assertRaises(FileNotFoundError):
            dg_mod.update_candidate_status(self.project_dir, "run-1", "dc9",
                                           "approved")
        with self.assertRaises(ValueError):
            dg_mod.update_candidate_status(self.project_dir, "run-1", "dc1",
                                           "maybe")


# ---------------------------------------------------------------------------
# Full-text acquisition
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.opened = []

    def open(self, request, timeout=None):
        url = request.get_full_url()
        self.opened.append(url)
        item = self.routes[url]
        if isinstance(item, Exception):
            raise item
        return item


def _public_dns(host, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
             ("93.184.216.34", 0))]


def _mixed_dns(host, *args, **kwargs):
    ip = "127.0.0.1" if "evil" in host else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


PDF_BYTES = b"%PDF-1.4 fake body for tests\n%%EOF"


class AcquisitionValidationTest(unittest.TestCase):
    def test_rejects_bad_urls(self):
        # Direct-connection semantics regardless of ambient proxy env.
        bad = ["", "file:///etc/passwd",
               "ftp://example.com/a.pdf",
               "https://user:pass@example.com/a.pdf",
               "https://example.com:8080/a.pdf",
               "http://127.0.0.1/a.pdf",
               "http://localhost/a.pdf",
               "http://10.0.0.5/a.pdf",
               "http://[::1]/a.pdf",
               "not a url"]
        with mock.patch.object(acq_mod, "_uses_proxy", return_value=False):
            for url in bad:
                with self.subTest(url=url):
                    with self.assertRaises(acq_mod.AcquisitionError) as cm:
                        acq_mod.validate_fetch_url(url)
                    self.assertFalse(cm.exception.retryable)

    def test_rejects_unresolvable_host(self):
        with mock.patch.object(acq_mod, "_uses_proxy", return_value=False), \
                mock.patch("socket.getaddrinfo",
                           side_effect=socket.gaierror("nope")):
            with self.assertRaises(acq_mod.AcquisitionError):
                acq_mod.validate_fetch_url("https://nonexistent.invalid/a.pdf")

    def test_accepts_public_host(self):
        with mock.patch.object(acq_mod, "_uses_proxy", return_value=False), \
                mock.patch("socket.getaddrinfo",
                           wraps=_public_dns) as dns:
            parts = acq_mod.validate_fetch_url(
                "https://oa.example.org/papers/a.pdf")
            self.assertEqual(parts.hostname, "oa.example.org")
            dns.assert_called()

    def test_proxied_host_skips_local_dns(self):
        with mock.patch("urllib.request.proxy_bypass", return_value=False), \
                mock.patch("urllib.request.getproxies",
                           return_value={"https": "http://proxy:8080"}), \
                mock.patch("socket.getaddrinfo",
                           side_effect=AssertionError("must not resolve")):
            parts = acq_mod.validate_fetch_url("https://oa.example.org/a.pdf")
            self.assertEqual(parts.hostname, "oa.example.org")

    def test_private_literal_rejected_even_when_proxied(self):
        with mock.patch("urllib.request.proxy_bypass", return_value=False), \
                mock.patch("urllib.request.getproxies",
                           return_value={"http": "http://proxy:8080"}):
            with self.assertRaises(acq_mod.AcquisitionError):
                acq_mod.validate_fetch_url("http://127.0.0.1/a.pdf")

    def test_proxy_bypass_uses_direct_dns(self):
        with mock.patch("urllib.request.proxy_bypass", return_value=True), \
                mock.patch("socket.getaddrinfo", _mixed_dns):
            with self.assertRaises(acq_mod.AcquisitionError):
                acq_mod.validate_fetch_url("http://evil.internal/a.pdf")
            parts = acq_mod.validate_fetch_url("https://oa.example.org/a.pdf")
            self.assertEqual(parts.hostname, "oa.example.org")


class FetchPdfTest(unittest.TestCase):
    def _fetch(self, routes, url, dns=_public_dns, **kwargs):
        opener = FakeOpener(routes)
        with mock.patch.object(acq_mod, "_uses_proxy", return_value=False), \
                mock.patch("socket.getaddrinfo", dns):
            return acq_mod.fetch_pdf(
                url, opener_factory=lambda: opener, **kwargs)

    def test_success(self):
        fetched = self._fetch(
            {"https://oa.example.org/a.pdf": FakeResponse(
                headers={"Content-Type": "application/pdf",
                         "Content-Length": str(len(PDF_BYTES))},
                body=PDF_BYTES)},
            "https://oa.example.org/a.pdf")
        self.assertEqual(fetched.pdf_bytes, PDF_BYTES)
        self.assertEqual(fetched.final_url, "https://oa.example.org/a.pdf")

    def test_redirect_chain_followed_and_validated(self):
        fetched = self._fetch(
            {"https://oa.example.org/a": FakeResponse(
                status=302, headers={"Location": "/b"}),
             "https://oa.example.org/b": FakeResponse(
                headers={"Content-Type": "application/pdf"},
                body=PDF_BYTES)},
            "https://oa.example.org/a")
        self.assertEqual(fetched.final_url, "https://oa.example.org/b")

    def test_redirect_to_private_host_rejected(self):
        with self.assertRaises(acq_mod.AcquisitionError) as cm:
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(
                    status=302,
                    headers={"Location": "http://evil.internal/x.pdf"})},
                "https://oa.example.org/a", dns=_mixed_dns)
        self.assertFalse(cm.exception.retryable)

    def test_html_and_bad_magic_rejected(self):
        with self.assertRaises(acq_mod.AcquisitionError):
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(
                    headers={"Content-Type": "text/html"},
                    body=b"<html>nope</html>")},
                "https://oa.example.org/a")
        with self.assertRaises(acq_mod.AcquisitionError):
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(
                    headers={"Content-Type": "application/pdf"},
                    body=b"not a pdf at all")},
                "https://oa.example.org/a")

    def test_size_caps(self):
        big_len = acq_mod.max_acquisition_bytes() + 1
        with self.assertRaises(acq_mod.AcquisitionError):
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(
                    headers={"Content-Type": "application/pdf",
                             "Content-Length": str(big_len)},
                    body=PDF_BYTES)},
                "https://oa.example.org/a")
        with self.assertRaises(acq_mod.AcquisitionError):
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(
                    headers={"Content-Type": "application/pdf"},
                    body=PDF_BYTES)},
                "https://oa.example.org/a", max_bytes=4)

    def test_retryable_vs_fatal_status(self):
        with self.assertRaises(acq_mod.AcquisitionError) as cm:
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(status=429)},
                "https://oa.example.org/a")
        self.assertTrue(cm.exception.retryable)
        with self.assertRaises(acq_mod.AcquisitionError) as cm:
            self._fetch(
                {"https://oa.example.org/a": FakeResponse(status=404)},
                "https://oa.example.org/a")
        self.assertFalse(cm.exception.retryable)

    def test_stage_download_atomically(self):
        with tempfile.TemporaryDirectory(prefix="m8-test-") as tmp:
            project_dir = make_graph_project(Path(tmp))
            dest = acq_mod.stage_downloaded_pdf(project_dir, "disc-abc-dc1",
                                                PDF_BYTES)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), PDF_BYTES)
            with self.assertRaises(ValueError):
                acq_mod.stage_downloaded_pdf(project_dir, "../evil",
                                             PDF_BYTES)


# ---------------------------------------------------------------------------
# Server API
# ---------------------------------------------------------------------------


class DiscoveryServerTest(tw.ServerTest):
    def setUp(self):
        super().setUp()
        self._old_disc = srv_mod.DISCOVERY_DEPS
        self.addCleanup(self._restore_disc)

    def _restore_disc(self):
        srv_mod.DISCOVERY_DEPS = self._old_disc

    def _deps(self, spark, openalex, fetch=None):
        srv_mod.DISCOVERY_DEPS = DiscoveryDeps(
            spark=spark, openalex=openalex, fetch_pdf=fetch)

    def _start(self, pid, payload=None):
        code, body, _ = self.post_json(f"/api/projects/{pid}/discoveries/start",
                                       payload or {})
        self.assertEqual(code, 202, body.decode()[:500])
        return json.loads(body.decode())["data"]["job"]

    def _run_discovery(self, pid, query="entropic optimal transport shapes",
                       records=(), evals=(), reflect=None):
        spark = FakeSpark({
            "plan_search": [_plan(query)],
            "evaluate_relevance": [evals or _evals(_eval("dc1", "relevant"))],
            "reflect": [reflect or _reflect("stop")],
            "discovery_critic": [_critic()],
        })
        oa = FakeOpenAlex({query: list(records) or [_oa_record("W1")]})
        self._deps(spark, oa)
        job = self._start(pid)
        return self.wait_job(job["job_id"])

    def test_start_list_get_run(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-disc")
        job = self._run_discovery(pid)
        self.assertEqual(job["state"], "COMPLETED")
        run_id = job["result"]["run_id"]
        code, data = self.get_json(f"/api/projects/{pid}/discoveries")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["data"]["discoveries"]), 1)
        self.assertIsNone(data["data"]["active_job"])
        code, data = self.get_json(f"/api/projects/{pid}/discoveries/{run_id}")
        self.assertEqual(code, 200)
        self.assertEqual(data["data"]["run"]["run_id"], run_id)
        self.assertIsNotNone(data["data"]["retrieval_log"])
        self.assertEqual(data["data"]["record"]["status"], "completed")
        code, _ = self.get_json(f"/api/projects/{pid}/discoveries/nope")
        self.assertEqual(code, 404)

    def test_start_validation(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-val")
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/start", {"budget": {"nope": 1}})
        self.assertEqual(code, 400)
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/start", {"focus": 42})
        self.assertEqual(code, 400)
        code, body, _ = self.post_json(
            "/api/projects/nope/discoveries/start", {})
        self.assertEqual(code, 404)

    def test_decide_and_import_end_to_end(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-imp")
        job = self._run_discovery(pid)
        run_id = job["result"]["run_id"]
        # Decide: approve dc1.
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/decide",
            {"decisions": [{"candidate_id": "dc1", "action": "approve"},
                           {"candidate_id": "dc9", "action": "dismiss"},
                           {"candidate_id": "dc1", "action": "bogus"}]})
        self.assertEqual(code, 200)
        items = json.loads(body.decode())["data"]["items"]
        self.assertEqual(
            [(i["ok"], i.get("status", i.get("error", ""))[:8]) for i in items],
            [(True, "approved"), (False, "unknown "), (False, "action m")])
        # Import with a fake PDF fetch + the M7 fake pipeline. The bytes
        # must be a parseable PDF: validation is real, models are fake.
        tiny = tw.tiny_pdf_bytes()
        fetched = acq_mod.FetchedPDF(
            pdf_bytes=tiny, final_url="https://oa.example.org/W1.pdf",
            content_type="application/pdf",
            sha256="ab" * 32, size=len(tiny))
        srv_mod.DISCOVERY_DEPS.fetch_pdf = lambda url: fetched
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/import",
            {"candidate_ids": ["dc1"]})
        self.assertEqual(code, 202, body.decode()[:500])
        import_job = self.wait_job(
            json.loads(body.decode())["data"]["job"]["job_id"])
        self.assertEqual(import_job["state"], "COMPLETED")
        self.assertEqual(import_job["result"]["imported"], 1)
        paper_id = import_job["result"]["items"][0]["paper_id"]
        # Paper attached to the project through the real M7 path.
        _, project = tw.ws_mod.get_project(self.root, pid)
        self.assertTrue(any(p.paper_id == paper_id for p in project.papers))
        cand = next(c for c in project.discoveries[0].candidates
                    if c.candidate_id == "dc1")
        self.assertEqual(cand.status, "imported")
        self.assertEqual(cand.evidence_access_level, "full_text_analyzed")
        # Re-import is idempotent (skipped, still ok).
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/import",
            {"candidate_ids": ["dc1"]})
        self.assertEqual(code, 202)
        again = self.wait_job(
            json.loads(body.decode())["data"]["job"]["job_id"])
        self.assertEqual(again["result"]["imported"], 1)
        self.assertIn("skipped",
                      again["result"]["items"][0].get("note", ""))

    def test_import_unavailable_and_dismissed(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-unav")
        job = self._run_discovery(
            pid, records=[_oa_record("W1", pdf_url=""),
                          _oa_record("W2", pdf_url="https://x.org/w2.pdf")],
            evals=_evals(_eval("dc1", "relevant"),
                         _eval("dc2", "relevant")))
        run_id = job["result"]["run_id"]
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/decide",
            {"decisions": [{"candidate_id": "dc2", "action": "dismiss"}]})
        self.assertEqual(code, 200)
        # Dismissed candidates cannot be imported.
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/import",
            {"candidate_ids": ["dc2"]})
        self.assertEqual(code, 400)
        # Missing PDF URL -> explicit unavailable state (job still completes).
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/{run_id}/import",
            {"candidate_ids": ["dc1"]})
        self.assertEqual(code, 202)
        done = self.wait_job(
            json.loads(body.decode())["data"]["job"]["job_id"])
        self.assertEqual(done["state"], "COMPLETED")
        self.assertTrue(done["result"]["items"][0]["unavailable"])
        _, project = tw.ws_mod.get_project(self.root, pid)
        cand = next(c for c in project.discoveries[0].candidates
                    if c.candidate_id == "dc1")
        self.assertEqual(cand.status, "full_text_unavailable")

    def test_discovery_retry_after_failure(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-retry")
        spark = FakeSpark({
            "plan_search": [SparkOpenError("flake"),
                            _plan("entropic optimal transport shapes")],
            "evaluate_relevance": [_evals(_eval("dc1", "relevant"))],
            "reflect": [_reflect("stop")],
            "discovery_critic": [_critic()],
        })
        oa = FakeOpenAlex(
            {"entropic optimal transport shapes": [_oa_record("W1")]})
        self._deps(spark, oa)
        job = self._start(pid)
        failed = self.wait_job(job["job_id"])
        self.assertEqual(failed["state"], "FAILED")
        code, body, _ = self.post_json(f"/api/jobs/{job['job_id']}/retry", {})
        self.assertEqual(code, 200)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["state"], "COMPLETED")
        # Retry used a fresh thread; exactly one run persisted.
        self.assertNotEqual(done["result"]["run_id"],
                            f"discover-{job['job_id']}")
        _, project = tw.ws_mod.get_project(self.root, pid)
        self.assertEqual(len(project.discoveries), 1)

    def test_import_self_heals_after_review_attach(self):
        from project import PaperEntry, load_project, save_project

        pid, _, _ = self.make_project("T", QUESTION, "srv-heal")
        project_dir = tw.ws_mod.resolve_project_dir(self.root, pid)
        project = load_project(project_dir)
        project.papers.append(PaperEntry(
            paper_id="disc-abc123-dc1", title="Healed paper",
            filename="a.pdf", added_order=1, n_objects=0, n_localized=0))
        project.discoveries = [DiscoveryRun(
            run_id="run-1", thread_id="run-1", project_id=pid,
            research_question=QUESTION,
            candidates=[DiscoveryCandidate(
                candidate_id="dc1", title="Healed paper", status="importing",
                project_paper_id="disc-abc123-dc1",
                pdf_url="https://x.org/a.pdf")])]
        save_project(project_dir, project)
        item = srv_mod._import_one_candidate(
            project_dir, pid, "run-1", "dc1",
            lambda stage, detail="": None, lambda: False, "job-none")
        self.assertTrue(item["ok"])
        self.assertIn("finalized", item["note"])
        project = load_project(project_dir)
        self.assertEqual(project.discoveries[0].candidates[0].status,
                         "imported")

    def test_stale_importing_reruns_when_no_job_active(self):
        pid, _, _ = self.make_project("T", QUESTION, "srv-stale")
        project_dir = tw.ws_mod.resolve_project_dir(self.root, pid)
        project = load_project(project_dir)
        project.discoveries = [DiscoveryRun(
            run_id="run-1", thread_id="run-1", project_id=pid,
            research_question=QUESTION,
            candidates=[DiscoveryCandidate(
                candidate_id="dc1", title="Stale import paper one two",
                status="importing", pdf_url="https://x.org/a.pdf")])]
        save_project(project_dir, project)
        tiny = tw.tiny_pdf_bytes()
        self._deps(FakeSpark({}), FakeOpenAlex({}),
                   fetch=lambda url: acq_mod.FetchedPDF(
                       pdf_bytes=tiny, final_url=url,
                       content_type="application/pdf",
                       sha256="cc" * 32, size=len(tiny)))
        code, body, _ = self.post_json(
            f"/api/projects/{pid}/discoveries/run-1/import",
            {"candidate_ids": ["dc1"]})
        self.assertEqual(code, 202)
        done = self.wait_job(
            json.loads(body.decode())["data"]["job"]["job_id"])
        self.assertEqual(done["state"], "COMPLETED")
        self.assertTrue(done["result"]["items"][0]["ok"])
        _, project = tw.ws_mod.get_project(self.root, pid)
        self.assertEqual(project.discoveries[0].candidates[0].status,
                         "imported")

    def test_restart_recovery_requeues_discovery(self):
        import jobs as jobs_mod

        pid, _, _ = self.make_project("T", QUESTION, "srv-restart")
        spark = FakeSpark({
            "plan_search": [_plan("entropic optimal transport shapes")],
            "evaluate_relevance": [_evals(_eval("dc1", "relevant"))],
            "reflect": [_reflect("stop")],
            "discovery_critic": [_critic()],
        })
        oa = FakeOpenAlex(
            {"entropic optimal transport shapes": [_oa_record("W1")]})
        self._deps(spark, oa)
        job = self._start(pid)
        jobs_mod.update_job(self.root, job["job_id"], state="RUNNING")
        recovered = jobs_mod.reconcile_after_restart(self.root)
        self.assertEqual(recovered["requeued"], 1)
        done = self.wait_job(job["job_id"])
        self.assertEqual(done["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
