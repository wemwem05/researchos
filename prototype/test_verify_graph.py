"""Offline tests for Milestone 6 computational verification (no API keys,
no Modal account needed). Scripted FakeSpark + FakeModal doubles stand in
for Baseten and Modal; every test asserts graph behavior, deterministic
gates, persistence, and resume.

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import project as pr
import verify_graph as vg
from agent_graph import RunRecord
from analyze_paper import AnalysisResult
from langgraph.checkpoint.sqlite import SqliteSaver
from modal_exec import FakeModal, ModalError, fake_modal_run
from spark_open import TelemetryRecord
from verify_graph import (
    ExperimentPlan,
    GeneratedCode,
    Interpretation,
    RepairDiagnosis,
    TestabilityVerdict,
    VerifyContext,
)


def analysis_object(obj_id, otype="equation"):
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
        "research_relevance_score": 0.9,
        "research_relevance_reason": "y",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.1",
    }


def analysis_fixture():
    return AnalysisResult.model_validate(
        {
            "paper_title": "Paper",
            "research_question": "Q?",
            "objects": [
                analysis_object("eq1", "equation"),
                analysis_object("fig1", "figure"),
                analysis_object("tab1", "table"),
                analysis_object("claim-1", "claim"),
            ],
        }
    )


def paper_entry(paper_id="p1", localized_path="prototype/x_localized.json"):
    return pr.PaperEntry(
        paper_id=paper_id, title="Paper", filename="p.pdf",
        added_order=1, n_objects=4, n_localized=3,
        localized_path=localized_path,
    )


def plan_fixture(**over):
    base = {
        "claim": "Sliced Wasserstein is computationally cheaper than entropic OT.",
        "paper_refs": ["p1"],
        "object_refs": ["claim-1", "eq1"],
        "hypothesis": "SW median runtime < Sinkhorn median runtime at n=512.",
        "independent_vars": ["n"],
        "dependent_vars": ["runtime_s", "mem_bytes"],
        "controlled_vars": ["dim", "seeds", "projections"],
        "algorithms": ["sliced_wasserstein", "sinkhorn"],
        "params": {
            "sizes": [256, 512], "dim": 3, "n_projections": 10, "reps": 2,
            "seeds": [7], "warmup_reps": 1,
            "algorithms": ["sliced_wasserstein", "sinkhorn"],
            "sinkhorn_reg": 0.1, "sinkhorn_max_iter": 500, "sinkhorn_tol": 1e-6,
        },
        "success_criteria": [
            {"name": "sw faster", "kind": "runtime_comparison",
             "rule": "median sw < median sinkhorn at n=512",
             "metric": "runtime_s", "algorithm_a": "sliced_wasserstein",
             "algorithm_b": "sinkhorn", "at_n": 512, "op": "lt", "stat": "median"},
            {"name": "sanity", "kind": "sanity", "rule": "all numerical checks pass"},
            {"name": "scaling", "kind": "scaling", "rule": "ratios recorded"},
        ],
        "budget": {"max_input_n": 512, "est_seconds": 60, "note": ""},
        "limitations": ["synthetic data only"],
        "scope_notes": "Tests efficiency motivation, not accuracy.",
    }
    base.update(over)
    return ExperimentPlan.model_validate(base)


def csv_bytes(sizes, algos, reps, runtimes):
    lines = ["n,algorithm,rep,runtime_s,mem_bytes"]
    for n in sizes:
        for a in algos:
            for r in range(reps):
                lines.append(f"{n},{a},{r},{runtimes[(a, n)][r]},1024")
    return ("\n".join(lines) + "\n").encode()


def stdout_ok(sanity_extra=None):
    sanity = {"nonnegative": True, "self_zero": True, "symmetric": True,
              "sinkhorn_converged": True}
    sanity.update(sanity_extra or {})
    doc = {"sanity": sanity, "config_echo": {"sizes": [256, 512]}, "notes": ""}
    return "run starting\nRESULT_JSON: " + json.dumps(doc) + "\n"


SW_FAST = {("sliced_wasserstein", 256): [0.01, 0.02],
           ("sliced_wasserstein", 512): [0.03, 0.04],
           ("sinkhorn", 256): [0.10, 0.12],
           ("sinkhorn", 512): [0.30, 0.34]}
SW_SLOW = {("sliced_wasserstein", 256): [0.50, 0.52],
           ("sliced_wasserstein", 512): [0.90, 0.94],
           ("sinkhorn", 256): [0.10, 0.12],
           ("sinkhorn", 512): [0.30, 0.34]}

CODE_OK = (
    "import numpy\nimport matplotlib\nimport json\n"
    "print('RESULT_JSON: {\"sanity\": {\"a\": true}}')\n"
)
CODE_UNSAFE = "import socket\nprint('hi')\n"
CODE_SYNTAX = "def broken(:\n  pass\n"

PNG_BYTES = b"\x89PNG\r\n\x1a" + b"\x00" * 64


def valid_modal_outcome(runtimes=SW_FAST):
    return fake_modal_run(
        returncode=0, stdout=stdout_ok(),
        artifacts={"/work/results.csv": csv_bytes([256, 512],
                    ["sliced_wasserstein", "sinkhorn"], 2, runtimes),
                   "/work/scaling.png": PNG_BYTES},
    )


def interpretation_fixture(status="supported"):
    return Interpretation.model_validate({
        "status": status,
        "observed": ["median sw 0.035s vs sinkhorn 0.32s at n=512"],
        "supported_conclusions": ["SW faster in tested conditions"] if status == "supported" else [],
        "partial": [],
        "out_of_scope": ["accuracy", "real data"],
        "limitations": ["synthetic only"],
        "scaling_comment": "ratios recorded descriptively",
    })


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


class VerifyGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project_dir = Path(self.tmp.name) / "proj"
        self.analysis = analysis_fixture()
        loc = Path(self.tmp.name) / "loc.json"
        loc.write_text(json.dumps({"analysis": self.analysis.model_dump(),
                                   "localizations": []}))
        self.paper = paper_entry("p1", localized_path=str(loc))
        proj = pr.Project(research_question="Q?")
        proj.papers.append(self.paper)
        pr.save_project(self.project_dir, proj)

    def run_graph(self, fake_spark, fake_modal, thread_id="v-t1", claim_id="claim-1"):
        record = RunRecord(thread_id=thread_id, project_id="proj", kind="verify",
                           paper_id="p1", claim_id=claim_id, research_question="Q?",
                           status="running", created_at="x", paper=self.paper.model_dump(),
                           analysis_path="a.json")
        from agent_graph import save_run_record, load_run_record

        save_run_record(self.project_dir, record)
        ctx = VerifyContext(project_dir=self.project_dir, analysis=self.analysis,
                            paper=self.paper, claim_id=claim_id,
                            spark=fake_spark, modal=fake_modal)
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(vg.agent_dir(self.project_dir) / "checkpoints.sqlite")) as cp:
            graph = vg.build_verify_graph(cp)
            result = graph.invoke(
                vg.initial_verify_state("proj", thread_id, "p1", claim_id), config)
            snapshot = graph.get_state(config)
        return result, snapshot, load_run_record(self.project_dir, thread_id)

    def resume(self, thread_id, choice, spark, modal, free_text=""):
        from agent_graph import load_run_record, resume_command

        ctx = VerifyContext(project_dir=self.project_dir, analysis=self.analysis,
                            paper=self.paper,
                            claim_id=load_run_record(self.project_dir, thread_id).claim_id,
                            spark=spark, modal=modal)
        config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
        with SqliteSaver.from_conn_string(str(vg.agent_dir(self.project_dir) / "checkpoints.sqlite")) as cp:
            graph = vg.build_verify_graph(cp)
            result = graph.invoke(resume_command(choice, free_text), config)
        return result, load_run_record(self.project_dir, thread_id)

    def happy_spark(self, **over):
        script = {
            "classify_testability": [TestabilityVerdict.model_validate(
                {"testable": True, "rationale": "benchmarkable"})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "interpret_result": [interpretation_fixture(over.get("status", "supported"))],
        }
        script.update(over.get("script", {}))
        return FakeSpark(script, allowed=over.get("allowed"))

    # -- claim selection -----------------------------------------------------

    def test_unknown_claim_fails_without_modal_calls(self):
        modal = FakeModal([])
        result, _, record = self.run_graph(self.happy_spark(), modal,
                                           thread_id="v-unknown", claim_id="nope")
        self.assertEqual(result["status"], "failed")
        self.assertIn("nope", record.error or "")
        self.assertEqual(modal.calls, [])

    # -- testability ---------------------------------------------------------

    def test_not_testable_attaches_without_execution(self):
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate(
                {"testable": False, "rationale": "needs human judgment"})],
        })
        modal = FakeModal([])
        result, _, _ = self.run_graph(spark, modal, thread_id="v-nt")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(modal.calls, [])
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.verifications), 1)
        self.assertEqual(project.verifications[0].status, "not_testable")

    def test_data_escalation_takes_precedence_over_not_testable(self):
        # testable=false + needs_unavailable_data=true must still escalate:
        # the researcher decides whether a synthetic proxy is worthwhile.
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate(
                {"testable": False, "rationale": "accuracy needs real data",
                 "needs_unavailable_data": True,
                 "unavailable_detail": "needs FAUST meshes",
                 "suggested_benchmark": "synthetic runtime proxy"})],
        })
        result, _, record = self.run_graph(spark, FakeModal([]), thread_id="v-prec")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertEqual(record.human_review_request["kind"], "verification")
        self.assertIn("FAUST", record.human_review_request["why_stopped"])
        result2, _ = self.resume("v-prec", "Abort verification",
                                 FakeSpark({}, allowed=[]), FakeModal([]))
        self.assertEqual(result2["status"], "discarded")

    def test_proceed_past_data_escalation_uses_execution_outcome(self):
        # testable=false + needs_data, researcher proceeds, execution then
        # fails: status must be execution_failed, never not_testable.
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate(
                {"testable": False, "rationale": "r",
                 "needs_unavailable_data": True, "unavailable_detail": "d"})],
        })
        self.run_graph(spark, FakeModal([]), thread_id="v-prec2")
        modal2 = FakeModal([fake_modal_run(returncode=1, stdout="x\n", stderr="e")
                            for _ in range(4)])
        spark2 = FakeSpark({
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": f"d{i}", "fix_strategy": "f", "code": CODE_OK})
                for i in range(3)],
        })
        result2, _ = self.resume("v-prec2", "Proceed anyway (record limitation)",
                                 spark2, modal2)
        self.assertEqual(result2["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "execution_failed")

    def test_infer_timeout(self):
        from modal_exec import infer_timeout

        self.assertTrue(infer_timeout(-1, 600.3, 600))
        self.assertTrue(infer_timeout(1, 599.0, 600))
        self.assertFalse(infer_timeout(0, 600.3, 600))
        self.assertFalse(infer_timeout(1, 12.0, 600))
        self.assertFalse(infer_timeout(-1, 12.0, 600))

    def test_unavailable_data_escalates_then_proceeds(self):
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate(
                {"testable": True, "rationale": "ok",
                 "needs_unavailable_data": True,
                 "unavailable_detail": "needs FAUST meshes"})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "interpret_result": [interpretation_fixture()],
        })
        modal = FakeModal([valid_modal_outcome()])
        result, snapshot, record = self.run_graph(spark, modal, thread_id="v-data")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertTrue(snapshot.interrupts)
        self.assertEqual(record.human_review_request["kind"], "verification")
        spark2 = FakeSpark({
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "interpret_result": [interpretation_fixture()],
        }, allowed=["experiment_planner", "generate_code", "interpret_result"])
        result2, _ = self.resume("v-data", "Proceed anyway (record limitation)",
                                 spark2, modal)
        self.assertEqual(result2["status"], "completed")
        project = pr.load_project(self.project_dir)
        self.assertIn("FAUST", " ".join(project.verifications[0].limitations))

    # -- happy path ----------------------------------------------------------

    def test_happy_path_supported_and_attached(self):
        modal = FakeModal([valid_modal_outcome()])
        result, _, record = self.run_graph(self.happy_spark(), modal, thread_id="v-happy")
        self.assertEqual(result["status"], "completed")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.verifications), 1)
        ev = project.verifications[0]
        self.assertEqual(ev.id, "v1")
        self.assertEqual(ev.status, "supported")
        self.assertEqual(ev.paper_id, "p1")
        self.assertEqual(ev.claim_id, "claim-1")
        self.assertIn("claim-1", ev.object_refs)
        self.assertTrue(ev.code_sha256)
        self.assertTrue(all(c["met"] for c in ev.criteria if c["evaluated"]))
        nodes = [t.node for t in record.transitions]
        for expected in ("select_claim", "classify_testability", "experiment_planner",
                         "validate_plan", "generate_code", "code_preflight",
                         "modal_execute", "validate_result", "interpret_result",
                         "evidence_report", "attach_to_project"):
            self.assertIn(expected, nodes, nodes)

    def test_artifacts_preserved_on_disk(self):
        modal = FakeModal([valid_modal_outcome()])
        self.run_graph(self.happy_spark(), modal, thread_id="v-art")
        tdir = vg.verify_thread_dir(self.project_dir, "v-art")
        self.assertTrue((tdir / "evidence.json").is_file())
        adir = tdir / "attempt-0"
        for name in ("experiment.py", "results.csv", "scaling.png", "stdout.txt",
                     "stderr.txt", "env.json", "meta.json"):
            self.assertTrue((adir / name).is_file(), name)
        self.assertIn("RESULT_JSON", (adir / "stdout.txt").read_text())
        ev = json.loads((tdir / "evidence.json").read_text())
        self.assertEqual(ev["status"], "supported")
        # every referenced path resolves inside the project dir
        for key in ("code_path", "measurements_path", "plot_path"):
            self.assertTrue((self.project_dir / ev[key]).is_file(), key)

    def test_telemetry_exact_model_and_key_hygiene(self):
        modal = FakeModal([valid_modal_outcome()])
        self.run_graph(self.happy_spark(), modal, thread_id="v-tel")
        from agent_graph import read_telemetry

        rows = read_telemetry(self.project_dir)
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(row["model"], "meta-models/muse-spark-open-1026")
            self.assertEqual(row["provider"], "Baseten")
        blob = "".join(
            p.read_bytes().decode("utf-8", "ignore")
            for p in vg.verify_thread_dir(self.project_dir, "v-tel").rglob("*")
            if p.is_file() and p.suffix != ".png")
        for secret in ("MODAL_TOKEN", "BASETEN_API_KEY", "MODEL_API_KEY", "SECRET"):
            self.assertNotIn(secret, blob)

    # -- budget escalation ---------------------------------------------------

    def over_budget_plan(self):
        return plan_fixture(params={
            "sizes": [1048576], "dim": 3, "n_projections": 10, "reps": 2,
            "seeds": [7], "warmup_reps": 1,
            "algorithms": ["sliced_wasserstein", "sinkhorn"],
            "sinkhorn_reg": 0.1, "sinkhorn_max_iter": 500, "sinkhorn_tol": 1e-6,
        }, success_criteria=[
            {"name": "sw faster", "kind": "runtime_comparison",
             "rule": "median sw < median at big n",
             "metric": "runtime_s", "algorithm_a": "sliced_wasserstein",
             "algorithm_b": "sinkhorn", "at_n": 1048576, "op": "lt", "stat": "median"},
            {"name": "sanity", "kind": "sanity", "rule": "checks pass"},
            {"name": "scaling", "kind": "scaling", "rule": "ratios"},
        ])

    def test_budget_abort_discards_without_execution(self):
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [self.over_budget_plan()],
        })
        modal = FakeModal([])
        result, _, _ = self.run_graph(spark, modal, thread_id="v-budget-abort")
        self.assertEqual(result["status"], "awaiting_human")
        spark2 = FakeSpark({}, allowed=[])
        modal2 = FakeModal([])
        result2, _ = self.resume("v-budget-abort", "Abort verification", spark2, modal2)
        self.assertEqual(result2["status"], "discarded")
        self.assertEqual(modal.calls, [])
        self.assertEqual(modal2.calls, [])
        self.assertEqual(pr.load_project(self.project_dir).verifications, [])

    def test_budget_approve_applies_deterministic_caps(self):
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [self.over_budget_plan()],
        })
        modal = FakeModal([])
        result, _, _ = self.run_graph(spark, modal, thread_id="v-budget-ok")
        self.assertEqual(result["status"], "awaiting_human")
        # clamped plan: sizes=[8192], reps=2, one default comparison at 8192
        csv = csv_bytes([8192], ["sliced_wasserstein", "sinkhorn"], 2, {
            ("sliced_wasserstein", 8192): [0.05, 0.06],
            ("sinkhorn", 8192): [0.50, 0.55]})
        modal2 = FakeModal([fake_modal_run(
            returncode=0, stdout=stdout_ok(),
            artifacts={"/work/results.csv": csv, "/work/scaling.png": PNG_BYTES})])
        spark2 = FakeSpark({
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "interpret_result": [interpretation_fixture()],
        }, allowed=["generate_code", "interpret_result"])
        result2, _ = self.resume("v-budget-ok", "Approve capped experiment", spark2, modal2)
        self.assertEqual(result2["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.params["sizes"], [8192])
        self.assertTrue(any("capped" in (l or "").lower() for l in ev.limitations))
        # pre-interrupt Spark calls were not repeated after resume
        self.assertEqual(spark.calls, ["classify_testability", "experiment_planner"])
        self.assertEqual(spark2.calls, ["generate_code", "interpret_result"])

    # -- unsafe code ---------------------------------------------------------

    def unsafe_spark(self):
        return FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_UNSAFE})],
        })

    def test_unsafe_code_never_executes_on_abort(self):
        modal = FakeModal([])
        result, _, record = self.run_graph(self.unsafe_spark(), modal, thread_id="v-unsafe")
        self.assertEqual(result["status"], "awaiting_human")
        self.assertIn("socket", record.human_review_request["why_stopped"])
        result2, _ = self.resume("v-unsafe", "Abort verification",
                                 FakeSpark({}, allowed=[]), FakeModal([]))
        self.assertEqual(result2["status"], "discarded")
        self.assertEqual(modal.calls, [])

    def test_unsafe_code_autorepair_path(self):
        modal = FakeModal([])
        self.run_graph(self.unsafe_spark(), modal, thread_id="v-repair0")
        modal2 = FakeModal([valid_modal_outcome()])
        spark2 = FakeSpark({
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": "banned import", "fix_strategy": "drop socket",
                 "code": CODE_OK})],
            "generate_code": [],
            "interpret_result": [interpretation_fixture()],
        }, allowed=["diagnose_and_repair", "interpret_result"])
        result2, _ = self.resume("v-repair0", "Auto-repair and retry", spark2, modal2)
        self.assertEqual(result2["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(len(ev.repairs), 1)
        self.assertEqual(ev.status, "supported")

    # -- execution failure + bounded repair ----------------------------------

    def test_exec_failure_repaired_then_supported(self):
        modal = FakeModal([
            fake_modal_run(returncode=1, stdout="boom\n", stderr="traceback"),
            valid_modal_outcome(),
        ])
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": "crash", "fix_strategy": "fix", "code": CODE_OK})],
            "interpret_result": [interpretation_fixture()],
        })
        result, _, _ = self.run_graph(spark, modal, thread_id="v-fix1")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "supported")
        self.assertEqual(len(ev.repairs), 1)
        self.assertEqual(ev.attempts, 2)
        self.assertEqual(len(modal.calls), 2)

    def test_repair_bound_exhausted_marks_execution_failed(self):
        modal = FakeModal([fake_modal_run(returncode=1, stdout="x\n", stderr="e")
                           for _ in range(4)])
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": f"d{i}", "fix_strategy": "f", "code": CODE_OK})
                for i in range(3)],
        })
        result, _, record = self.run_graph(spark, modal, thread_id="v-exhaust")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "execution_failed")
        self.assertEqual(len(ev.repairs), 3)
        self.assertEqual(spark.calls.count("diagnose_and_repair"), 3)
        self.assertEqual(len(modal.calls), 4)
        self.assertNotEqual(ev.status, "supported")

    def test_timeout_triggers_repair_then_completes(self):
        modal = FakeModal([
            fake_modal_run(returncode=-1, stdout="", stderr="killed", timed_out=True),
            valid_modal_outcome(),
        ])
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_OK})],
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": "too slow", "fix_strategy": "shrink", "code": CODE_OK})],
            "interpret_result": [interpretation_fixture()],
        })
        result, _, _ = self.run_graph(spark, modal, thread_id="v-timeout")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "supported")
        self.assertEqual(ev.attempts, 2)

    def test_infra_failure_marks_execution_failed(self):
        modal = FakeModal([ModalError("boom"), ModalError("boom")])
        result, _, _ = self.run_graph(self.happy_spark(), modal, thread_id="v-infra")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "execution_failed")
        self.assertIn("boom", ev.modal.get("infra_error", ""))
        self.assertEqual(len(modal.calls), 2, "one automatic infra retry")

    def test_codegen_failure_ends_run_without_escalation(self):
        from spark_open import SparkOpenError

        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [SparkOpenError("truncated output")],
        })
        modal = FakeModal([])
        result, snapshot, record = self.run_graph(spark, modal, thread_id="v-codefail")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(snapshot.interrupts, "failed runs must not interrupt")
        self.assertNotIn("code_preflight", [t.node for t in record.transitions])
        self.assertNotIn("human_review_requested", [t.node for t in record.transitions])
        self.assertEqual(modal.calls, [])
        self.assertEqual(pr.load_project(self.project_dir).verifications, [])

    def test_persistent_syntax_errors_terminate_as_execution_failed(self):
        spark = FakeSpark({
            "classify_testability": [TestabilityVerdict.model_validate({"testable": True})],
            "experiment_planner": [plan_fixture()],
            "generate_code": [GeneratedCode.model_validate({"code": CODE_SYNTAX})],
            "diagnose_and_repair": [RepairDiagnosis.model_validate(
                {"diagnosis": f"d{i}", "fix_strategy": "f", "code": CODE_SYNTAX})
                for i in range(3)],
        })
        modal = FakeModal([])
        result, _, _ = self.run_graph(spark, modal, thread_id="v-syntax")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "execution_failed")
        self.assertEqual(spark.calls.count("diagnose_and_repair"), 3)
        self.assertEqual(modal.calls, [], "uncompilable code never executes")

    # -- honesty clamp -------------------------------------------------------

    def test_unmet_criteria_clamp_supported_to_inconclusive(self):
        modal = FakeModal([valid_modal_outcome(SW_SLOW)])
        result, _, _ = self.run_graph(self.happy_spark(), modal, thread_id="v-clamp")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        self.assertEqual(ev.status, "inconclusive")
        comp = next(c for c in ev.criteria if c["kind"] == "runtime_comparison")
        self.assertTrue(comp["evaluated"])
        self.assertFalse(comp["met"])

    def test_clamp_unit_cases(self):
        crit_met = [{"evaluated": True, "met": True}]
        crit_unmet = [{"evaluated": True, "met": False}]
        crit_uneval = [{"evaluated": False, "met": False}]
        self.assertEqual(vg.clamp_status("supported", True, crit_met), "supported")
        self.assertEqual(vg.clamp_status("supported", True, crit_unmet), "inconclusive")
        self.assertEqual(vg.clamp_status("supported", True, crit_uneval), "inconclusive")
        self.assertEqual(vg.clamp_status("supported", False, crit_met), "execution_failed")
        self.assertEqual(vg.clamp_status("not_supported", True, crit_unmet), "not_supported")
        self.assertEqual(vg.clamp_status("weird", True, crit_met), "inconclusive")

    def test_no_successful_verdict_without_valid_execution(self):
        # Even if the interpreter proposes "supported", invalid execution clamps.
        for proposed in ("supported", "not_supported", "inconclusive"):
            status = vg.clamp_status(proposed, False, [])
            self.assertNotEqual(status, "supported")
            self.assertEqual(status, "execution_failed")

    # -- validators ----------------------------------------------------------

    def test_preflight_rejects_banned_imports_and_patterns(self):
        self.assertEqual(vg.preflight_code(CODE_OK), [])
        self.assertTrue(any("socket" in e for e in vg.preflight_code(CODE_UNSAFE)))
        self.assertTrue(any("syntax" in e for e in vg.preflight_code(CODE_SYNTAX)))
        self.assertTrue(vg.preflight_code("import numpy\nimport subprocess\n"))
        self.assertTrue(vg.preflight_code("import numpy\npassword = 'x'\nimport os\nos.system('x')\n"))

    def test_validate_plan_requires_rigor_fields(self):
        errors, over = vg.validate_plan(plan_fixture())
        self.assertEqual(errors, [])
        self.assertFalse(over)
        bad = plan_fixture().model_dump()
        bad["params"]["seeds"] = []
        errors, _ = vg.validate_plan(bad)
        self.assertTrue(any("seeds" in e for e in errors))
        bad2 = plan_fixture().model_dump()
        bad2["success_criteria"] = [c for c in bad2["success_criteria"]
                                    if c["kind"] != "sanity"]
        errors, _ = vg.validate_plan(bad2)
        self.assertTrue(any("sanity" in e for e in errors))
        errors, over = vg.validate_plan(self.over_budget_plan())
        self.assertEqual(errors, [])
        self.assertTrue(over)

    def test_result_contract_rejects_bad_csv(self):
        plan = plan_fixture().model_dump()
        rows = csv_bytes([256, 512], ["sliced_wasserstein", "sinkhorn"], 2, SW_FAST)
        errors, sanity, rj, parsed = vg.validate_result_contract(
            plan, 0, False, stdout_ok(), rows, PNG_BYTES)
        self.assertEqual(errors, [])
        self.assertEqual(sanity, [])
        self.assertEqual(len(parsed), 8)
        bad = csv_bytes([256], ["sliced_wasserstein"], 1,
                        {("sliced_wasserstein", 256): [0.1]})
        errors, _, _, _ = vg.validate_result_contract(plan, 0, False, stdout_ok(), bad, PNG_BYTES)
        self.assertTrue(errors)
        errors, _, _, _ = vg.validate_result_contract(plan, 1, False, stdout_ok(), rows, PNG_BYTES)
        self.assertTrue(any("code 1" in e for e in errors))
        errors, _, _, _ = vg.validate_result_contract(plan, 0, True, stdout_ok(), rows, PNG_BYTES)
        self.assertTrue(any("timed out" in e for e in errors))

    def test_sanity_flags_are_findings_not_contract_errors(self):
        plan = plan_fixture().model_dump()
        rows = csv_bytes([256, 512], ["sliced_wasserstein", "sinkhorn"], 2, SW_FAST)
        bad_sanity = stdout_ok({"sinkhorn_converged": False})
        errors, sanity, _, _ = vg.validate_result_contract(
            plan, 0, False, bad_sanity, rows, PNG_BYTES)
        self.assertEqual(errors, [])
        self.assertEqual(sanity, ["sinkhorn_converged"])

    def test_sanity_failure_routes_to_interpret_and_clamps(self):
        modal = FakeModal([fake_modal_run(
            returncode=0, stdout=stdout_ok({"sinkhorn_converged": False}),
            artifacts={"/work/results.csv": csv_bytes(
                [256, 512], ["sliced_wasserstein", "sinkhorn"], 2, SW_FAST),
                "/work/scaling.png": PNG_BYTES})])
        result, _, _ = self.run_graph(self.happy_spark(), modal, thread_id="v-sanity")
        self.assertEqual(result["status"], "completed")
        ev = pr.load_project(self.project_dir).verifications[0]
        # interpreter proposed supported, but the sanity criterion is unmet
        self.assertEqual(ev.status, "inconclusive")
        self.assertEqual(ev.sanity_failed, ["sinkhorn_converged"])
        self.assertEqual(ev.repairs, [], "no repair budget spent on findings")

    # -- idempotency + resume -------------------------------------------------

    def test_attach_is_idempotent(self):
        modal = FakeModal([valid_modal_outcome()])
        self.run_graph(self.happy_spark(), modal, thread_id="v-idem")
        project = pr.load_project(self.project_dir)
        self.assertEqual(len(project.verifications), 1)
        # Re-running attach with the same evidence must not duplicate.
        import agent_graph as ag

        record = ag.load_run_record(self.project_dir, "v-idem")
        self.assertEqual(record.status, "completed")
        ctx = VerifyContext(project_dir=self.project_dir, analysis=self.analysis,
                            paper=self.paper, claim_id="claim-1",
                            spark=self.happy_spark(), modal=FakeModal([]))
        ev = json.loads((vg.verify_thread_dir(self.project_dir, "v-idem") / "evidence.json").read_text())
        state = vg.initial_verify_state("proj", "v-idem", "p1", "claim-1")
        state["evidence"] = ev
        vg.v_attach(state, {"configurable": {"thread_id": "v-idem", "ctx": ctx}})
        self.assertEqual(len(pr.load_project(self.project_dir).verifications), 1)

    def test_claim_ref_validation(self):
        import agent

        paper, analysis, _ = agent.validate_claim_ref(self.project_dir, "p1", "claim-1")
        self.assertEqual(paper.paper_id, "p1")
        with self.assertRaises(ValueError):
            agent.validate_claim_ref(self.project_dir, "p1", "eq1")
        with self.assertRaises(ValueError):
            agent.validate_claim_ref(self.project_dir, "p1", "missing")
        with self.assertRaises(ValueError):
            agent.validate_claim_ref(self.project_dir, "nope", "claim-1")

    def test_stage_verifications_copies_artifacts(self):
        import agent

        modal = FakeModal([valid_modal_outcome()])
        self.run_graph(self.happy_spark(), modal, thread_id="v-stage")
        web_dir = Path(self.tmp.name) / "web"
        agent.stage_verifications(self.project_dir, web_dir)
        staged = json.loads((web_dir / "public" / "data" / "agent" / "verifications.json").read_text())
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["status"], "supported")
        tdir = web_dir / "public" / "data" / "agent" / "verifications" / "v-stage"
        for name in ("experiment.py", "results.csv", "scaling.png", "evidence.json"):
            self.assertTrue((tdir / name).is_file(), name)
        self.assertTrue(staged[0]["web_plot"].endswith("scaling.png"))


if __name__ == "__main__":
    unittest.main()
