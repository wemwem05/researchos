"""Offline tests for the Milestone 5 agent graph (no API key needed).

A scripted FakeSpark test double stands in for Muse Spark Open; every test
asserts graph behavior, deterministic gates, persistence, and resume.

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_graph as ag
import project as pr
import spark_open
from agent_graph import (
    CriticVerdict,
    HumanQuestionDraft,
    InvestigatorProposal,
    RunContext,
)
from analyze_paper import AnalysisResult
from langgraph.checkpoint.sqlite import SqliteSaver
from spark_open import SparkOpenError, TelemetryRecord


def analysis_object(obj_id, otype="equation", score=0.9):
    return {
        "id": obj_id,
        "type": otype,
        "page": 1,
        "label": f"Label {obj_id}",
        "title": "T",
        "what_it_is": "x",
        "why_it_is_here": "x",
        "claim_supported": "x",
        "limitations": "x",
        "paper_importance": 0.8,
        "paper_importance_reason": "core",
        "research_relevance": "x",
        "research_relevance_score": score,
        "research_relevance_reason": "y",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.1",
    }


def analysis_fixture():
    return AnalysisResult.model_validate(
        {
            "paper_title": "New Paper",
            "research_question": "Q?",
            "objects": [
                analysis_object("eq1", "equation"),
                analysis_object("fig1", "figure", 0.1),
                analysis_object("tab1", "table", 0.5),
                analysis_object("c1", "claim", 0.5),
            ],
        }
    )


def paper_entry(paper_id="p1", order=1):
    return pr.PaperEntry(
        paper_id=paper_id,
        title="Paper",
        filename="p.pdf",
        added_order=order,
        n_objects=4,
        n_localized=3,
    )


def project_fixture(with_prior=True):
    proj = pr.Project(research_question="Can X improve Y?")
    if with_prior:
        proj.papers.append(paper_entry("p1", 1))
        proj.findings.append(
            pr.Finding(id="f1", paper_id="p1", statement="Prior result.",
                       evidence="Table 1, p.2", page=2, object_ids=[],
                       confidence=0.8)
        )
        proj.contributions.append(
            pr.PaperContribution(paper_id="p1", what_it_adds="baseline",
                                 stance_toward_prior="first paper",
                                 overlap_with_direction="partial")
        )
    return proj


def valid_proposal(**over):
    base = {
        "findings": [
            {
                "statement": "New cost improves correspondence.",
                "evidence": "Table 1, p.3",
                "page": 3,
                "object_ids": ["eq1"],
                "confidence": 0.8,
            }
        ],
        "connections": [
            {
                "from_ref": "new[0]",
                "to_ref": "f1",
                "type": "extends",
                "reason": "Builds on the prior result.",
                "confidence": 0.7,
            }
        ],
        "contribution": {
            "what_it_adds": "A new cost.",
            "stance_toward_prior": "Supports and extends f1.",
            "overlap_with_direction": "Directly on-question.",
            "new_open_questions": [],
        },
        "open_questions": [{"text": "Does it generalize?", "finding_refs": ["new[0]"]}],
        "novelty_risks": [
            {
                "direction": "Within the analyzed papers, the new-cost direction overlaps f1.",
                "overlapping_finding_refs": ["new[0]"],
                "note": "Both papers explore alternative costs on similar data.",
            }
        ],
        "methodological_differences": ["Different dataset split."],
        "what_changed": "The new paper strengthens the case for alternative costs.",
    }
    base.update(over)
    return InvestigatorProposal.model_validate(base)


def critic_verdict(verdict, **over):
    base = {"verdict": verdict, "summary": "Reviewed."}
    base.update(over)
    return CriticVerdict.model_validate(base)


class FakeSpark:
    """Scripted Spark Open double: script maps node name -> responses/errors."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def complete_json(self, messages, schema, *, node, thread_id):
        self.calls.append({"node": node, "thread_id": thread_id,
                           "schema": schema.__name__})
        if node not in self.script or not self.script[node]:
            raise AssertionError(f"unexpected spark call in node '{node}'")
        item = self.script[node].pop(0)
        if isinstance(item, Exception):
            raise item
        tel = TelemetryRecord(
            timestamp="2026-11-03T00:00:00+00:00",
            provider=spark_open.PROVIDER,
            model=spark_open.MODEL_ID,
            thread_id=thread_id,
            node=node,
            latency_ms=5,
            success=True,
        )
        return item, tel

    def calls_to(self, node):
        return [c for c in self.calls if c["node"] == node]


