"""Offline tests for project.py (no API key needed).

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
from analyze_paper import AnalysisResult
from pydantic import ValidationError


def analysis_object(obj_id, score=0.9):
    return {
        "id": obj_id,
        "type": "equation",
        "page": 1,
        "label": "Equation 1",
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
            "paper_title": "Paper",
            "research_question": "Q?",
            "objects": [
                {**analysis_object("eq1"), "type": "equation"},
                {**analysis_object("fig1", 0.1), "type": "figure", "label": "Figure 9"},
                {**analysis_object("tab1", 0.5), "type": "table"},
                {**analysis_object("c1", 0.5), "type": "claim"},
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


def delta_fixture(**over):
    d = {
        "findings": [
            {
                "statement": "S1",
                "evidence": "Table 1, p.2",
                "page": 2,
                "object_ids": ["tab1"],
                "confidence": 0.9,
            }
        ],
        "connections": [],
        "contribution": {
            "what_it_adds": "a",
            "stance_toward_prior": "b",
            "overlap_with_direction": "c",
            "new_open_questions": ["q?"],
        },
        "open_questions": [],
        "novelty_risks": [],
    }
    d.update(over)
    return pr.ProjectDelta.model_validate(d)


class TestConnectionTypes(unittest.TestCase):
    def test_enum_has_exactly_seven_types(self):
        self.assertEqual(
            set(pr.CONNECTION_TYPES),
            {
                "supports",
                "contradicts",
                "extends",
                "overlaps_with",
                "uses_same_method",
                "uses_different_assumption",
                "leaves_open",
            },
        )

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValidationError):
            pr.NewConnection.model_validate(
                {"from_ref": "f1", "to_ref": "f2", "type": "cites",
                 "reason": "x", "confidence": 0.5}
            )


class TestApplyDelta(unittest.TestCase):
    def test_first_paper_assigns_canonical_ids(self):
        p = pr.apply_delta(pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture())
        self.assertEqual([f.id for f in p.findings], ["f1"])
        self.assertEqual(p.findings[0].paper_id, "p1")
        self.assertEqual(len(p.contributions), 1)

    def test_second_paper_resolves_new_refs(self):
        p1 = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        delta = delta_fixture(
            connections=[
                {"from_ref": "new[0]", "to_ref": "f1", "type": "extends",
                 "reason": "builds on it", "confidence": 0.8}
            ]
        )
        p2 = pr.apply_delta(p1, paper_entry("p2", 2), analysis_fixture(), delta)
        self.assertEqual([f.id for f in p2.findings], ["f1", "f2"])
        (c,) = p2.connections
        self.assertEqual((c.from_ref, c.to_ref), ("f2", "f1"))

    def test_first_paper_must_not_propose_connections(self):
        delta = delta_fixture(
            connections=[
                {"from_ref": "new[0]", "to_ref": "paper:p1", "type": "supports",
                 "reason": "x", "confidence": 0.5}
            ]
        )
        with self.assertRaises(ValueError):
            pr.apply_delta(pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta)

    def test_unknown_object_id_rejected(self):
        delta = delta_fixture(
            findings=[
                {"statement": "S", "evidence": "p.1", "object_ids": ["ghost"], "confidence": 0.5}
            ]
        )
        with self.assertRaises(ValueError):
            pr.apply_delta(pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta)

    def test_unknown_finding_ref_rejected(self):
        p1 = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        delta = delta_fixture(
            connections=[
                {"from_ref": "new[0]", "to_ref": "f99", "type": "supports",
                 "reason": "x", "confidence": 0.5}
            ]
        )
        with self.assertRaises((ValueError, ValidationError)):
            pr.apply_delta(p1, paper_entry("p2", 2), analysis_fixture(), delta)

    def test_new_ref_out_of_range_rejected(self):
        p1 = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        delta = delta_fixture(
            connections=[
                {"from_ref": "new[3]", "to_ref": "f1", "type": "supports",
                 "reason": "x", "confidence": 0.5}
            ]
        )
        with self.assertRaises(ValueError):
            pr.apply_delta(p1, paper_entry("p2", 2), analysis_fixture(), delta)

    def test_duplicate_paper_rejected(self):
        p1 = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        with self.assertRaises(ValueError):
            pr.apply_delta(p1, paper_entry("p1", 2), analysis_fixture(), delta_fixture())

    def test_findings_capped_at_five(self):
        many = [
            {"statement": f"S{i}", "evidence": "p.1", "confidence": 0.5}
            for i in range(6)
        ]
        with self.assertRaises(ValidationError):
            delta_fixture(findings=many)

    def test_zero_connections_is_valid(self):
        p1 = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        p2 = pr.apply_delta(p1, paper_entry("p2", 2), analysis_fixture(), delta_fixture())
        self.assertEqual(p2.connections, [])


class TestIntegrity(unittest.TestCase):
    def test_novelty_risk_requires_overlapping_refs(self):
        with self.assertRaises(ValidationError):
            pr.NoveltyRisk.model_validate(
                {"id": "n1", "direction": "d", "overlapping_finding_refs": [],
                 "note": "x", "raised_by": "p1"}
            )

    def test_dangling_question_ref_rejected(self):
        p = pr.apply_delta(
            pr.Project(research_question="Q?"), paper_entry(), analysis_fixture(), delta_fixture()
        )
        bad = p.model_copy(deep=True)
        bad.open_questions.append(
            pr.OpenQuestion(id="q1", text="t", raised_by="p1", finding_refs=["nope"])
        )
        with self.assertRaises(ValidationError):
            pr.Project.model_validate(bad.model_dump())


class TestSummaries(unittest.TestCase):
    def test_brief_includes_salient_not_context_detail(self):
        brief = pr.summarize_analysis(analysis_fixture())
        self.assertIn("eq1", brief)
        self.assertIn("tab1", brief)
        self.assertIn("Figure", brief)  # context object label listed
        self.assertNotIn("fig1", brief)  # ...but not its id/detail block

    def test_findings_summary_empty_state(self):
        self.assertIn("first paper", pr.summarize_findings(pr.Project(research_question="Q?")))


class TestStageWeb(unittest.TestCase):
    def test_stage_copies_fixtures_and_sets_urls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proj_dir = root / "proj"
            web_dir = root / "web"
            (proj_dir).mkdir()
            loc = {"analysis": {}, "localizations": []}
            (root / "loc.json").write_text(json.dumps(loc))
            (root / "p.pdf").write_bytes(b"%PDF-1.4 fake")
            p = pr.Project(research_question="Q?")
            paper = paper_entry()
            # Absolute paths pass through stage_web's repo-root join unchanged.
            paper.pdf_path = str(root / "p.pdf")
            paper.localized_path = str(root / "loc.json")
            pr.save_project(proj_dir, p.model_copy(update={"papers": [paper]}))
            pr.stage_web(proj_dir, web_dir)
            out = json.loads((web_dir / "public" / "data" / "project.json").read_text())
            self.assertEqual(out["papers"][0]["web_pdf"], "/papers/p1.pdf")
            self.assertEqual(out["papers"][0]["web_data"], "/data/papers/p1.json")
            self.assertTrue((web_dir / "public" / "papers" / "p1.pdf").is_file())
            self.assertTrue((web_dir / "public" / "data" / "papers" / "p1.json").is_file())


if __name__ == "__main__":
    unittest.main()
