"""Offline tests for Milestone 6.1 evidence audits (no API keys needed).

Scripted FakeSpark + FakeOpenAlex doubles stand in for Baseten and
OpenAlex; every test asserts graph behavior, deterministic gates,
budgets, persistence, and resume.

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_graph as ag
import project as pr
from agent_graph import CriticVerdict, RunRecord
from analyze_paper import AnalysisResult
from audit_graph import (
    AuditContext,
    AuditPlan,
    AuditRevision,
    ClaimClassification,
    CrossPaperAssessment,
    MethodologyAssessment,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from openalex import FakeOpenAlex, OpenAlexError, fake_record
from spark_open import TelemetryRecord


def analysis_object(obj_id, otype="equation"):
    return {
        "id": obj_id,
        "type": otype,
        "page": 2,
        "label": f"Label {obj_id}",
        "title": "T",
        "what_it_is": "x",
        "why_it_is_here": "x",
        "claim_supported": "x",
        "limitations": "x",
        "paper_importance": 0.8,
        "paper_importance_reason": "core",
        "research_relevance": "x",
        "research_relevance_score": 0.9,
        "research_relevance_reason": "y",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.2",
    }


def analysis_fixture():
    claim = analysis_object("claim-1", "claim")
    claim["title"] = "Method A beats baseline"
    claim["related_objects"] = ["tab1", "eq1"]
    return AnalysisResult.model_validate(
        {
            "paper_title": "Paper",
            "research_question": "Q?",
            "objects": [analysis_object("eq1"), analysis_object("tab1", "table"),
                        analysis_object("fig1", "figure"), claim],
        }
    )


def percent_check(**over):
    base = {
        "check_id": "c1",
        "check_type": "percentage_improvement",
        "description": "recompute improvement",
        "metric_direction": "lower",
        "values": {"baseline": 10.0, "proposed": 7.0},
        "expected_value": 30.0,
        "abs_tol": 0.5,
        "rel_tol": 0.01,
        "source_snippet": "baseline 10.0, proposed 7.0, improvement 30.0%",
        "evidence_refs": ["tab1"],
    }
    base.update(over)
    return base


def plan_fixture(**over):
    base = {
        "scope_notes": "audit tables only",
        "numerical_checks": [percent_check()],
        "methodology_focus": ["baselines"],
        "cross_paper_targets": ["f0"],
        "needs_external": False,
        "external_rationale": "",
        "suggested_queries": [],
        "limitations": ["synthetic fixture"],
    }
    base.update(over)
    return AuditPlan.model_validate(base)


class FakeSpark:
    def __init__(self, script, allowed=None):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self.allowed = allowed

    def complete_json(self, messages, schema, *, node, thread_id, **kwargs):
        if self.allowed is not None and node not in self.allowed:
            raise AssertionError(f"spark call to '{node}' not allowed here")
        self.calls.append(node)
        if node not in self.script or not self.script[node]:
            raise AssertionError(f"unexpected spark call in node '{node}'")
        item = self.script[node].pop(0)
        if isinstance(item, Exception):
            raise item
        tel = TelemetryRecord(
            timestamp="2026-11-03T00:00:00+00:00", thread_id=thread_id,
            node=node, latency_ms=5, success=True)
        return item, tel


def critic(verdict, **over):
    base = {"verdict": verdict, "summary": "s"}
    base.update(over)
    return CriticVerdict.model_validate(base)


class AuditGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "proj"
        self.analysis = analysis_fixture()
        proj = pr.Project(research_question="Q?")
        proj.papers.append(pr.PaperEntry(
            paper_id="p0", title="P0", filename="p0.pdf", added_order=1,
            n_objects=1, n_localized=0))
        proj.papers.append(pr.PaperEntry(
            paper_id="p1", title="P1", filename="p1.pdf", added_order=2,
            n_objects=3, n_localized=2))
        proj.findings.append(pr.Finding(
            id="f0", paper_id="p0", statement="Prior result.",
            evidence="Table 1, p.2", page=2, object_ids=[], confidence=0.8))
        proj.findings.append(pr.Finding(
            id="f1", paper_id="p1", statement="New result.",
            evidence="Table 2, p.3", page=3, object_ids=[], confidence=0.8))
        pr.save_project(self.project_dir, proj)
        self.paper = next(p for p in proj.papers if p.paper_id == "p1")

    def run_graph(self, fake_spark, fake_openalex=None, thread_id="a-t1",
                  claim_id="claim-1", external=False):
        from agent_graph import load_run_record, save_run_record

        record = RunRecord(thread_id=thread_id, project_id="proj", kind="audit",
                           paper_id="p1", claim_id=claim_id, research_question="Q?",
                           status="running", created_at="x", paper=self.paper.model_dump(),
                           analysis_path="a.json")
        save_run_record(self.project_dir, record)
        ctx = AuditContext(project_dir=self.project_dir, analysis=self.analysis,
                           paper=self.paper, claim_id=claim_id, pdf_path=None,
                           spark=fake_spark, openalex=fake_openalex or FakeOpenAlex({}),
                           external_enabled=external)
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(ag.agent_dir(self.project_dir) / "checkpoints.sqlite")) as cp:
            graph = ag.build_audit_graph(cp)
            result = graph.invoke(
                ag.initial_audit_state("proj", thread_id, "p1", claim_id, external), config)
            snapshot = graph.get_state(config)
        return result, snapshot, load_run_record(self.project_dir, thread_id)

    def resume(self, thread_id, choice, spark, openalex=None, free_text=""):
        from agent_graph import load_run_record, resume_command

        record = load_run_record(self.project_dir, thread_id)
        ctx = AuditContext(project_dir=self.project_dir, analysis=self.analysis,
                           paper=self.paper, claim_id=record.claim_id, pdf_path=None,
                           spark=spark, openalex=openalex or FakeOpenAlex({}),
                           external_enabled=False)
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(ag.agent_dir(self.project_dir) / "checkpoints.sqlite")) as cp:
            graph = ag.build_audit_graph(cp)
            result = graph.invoke(resume_command(choice, free_text), config)
        return result, load_run_record(self.project_dir, thread_id)

    def happy_spark(self, **over):
        script = {
            "classify_claim": [ClaimClassification.model_validate(
                {"types": ["empirical_benchmark", "numerical"], "rationale": "r"})],
            "plan_audit_checks": [plan_fixture()],
            "methodology_assessment": [MethodologyAssessment.model_validate({"findings": []})],
            "compare_project_evidence": [CrossPaperAssessment.model_validate({
                "comparisons": [{
                    "comparability": "DIRECTLY_COMPARABLE", "finding_ref": "f0",
                    "paper_id": "p0", "relationship": "corroborates",
                    "detail": "same metric", "evidence": "Table 1"}]})],
            "scientific_critic": [critic("SUPPORTED")],
        }
        script.update(over.get("script", {}))
        return FakeSpark(script, allowed=over.get("allowed"))

    # -- classification + evidence -------------------------------------------

    def test_classification_types_stored(self):
        result, _, _ = self.run_graph(self.happy_spark(), thread_id="a-cls")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["claim_types"], ["empirical_benchmark", "numerical"])

    def test_evidence_bundle_reports_gaps_without_pdf(self):
        bundle = ag.collect_evidence_bundle(self.analysis, "claim-1", None, "brief")
        self.assertEqual(bundle["claim"]["id"], "claim-1")
        self.assertEqual(len(bundle["objects"]), 3, "claim + 2 related objects")
        self.assertTrue(any("PDF" in g for g in bundle["gaps"]))
        self.assertTrue(any("section-level" in g for g in bundle["gaps"]))

    def test_evidence_bundle_extracts_real_pdf_pages(self):
        import fitz

        pdf = Path(self.tmp.name) / "doc.pdf"
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page()
            page.insert_text((72, 72), f"page-{i + 1} Table 1 shows 4.7 vs 7.2")
        doc.save(pdf)
        doc.close()
        texts, gaps = ag.extract_pdf_pages(pdf, [2, 9])
        self.assertEqual(len(texts), 1)
        self.assertIn("4.7 vs 7.2", texts[0]["text"])
        self.assertTrue(any("out of range" in g for g in gaps))

    # -- numerical executors ---------------------------------------------------

    def test_percentage_improvement_pass_and_direction(self):
        rec = ag.execute_numerical_check(percent_check())
        self.assertEqual(rec["status"], "PASSED")
        # wrong direction flips the verdict
        rec = ag.execute_numerical_check(percent_check(metric_direction="higher"))
        self.assertEqual(rec["status"], "DISCREPANCY")

    def test_incorrect_arithmetic_detected(self):
        rec = ag.execute_numerical_check(percent_check(expected_value=99.0))
        self.assertEqual(rec["status"], "DISCREPANCY")
        self.assertIn("30", rec["actual_computation_or_reasoning"])

    def test_rounding_tolerance(self):
        rec = ag.execute_numerical_check(percent_check(expected_value=30.2))
        self.assertEqual(rec["status"], "PASSED", "within abs_tol=0.5")
        rec = ag.execute_numerical_check(
            percent_check(expected_value=31.0, abs_tol=0.01, rel_tol=0.0))
        self.assertEqual(rec["status"], "DISCREPANCY")

    def test_paper_precision_rounding_passes(self):
        # live-audit cases: quoted paper precision must not flag discrepancies
        cases = [
            ({"check_type": "percentage_improvement", "metric_direction": "lower",
              "values": {"baseline": 7.6, "proposed": 7.0},
              "source_snippet": "baseline 7.6 proposed 7.0", "expected_value": 7.89}),
            ({"check_type": "ratio", "metric_direction": "na",
              "values": {"a": 1.531915, "b": 1.0},
              "source_snippet": "a 1.531915 b 1", "expected_value": 1.5319}),
            ({"check_type": "error_reduction", "metric_direction": "na",
              "values": {"before": 34.3, "after": 4.7},
              "source_snippet": "before 34.3 after 4.7", "expected_value": 86.3}),
        ]
        for i, kw in enumerate(cases):
            rec = ag.execute_numerical_check({"check_id": f"p{i}", **kw,
                                              "description": "rounding"})
            self.assertEqual(rec["status"], "PASSED", rec)

    def test_empty_check_ids_get_stable_unique_ids(self):
        # live planner leaves check_id empty; execution must assign
        # unique ids (UI keys + review summaries depend on them)
        from agent_graph import save_run_record

        record = RunRecord(thread_id="a-ids", project_id="proj", kind="audit",
                           paper_id="p1", claim_id="claim-1", research_question="Q?",
                           status="running", created_at="x", paper=self.paper.model_dump(),
                           analysis_path="a.json")
        save_run_record(self.project_dir, record)
        ctx = AuditContext(project_dir=self.project_dir, analysis=self.analysis,
                           paper=self.paper, claim_id="claim-1", pdf_path=None,
                           spark=FakeSpark({}), openalex=FakeOpenAlex({}),
                           external_enabled=False)
        config = {"configurable": {"thread_id": "a-ids", "ctx": ctx}}
        plan = plan_fixture(numerical_checks=[
            percent_check(check_id=""), percent_check(check_id="")]).model_dump()
        out = ag.a_execute_checks({"thread_id": "a-ids", "audit_plan": plan}, config)
        ids = [r["check_id"] for r in out["numerical_results"]]
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(ids), "ids must be non-empty")
        self.assertEqual(len(set(ids)), 2, "ids must be unique")

    def test_best_method_lower_is_better(self):
        rec = ag.execute_numerical_check({
            "check_id": "b", "check_type": "best_method", "description": "d",
            "metric_direction": "lower",
            "values": {"ours": 4.7, "base": 7.2},
            "expected_label": "ours",
            "source_snippet": "ours 4.7 base 7.2", "evidence_refs": []})
        self.assertEqual(rec["status"], "PASSED")
        rec = ag.execute_numerical_check({
            "check_id": "b", "check_type": "best_method", "description": "d",
            "metric_direction": "higher",
            "values": {"ours": 4.7, "base": 7.2},
            "expected_label": "ours",
            "source_snippet": "ours 4.7 base 7.2", "evidence_refs": []})
        self.assertEqual(rec["status"], "DISCREPANCY")

    def test_ranking_and_aggregate_and_ratio(self):
        rank = ag.execute_numerical_check({
            "check_id": "r", "check_type": "ranking", "description": "d",
            "metric_direction": "lower", "values": {"a": 1.0, "b": 2.0},
            "expected_order": ["a", "b"],
            "source_snippet": "a 1.0 b 2.0", "evidence_refs": []})
        self.assertEqual(rank["status"], "PASSED")
        agg = ag.execute_numerical_check({
            "check_id": "g", "check_type": "aggregate", "description": "d",
            "metric_direction": "na", "values": {"x": 2.0, "y": 4.0},
            "expected_value": 3.0, "op_detail": "mean",
            "source_snippet": "x 2.0 y 4.0 mean 3.0", "evidence_refs": []})
        self.assertEqual(agg["status"], "PASSED")
        ratio = ag.execute_numerical_check({
            "check_id": "t", "check_type": "ratio", "description": "d",
            "metric_direction": "na", "values": {"a": 7.2, "b": 2.4},
            "expected_value": 3.0,
            "source_snippet": "7.2 / 2.4 = 3.0", "evidence_refs": []})
        self.assertEqual(ratio["status"], "PASSED")

    def test_untraceable_values_not_evaluated(self):
        rec = ag.execute_numerical_check(percent_check(source_snippet="no numbers here"))
        self.assertEqual(rec["status"], "NOT_EVALUATED")
        self.assertIn("not found", rec["actual_computation_or_reasoning"])

    def test_unknown_operation_not_evaluated(self):
        rec = ag.execute_numerical_check(percent_check(check_type="telepathy"))
        self.assertEqual(rec["status"], "NOT_EVALUATED")

    def test_lenient_plan_parsing(self):
        from audit_graph import AuditPlan, NumericalCheckProposal

        c = NumericalCheckProposal.model_validate(
            percent_check(metric_direction="lower is better", expected_value="30%"))
        self.assertEqual(c.metric_direction, "lower")
        self.assertEqual(c.expected_value, 30.0)
        p = AuditPlan.model_validate(plan_fixture().model_dump()
                                     | {"limitations": "one long string"})
        self.assertEqual(p.limitations, ["one long string"])
        p = AuditPlan.model_validate(
            plan_fixture().model_dump()
            | {"methodology_focus": "compare metrics",
               "cross_paper_targets": "f0",
               "suggested_queries": "one query"})
        self.assertEqual(p.methodology_focus, ["compare metrics"])
        self.assertEqual(p.cross_paper_targets, ["f0"])
        self.assertEqual(p.suggested_queries, ["one query"])

    def test_null_critic_question_parses_with_review_fallback(self):
        # live4: model returned human_question.question=null; must not fail closed
        from audit_graph import build_audit_review_request

        v = CriticVerdict.model_validate({
            "verdict": "AMBIGUOUS_REQUIRES_HUMAN", "summary": "metric dispute",
            "human_question": {"question": None, "investigator_position": None,
                               "critic_concern": None, "options": None},
            "required_fixes": "soften wording",
        })
        self.assertEqual(v.human_question.question, "")
        self.assertEqual(v.required_fixes, ["soften wording"])
        req = build_audit_review_request({
            "critic_assessment": v.model_dump(), "numerical_results": [],
            "claim_statement": "c", "claim_types": [], "object_refs": []})
        self.assertTrue(req.question, "fallback question must be non-empty")
        self.assertTrue(req.options)

    def test_failed_plan_ends_run_without_executing(self):
        from spark_open import SparkOpenError

        full = self.happy_spark()
        full.script["plan_audit_checks"] = [SparkOpenError("bad schema")]
        result, _, record = self.run_graph(full, thread_id="a-planfail")
        self.assertEqual(result["status"], "failed")
        nodes = [t.node for t in record.transitions]
        self.assertNotIn("execute_numerical_checks", nodes)

    # -- methodology + cross-paper ---------------------------------------------

    def test_methodology_schema_preserved(self):
        full = self.happy_spark()
        full.script["methodology_assessment"] = [MethodologyAssessment.model_validate({
            "findings": [{
                "severity": "MISSING_INFORMATION", "category": "controls",
                "finding": "no ablation", "evidence": "methods silent",
                "evidence_refs": ["eq1"]}]})]
        result, _, _ = self.run_graph(full, thread_id="a-meth")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["methodology_findings"][0]["severity"], "MISSING_INFORMATION")

    def test_incomparable_benchmark_preserved(self):
        full = self.happy_spark()
        full.script["compare_project_evidence"] = [CrossPaperAssessment.model_validate({
            "comparisons": [{
                "comparability": "NOT_DIRECTLY_COMPARABLE", "finding_ref": "f0",
                "paper_id": "p0", "relationship": "different datasets",
                "detail": "FAUST vs SCAPE", "evidence": "Table 1"}]})]
        result, _, _ = self.run_graph(full, thread_id="a-incomp")
        comp = result["cross_paper"][0]
        self.assertEqual(comp["comparability"], "NOT_DIRECTLY_COMPARABLE")

    def test_invalid_source_refs_dropped_and_recorded(self):
        full = self.happy_spark()
        full.script["compare_project_evidence"] = [CrossPaperAssessment.model_validate({
            "comparisons": [
                {"comparability": "DIRECTLY_COMPARABLE", "finding_ref": "f0",
                 "paper_id": "p0", "relationship": "ok", "detail": "d", "evidence": "e"},
                {"comparability": "DIRECTLY_COMPARABLE", "finding_ref": "f99",
                 "paper_id": "p0", "relationship": "fake", "detail": "d", "evidence": "e"},
            ]})]
        result, _, record = self.run_graph(full, thread_id="a-badref")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["cross_paper"]), 1)
        self.assertTrue(any("f99" in e for e in record.validation_failures))

    # -- external search ---------------------------------------------------------

    def external_spark(self, queries=("q1", "q2")):
        plan = plan_fixture(needs_external=True, external_rationale="r",
                            suggested_queries=list(queries))
        full = self.happy_spark()
        full.script["plan_audit_checks"] = [plan]
        return full

    def test_corpus_only_makes_no_external_calls(self):
        ox = FakeOpenAlex({})
        result, _, _ = self.run_graph(self.happy_spark(), ox, thread_id="a-off")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(ox.queries, [])
        self.assertEqual(result["external_sources"], [])

    def test_external_budgets_enforced(self):
        ox = FakeOpenAlex({
            "q1": [fake_record(f"https://openalex.org/W{i}", cited_by=i) for i in range(8)],
            "q2": [fake_record(f"https://openalex.org/X{i}", cited_by=i) for i in range(8)],
            "q3": [fake_record("https://openalex.org/Z9")],
        })
        spark = self.external_spark(("q1", "q2", "q3"))
        result, _, _ = self.run_graph(spark, ox, thread_id="a-budget", external=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["external_queries"]), 2, "at most 2 queries")
        self.assertLessEqual(len(ox.queries), 2)
        self.assertLessEqual(len(result["external_sources"]), 3, "at most 3 selected")

    def test_external_unavailable_fails_soft(self):
        ox = FakeOpenAlex({"q1": OpenAlexError("HTTP 503")})
        spark = self.external_spark(("q1",))
        result, _, _ = self.run_graph(spark, ox, thread_id="a-down", external=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["external_sources"], [])
        self.assertTrue(any("failed gracefully" in n for n in result["external_notes"]))

    def test_abstract_only_labeling_and_no_fulltext_claims(self):
        ox = FakeOpenAlex({"q1": [fake_record("https://openalex.org/W1", abstract="some text"),
                                  fake_record("https://openalex.org/W2", abstract="")]})
        spark = self.external_spark(("q1",))
        result, _, _ = self.run_graph(spark, ox, thread_id="a-abs", external=True)
        self.assertEqual(len(result["external_sources"]), 1, "empty abstracts dropped")
        src = result["external_sources"][0]
        self.assertEqual(src["evidence_level"], "ABSTRACT_ONLY")
        self.assertIn("ABSTRACT-LEVEL", src["evidence_label"])
        blob = json.dumps(result)
        self.assertNotIn("FULL_TEXT", blob)

    # -- critic / revision / human review -----------------------------------------

    def test_revision_path_then_finalize(self):
        full = self.happy_spark()
        full.script["scientific_critic"] = [critic(
            "REVISION_REQUIRED", required_fixes=["soften wording"])]
        full.script["bounded_revision"] = [AuditRevision.model_validate(
            {"corrected_methodology": [], "corrected_cross_paper": [],
             "notes": "softened"})]
        full.script["scientific_critic_rereview"] = [critic("SUPPORTED")]
        result, _, record = self.run_graph(full, thread_id="a-rev")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["revision_count"], 1)
        nodes = [t.node for t in record.transitions]
        self.assertIn("bounded_revision", nodes)

    def test_revision_bound_escalates_to_human(self):
        full = self.happy_spark()
        full.script["scientific_critic"] = [critic(
            "REVISION_REQUIRED", required_fixes=["fix"])]
        full.script["bounded_revision"] = [AuditRevision.model_validate(
            {"corrected_methodology": [], "corrected_cross_paper": [], "notes": "n"})]
        full.script["scientific_critic_rereview"] = [critic(
            "REVISION_REQUIRED", required_fixes=["still bad"])]
        result, snapshot, record = self.run_graph(full, thread_id="a-rev2")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(snapshot.interrupts)
        self.assertEqual(record.human_review_request["kind"], "audit")
        result2, _ = self.resume("a-rev2", "Accept audit as-is",
                                 FakeSpark({}, allowed=[]), FakeOpenAlex({}))
        self.assertEqual(result2["status"], "completed")

    def test_ambiguous_critic_interrupts_and_resumes_without_repeats(self):
        full = self.happy_spark()
        full.script["scientific_critic"] = [critic(
            "AMBIGUOUS_REQUIRES_HUMAN", summary="metric dispute",
            human_question={"question": "Which metric?",
                            "investigator_position": "A",
                            "critic_concern": "B",
                            "affected_connection_indexes": [],
                            "options": ["Accept audit as-is", "Discard audit"]})]
        result, snapshot, _ = self.run_graph(full, thread_id="a-hit")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(snapshot.interrupts)
        pre = list(full.calls)
        resume_spark = FakeSpark({}, allowed=[])
        result2, record2 = self.resume("a-hit", "Accept audit as-is",
                                       resume_spark, FakeOpenAlex({}))
        self.assertEqual(result2["status"], "completed")
        self.assertEqual(resume_spark.calls, [], "zero model calls after resume")
        self.assertEqual(pre, ["classify_claim", "plan_audit_checks",
                               "methodology_assessment", "compare_project_evidence",
                               "scientific_critic"])
        self.assertIsNotNone(record2.human_review_response)

    def test_discard_leaves_project_untouched(self):
        full = self.happy_spark()
        full.script["scientific_critic"] = [critic("AMBIGUOUS_REQUIRES_HUMAN")]
        self.run_graph(full, thread_id="a-disc")
        result2, _ = self.resume("a-disc", "Discard audit",
                                 FakeSpark({}, allowed=[]), FakeOpenAlex({}))
        self.assertEqual(result2["status"], "discarded")
        self.assertEqual(pr.load_project(self.project_dir).audits, [])

    # -- report / attach ------------------------------------------------------------

    def test_attach_and_report_contents(self):
        result, _, _ = self.run_graph(self.happy_spark(), thread_id="a-attach")
        self.assertEqual(result["status"], "completed")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.audits), 1)
        a = project.audits[0]
        self.assertEqual(a.audit_id, "a1")
        self.assertEqual(a.claim_id, "claim-1")
        self.assertEqual(a.claim_type, ["empirical_benchmark", "numerical"])
        self.assertEqual(a.checks[0]["status"], "PASSED")
        self.assertEqual(a.overall_assessment, "substantiated within audited evidence")
        self.assertTrue((ag.audit_thread_dir(self.project_dir, "a-attach") / "audit.json").is_file())

    def test_duplicate_attach_prevented(self):
        self.run_graph(self.happy_spark(), thread_id="a-dup")
        import agent_graph as _ag

        ctx = AuditContext(project_dir=self.project_dir, analysis=self.analysis,
                           paper=self.paper, claim_id="claim-1", pdf_path=None,
                           spark=self.happy_spark(), openalex=FakeOpenAlex({}))
        report = json.loads(
            (ag.audit_thread_dir(self.project_dir, "a-dup") / "audit.json").read_text())
        state = ag.initial_audit_state("proj", "a-dup", "p1", "claim-1")
        state["audit_report"] = report
        ag.a_attach(state, {"configurable": {"thread_id": "a-dup", "ctx": ctx}})
        audits = pr.load_project(self.project_dir).audits
        self.assertEqual(len(audits), 1)
        self.assertIsNotNone(_ag.load_run_record(self.project_dir, "a-dup"))

    def test_audit_statuses_distinct_from_computational(self):
        computational = {"supported", "not_supported", "inconclusive",
                         "execution_failed", "not_testable"}
        self.assertTrue(set(ag.OVERALL_ASSESSMENTS).isdisjoint(computational))

    def test_overall_assessment_cases(self):
        f = ag.compute_overall_assessment
        self.assertEqual(f([{"status": "PASSED"}], [], [], [])[0],
                         "substantiated within audited evidence")
        self.assertEqual(f([{"status": "DISCREPANCY"}], [], [], [])[0],
                         "discrepancies identified")
        self.assertEqual(f([], [{"severity": "OBSERVED_PROBLEM"}], [], [])[0],
                         "discrepancies identified")
        self.assertEqual(
            f([{"status": "PASSED"}], [{"severity": "POTENTIAL_CONCERN"}], [], [])[0],
            "partially substantiated")
        self.assertEqual(f([], [], [], ["gap"])[0], "insufficient evidence")
        self.assertEqual(f([{"status": "NOT_EVALUATED"}], [], [], [])[0],
                         "insufficient evidence")
        self.assertEqual(
            f([{"status": "NOT_EVALUATED"}],
              [{"severity": "POTENTIAL_CONCERN"}], [], [])[0],
            "inconclusive on audited evidence")

    def test_m1_to_m6_data_preserved(self):
        before = pr.load_project(self.project_dir)
        n_findings, n_papers = len(before.findings), len(before.papers)
        self.run_graph(self.happy_spark(), thread_id="a-preserve")
        after = pr.load_project(self.project_dir)
        self.assertEqual(len(after.findings), n_findings)
        self.assertEqual(len(after.papers), n_papers)
        self.assertEqual(after.findings[0].statement, "Prior result.")
        self.assertEqual(len(after.audits), 1)

    def test_stage_audits(self):
        import agent

        self.run_graph(self.happy_spark(), thread_id="a-stage")
        web_dir = Path(self.tmp.name) / "web"
        agent.stage_audits(self.project_dir, web_dir)
        staged = json.loads(
            (web_dir / "public" / "data" / "agent" / "audits.json").read_text())
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["overall_assessment"],
                         "substantiated within audited evidence")
        self.assertTrue((web_dir / "public" / "data" / "agent" / "audits"
                         / "a-stage" / "audit.json").is_file())


if __name__ == "__main__":
    unittest.main()