class RefusingSpark:
    """Proves resume performs zero model calls."""

    def __init__(self):
        self.calls = []

    def complete_json(self, *args, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("model called after human review")


BASE_STATE_KEYS = [
    "project_id", "thread_id", "research_question", "paper_id",
    "existing_findings", "new_paper_findings", "candidate_connections",
    "investigator_assessment", "critic_assessment", "revision_result",
    "open_questions", "novelty_risks", "needs_human_review",
    "human_review_request", "human_review_response", "status",
    "retry_count", "telemetry",
]


class AgentGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "proj"
        self.paper = paper_entry("p2", 2)
        self.analysis = analysis_fixture()

    def write_project(self, with_prior=True):
        pr.save_project(self.project_dir, project_fixture(with_prior))
        return self.project_dir

    def run_graph(self, fake, thread_id="t1", with_prior=True, config_extra=None):
        self.write_project(with_prior)
        record = ag.RunRecord(thread_id=thread_id, project_id="proj", paper_id="p2",
                              research_question="Can X improve Y?",
                              status="running", created_at="2026-11-03T00:00:00+00:00",
                              paper=self.paper.model_dump(), analysis_path="a.json")
        ag.save_run_record(self.project_dir, record)
        ctx = RunContext(project_dir=self.project_dir, analysis=self.analysis,
                         paper=self.paper, spark=fake)
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        if config_extra:
            config["configurable"].update(config_extra)
        with SqliteSaver.from_conn_string(str(ag.checkpoints_path(self.project_dir))) as cp:
            graph = ag.build_graph(cp)
            result = graph.invoke(ag.initial_state("proj", thread_id, "p2"), config)
            snapshot = graph.get_state(config)
        return result, snapshot, ag.load_run_record(self.project_dir, thread_id)

    # -- client configuration ------------------------------------------------

    def test_spark_client_uses_exact_hackathon_model(self):
        self.assertEqual(spark_open.MODEL_ID, "meta-models/muse-spark-open-1026")
        self.assertNotEqual(spark_open.MODEL_ID, "meta-models/muse-spark-open")
        self.assertEqual(spark_open.BASE_URL, "https://inference.baseten.co/v1")
        self.assertEqual(spark_open.PROVIDER, "Baseten")
        self.assertEqual(spark_open.API_KEY_ENV_VAR, "BASETEN_API_KEY")
        client = spark_open.SparkOpenClient(api_key="test-key-not-a-secret")
        self.assertEqual(client.model_id, "meta-models/muse-spark-open-1026")

    def test_graph_state_has_required_fields(self):
        state = ag.initial_state("proj", "t1", "p2")
        for key in BASE_STATE_KEYS:
            self.assertIn(key, state, f"state missing '{key}'")

    # -- deterministic validation --------------------------------------------

    def vctx(self):
        return ag.ValidationContext(paper_ids={"p1", "p2"}, finding_ids={"f1"},
                                    object_ids={"eq1", "fig1", "tab1", "c1"},
                                    is_first_paper=False)

    def test_valid_investigator_output_passes(self):
        self.assertEqual(ag.validate_proposal(valid_proposal(), self.vctx()), [])

    def test_unknown_finding_rejected(self):
        p = valid_proposal()
        p.connections[0].to_ref = "f99"
        errors = ag.validate_proposal(p, self.vctx())
        self.assertTrue(any("f99" in e for e in errors), errors)

    def test_unknown_object_rejected(self):
        p = valid_proposal()
        p.findings[0].object_ids = ["eq-nope"]
        errors = ag.validate_proposal(p, self.vctx())
        self.assertTrue(any("eq-nope" in e for e in errors), errors)

    def test_invalid_connection_type_rejected(self):
        d = valid_proposal().model_dump()
        d["connections"][0]["type"] = "likes"
        errors = ag.validate_proposal(d, self.vctx())
        self.assertTrue(any("likes" in e for e in errors), errors)

    def test_finding_count_bound(self):
        d = valid_proposal().model_dump()
        d["findings"] = d["findings"] * 6
        errors = ag.validate_proposal(d, self.vctx())
        self.assertTrue(any("1..5" in e for e in errors), errors)

    def test_first_paper_must_not_propose_connections(self):
        ctx = ag.ValidationContext(paper_ids={"p2"}, finding_ids=set(),
                                   object_ids={"eq1"}, is_first_paper=True)
        errors = ag.validate_proposal(valid_proposal(), ctx)
        self.assertTrue(any("first paper" in e for e in errors), errors)

    def test_global_novelty_wording_rejected(self):
        p = valid_proposal()
        p.novelty_risks[0].note = "This direction has never been studied before."
        errors = ag.validate_proposal(p, self.vctx())
        self.assertTrue(any("corpus" in e for e in errors), errors)

    def test_confidence_bounds_rejected(self):
        d = valid_proposal().model_dump()
        d["findings"][0]["confidence"] = 1.5
        errors = ag.validate_proposal(d, self.vctx())
        self.assertTrue(any("confidence" in e for e in errors), errors)

    def test_duplicate_ids_rejected(self):
        p = valid_proposal()
        p.findings[0].object_ids = ["eq1", "eq1"]
        errors = ag.validate_proposal(p, self.vctx())
        self.assertTrue(any("duplicate" in e for e in errors), errors)

    def test_malformed_state_rejected(self):
        self.assertTrue(ag.validate_proposal({"nope": 1}, self.vctx()))
        self.assertTrue(ag.validate_proposal([1, 2], self.vctx()))

    # -- clean path ----------------------------------------------------------

    def test_critic_supported_path_completes(self):
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("SUPPORTED")],
        })
        result, _, record = self.run_graph(fake)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.final_status, "completed")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.papers), 2)
        self.assertTrue(any(f.paper_id == "p2" for f in project.findings))
        self.assertEqual(len(project.connections), 1)
        nodes = [t.node for t in record.transitions]
        for expected in ("load_project_context", "investigator", "validate",
                         "critic", "decision_gate", "finalize",
                         "apply_final_decision", "update_project_state"):
            self.assertIn(expected, nodes, nodes)

    def test_invalid_investigator_output_rejected_without_mutation(self):
        bad = valid_proposal()
        bad.connections[0].to_ref = "f99"
        fake = FakeSpark({
            "investigator": [bad],
            "validate_repair": [bad],  # repair cannot fix it either
        })
        result, _, record = self.run_graph(fake)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(record.validation_failures)
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.papers), 1, "failed run must not mutate project")
        self.assertEqual(len(project.findings), 1)

    def test_no_silent_fallback_on_spark_failure(self):
        tel = TelemetryRecord(
            timestamp="2026-11-03T00:00:00+00:00", thread_id="t1",
            node="investigator", latency_ms=9, success=False,
            error="boom", provider=spark_open.PROVIDER, model=spark_open.MODEL_ID)
        fake = FakeSpark({
            "investigator": [SparkOpenError("Spark Open down. No fallback model was used.", tel)],
        })
        result, _, record = self.run_graph(fake)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No fallback", record.error or "")
        # failure telemetry is still persisted
        rows = ag.read_telemetry(self.project_dir)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["success"])

    # -- revision path -------------------------------------------------------

    def test_critic_revision_path_then_accept(self):
        fixed = valid_proposal()
        fixed.what_changed = "Corrected after criticism."
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("REVISION_REQUIRED",
                                      required_fixes=["Soften the extends claim."])],
            "revise_once": [fixed],
            "critic_after_revision": [critic_verdict("SUPPORTED")],
        })
        result, _, record = self.run_graph(fake)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(fake.calls_to("revise_once")), 1)
        self.assertIn("revise_once", record.revision_path)
        self.assertTrue(any("REVISION_REQUIRED" in e for e in record.revision_path))

    def test_revision_is_bounded_to_one_pass(self):
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("REVISION_REQUIRED", required_fixes=["Fix."])],
            "revise_once": [valid_proposal()],
            "critic_after_revision": [critic_verdict("REVISION_REQUIRED",
                                                     required_fixes=["Still bad."])],
        })
        result, _, record = self.run_graph(fake)
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(len(fake.calls_to("revise_once")), 1,
                         "at most one automatic revision pass")
        self.assertEqual(record.human_review_request["kind"], "revision_disagreement")

    # -- human review + resume -----------------------------------------------

    def ambiguous_fake(self):
        return FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict(
                "AMBIGUOUS_REQUIRES_HUMAN",
                summary="Datasets differ; comparability unclear.",
                concerns=[{"claim_ref": "connection new[0]", "question": "Comparable?",
                           "detail": "Different splits.", "severity": "blocking"}],
                human_question=HumanQuestionDraft(
                    question="Should these findings be treated as directly comparable?",
                    investigator_position="Yes: same metric family.",
                    critic_concern="Different splits undermine comparison.",
                    affected_connection_indexes=[0],
                ).model_dump(),
            )],
        })

    def test_human_review_interrupt(self):
        result, snapshot, record = self.run_graph(self.ambiguous_fake())
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(result["needs_human_review"])
        self.assertTrue(snapshot.interrupts, "checkpoint must hold the interrupt")
        self.assertTrue(ag.checkpoints_path(self.project_dir).is_file())
        req = record.human_review_request
        self.assertIn("comparable", req["question"])
        self.assertTrue(req["options"])
        self.assertTrue(req["allow_free_text"])
        self.assertIn("evidence_a", req)
        self.assertIn("evidence_b", req)

    def resume(self, thread_id, choice, free_text="", spark=None):
        """Simulate a fresh process: new graph + checkpointer on the same files."""
        record = ag.load_run_record(self.project_dir, thread_id)
        ctx = RunContext(project_dir=self.project_dir, analysis=self.analysis,
                         paper=self.paper, spark=spark or RefusingSpark())
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(ag.checkpoints_path(self.project_dir))) as cp:
            graph = ag.build_graph(cp)
            result = graph.invoke(ag.resume_command(choice, free_text), config)
        return result, ag.load_run_record(self.project_dir, thread_id), record

    def test_resume_after_simulated_restart_accept(self):
        fake = self.ambiguous_fake()
        self.run_graph(fake, thread_id="resume-1")
        result, record, _ = self.resume("resume-1", "Comparable — keep the connection")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(record.human_review_response["choice"],
                         "Comparable — keep the connection")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.connections), 1)

    def test_expensive_nodes_not_repeated_after_resume(self):
        fake = self.ambiguous_fake()
        self.run_graph(fake, thread_id="resume-2")
        before = list(fake.calls)
        refusing = RefusingSpark()
        result, _, _ = self.resume("resume-2", "Comparable — keep the connection",
                                   spark=refusing)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(refusing.calls, [], "resume must make zero model calls")
        self.assertEqual(len([c for c in before if c["node"] == "investigator"]), 1)
        self.assertEqual(len([c for c in before if c["node"] == "critic"]), 1)

    def test_human_discard_leaves_project_untouched(self):
        self.run_graph(self.ambiguous_fake(), thread_id="resume-3")
        result, record, _ = self.resume("resume-3", "Discard run", "Not convincing.")
        self.assertEqual(result["status"], "discarded")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.papers), 1)
        self.assertEqual(len(project.findings), 1)

    def test_human_not_comparable_drops_connection(self):
        self.run_graph(self.ambiguous_fake(), thread_id="resume-4")
        result, _, _ = self.resume("resume-4", "Not comparable — drop the connection")
        self.assertEqual(result["status"], "completed")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.connections), 0)
        self.assertEqual(len(project.findings), 2, "findings still land; only the link drops")

    def test_human_keep_unresolved_adds_open_question(self):
        self.run_graph(self.ambiguous_fake(), thread_id="resume-5")
        result, _, _ = self.resume("resume-5", "Keep unresolved", "Need the authors' split.")
        self.assertEqual(result["status"], "completed")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.connections), 0)
        self.assertTrue(any("Unresolved" in q.text for q in project.open_questions))

    # -- telemetry + audit ---------------------------------------------------

    def test_telemetry_exact_model_and_provider(self):
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("SUPPORTED")],
        })
        self.run_graph(fake)
        rows = ag.read_telemetry(self.project_dir)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["model"], "meta-models/muse-spark-open-1026")
            self.assertEqual(row["provider"], "Baseten")
            self.assertIn("latency_ms", row)
            self.assertIn("node", row)
            self.assertNotIn("api_key", json.dumps(row).lower())
        summary = ag.telemetry_summary(self.project_dir)
        self.assertEqual(summary["model"], "meta-models/muse-spark-open-1026")
        self.assertEqual(summary["calls"], 2)

    def test_api_key_never_serialized(self):
        marker = "sk-test-marker-never-persist-9f8e7d"
        client = spark_open.SparkOpenClient(api_key=marker)

        class KeyedFake(FakeSpark):
            pass

        fake = KeyedFake({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("SUPPORTED")],
        })
        # run the graph while a client holding the marker exists in-process
        assert client.model_id
        self.run_graph(fake)
        for path in self.project_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker, path.read_bytes().decode("utf-8", "ignore"),
                                 f"key material leaked into {path}")

    def test_run_record_audit_trail(self):
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("SUPPORTED")],
        })
        _, _, record = self.run_graph(fake, thread_id="audit-1")
        self.assertEqual(record.thread_id, "audit-1")
        self.assertEqual(record.project_id, "proj")
        self.assertTrue(all(t.at for t in record.transitions))
        self.assertTrue(record.decisions)
        self.assertTrue(record.telemetry)
        self.assertEqual(record.final_status, "completed")

    # -- compatibility -------------------------------------------------------

    def test_existing_milestone4_project_compatibility(self):
        proj = pr.Project(research_question="Q?")
        proj.papers.append(paper_entry("p1", 1))
        proj.findings.append(
            pr.Finding(id="f1", paper_id="p1", statement="A.", evidence="p.1",
                       confidence=0.9))
        proj.findings.append(
            pr.Finding(id="f2", paper_id="p1", statement="B.", evidence="p.2",
                       confidence=0.7))
        for i, ctype in enumerate(pr.CONNECTION_TYPES):
            proj.connections.append(
                pr.Connection(id=f"c{i}", from_ref="f1", to_ref="f2",
                              type=ctype, reason="r", confidence=0.5))
        proj.contributions.append(
            pr.PaperContribution(paper_id="p1", what_it_adds="a",
                                 stance_toward_prior="s",
                                 overlap_with_direction="o"))
        pr.save_project(self.project_dir, proj)
        fake = FakeSpark({
            "investigator": [valid_proposal()],
            "critic": [critic_verdict("SUPPORTED")],
        })
        record = ag.RunRecord(thread_id="compat", project_id="proj", paper_id="p2",
                              research_question="Q?", status="running",
                              created_at="x", paper=self.paper.model_dump(),
                              analysis_path="a.json")
        ag.save_run_record(self.project_dir, record)
        ctx = RunContext(project_dir=self.project_dir, analysis=self.analysis,
                         paper=self.paper, spark=fake)
        config = {"configurable": {"thread_id": "compat", "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(ag.checkpoints_path(self.project_dir))) as cp:
            graph = ag.build_graph(cp)
            result = graph.invoke(ag.initial_state("proj", "compat", "p2"), config)
        self.assertEqual(result["status"], "completed")
        merged = pr.load_project(self.project_dir)  # M4 validator re-checks all refs
        self.assertEqual(len(merged.connections), len(pr.CONNECTION_TYPES) + 1)
        self.assertEqual(len(merged.findings), 3)


if __name__ == "__main__":
    unittest.main()
