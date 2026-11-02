"""Offline tests for analyze_paper.py (no API key needed).

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_paper as ap
from pydantic import ValidationError


def base_draft_object(obj_id, obj_type):
    return {
        "id": obj_id,
        "type": obj_type,
        "page": 1,
        "label": f"{obj_type} 1",
        "title": f"Test {obj_type}",
        "what_it_is": "x",
        "why_it_is_here": "x",
        "claim_supported": "x",
        "limitations": "x",
        "paper_importance": 0.8,
        "paper_importance_reason": "core to the method",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.1",
    }


def base_object(obj_id, obj_type, score=0.9):
    return {
        **base_draft_object(obj_id, obj_type),
        "research_relevance": "x",
        "research_relevance_score": score,
        "research_relevance_reason": "directly about the question",
    }


def valid_fixture():
    return {
        "paper_title": "Test Paper",
        "research_question": "Q?",
        "objects": [
            base_object("eq1", "equation", 0.9),
            base_object("fig1", "figure", 0.5),
            base_object("tab1", "table", 0.2),
            base_object("c1", "claim", 0.8),
        ],
    }


def valid_draft_fixture():
    return {
        "paper_title": "Test Paper",
        "objects": [
            base_draft_object("eq1", "equation"),
            base_draft_object("fig1", "figure"),
            base_draft_object("tab1", "table"),
            base_draft_object("c1", "claim"),
        ],
    }


def relevance_fixture(scores=None):
    scores = scores or {"eq1": 0.9, "fig1": 0.5, "tab1": 0.2, "c1": 0.8}
    return {
        "scores": [
            {
                "id": oid,
                "research_relevance": "r",
                "research_relevance_score": s,
                "research_relevance_reason": "why",
            }
            for oid, s in scores.items()
        ]
    }


class TestAnalysisResult(unittest.TestCase):
    def test_valid_fixture_passes(self):
        r = ap.AnalysisResult.model_validate(valid_fixture())
        self.assertEqual(len(r.objects), 4)
        ap.sanity_check(r)  # must not raise

    def test_live_run_empty_shape_rejected(self):
        """The exact failed live-run shape: null title + empty objects."""
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(
                {"paper_title": None, "research_question": "Q?", "objects": []}
            )

    def test_empty_objects_rejected(self):
        bad = valid_fixture()
        bad["objects"] = []
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_null_title_rejected(self):
        bad = valid_fixture()
        bad["paper_title"] = None
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_blank_title_rejected_by_sanity_check(self):
        ok = valid_fixture()
        ok["paper_title"] = "   "
        r = ap.AnalysisResult.model_validate(ok)  # passes min_length=1
        with self.assertRaises(ValueError):
            ap.sanity_check(r)

    def test_missing_each_required_type_rejected(self):
        """4+ objects but one type absent must fail the model validator."""
        for missing in ("equation", "figure", "table", "claim"):
            with self.subTest(missing=missing):
                bad = valid_fixture()
                bad["objects"] = [
                    o for o in bad["objects"] if o["type"] != missing
                ]
                # Pad back to 4 so only the type validator can reject it.
                pad = base_object("pad", "claim" if missing != "claim" else "table")
                bad["objects"].append(pad)
                self.assertEqual(len(bad["objects"]), 4)
                with self.assertRaises(ValidationError):
                    ap.AnalysisResult.model_validate(bad)

    def test_fewer_than_four_objects_rejected(self):
        bad = valid_fixture()
        bad["objects"] = bad["objects"][:3]
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_invalid_type_rejected(self):
        bad = valid_fixture()
        bad["objects"][0]["type"] = "diagram"
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_out_of_range_confidence_rejected(self):
        bad = valid_fixture()
        bad["objects"][0]["confidence"] = 1.5
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_missing_required_field_rejected(self):
        bad = valid_fixture()
        del bad["objects"][0]["evidence"]
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_list_defaults_are_independent(self):
        a = ap.SciObject.model_validate(base_object("a", "claim"))
        b = ap.SciObject.model_validate(base_object("b", "claim"))
        a.related_objects.append("x")
        self.assertEqual(b.related_objects, [])


class TestMilestone31(unittest.TestCase):
    """Part 9: inventory/relevance contract (3.1)."""

    def test_relevance_score_bounds(self):
        for bad_score in (-0.1, 1.5):
            bad = valid_fixture()
            bad["objects"][0]["research_relevance_score"] = bad_score
            with self.subTest(score=bad_score):
                with self.assertRaises(ValidationError):
                    ap.AnalysisResult.model_validate(bad)

    def test_paper_importance_bounds(self):
        for bad_score in (-0.1, 1.5):
            bad = valid_fixture()
            bad["objects"][0]["paper_importance"] = bad_score
            with self.subTest(score=bad_score):
                with self.assertRaises(ValidationError):
                    ap.AnalysisResult.model_validate(bad)

    def test_threshold_mapping(self):
        from relevance import level_for_score

        self.assertEqual(level_for_score(1.0), "high")
        self.assertEqual(level_for_score(0.75), "high")
        self.assertEqual(level_for_score(0.749), "related")
        self.assertEqual(level_for_score(0.40), "related")
        self.assertEqual(level_for_score(0.399), "context")
        self.assertEqual(level_for_score(0.0), "context")

    def test_level_derives_from_score_not_model_text(self):
        """Whatever level string arrives, validation normalizes it from score."""
        obj = base_object("a", "claim", 0.1)
        obj["research_relevance_level"] = "high"  # model said high; score says context
        r = ap.SciObject.model_validate(obj)
        self.assertEqual(r.research_relevance_level, "context")

    def test_duplicate_ids_rejected(self):
        bad = valid_fixture()
        bad["objects"][1]["id"] = "eq1"
        with self.assertRaises(ValidationError):
            ap.AnalysisResult.model_validate(bad)

    def test_draft_duplicate_ids_rejected(self):
        bad = valid_draft_fixture()
        bad["objects"][1]["id"] = "eq1"
        with self.assertRaises(ValidationError):
            ap.AnalysisDraft.model_validate(bad)

    def test_merge_keeps_low_relevance_objects(self):
        """Context/low objects stay in the inventory; relevance never drops them."""
        draft = ap.AnalysisDraft.model_validate(valid_draft_fixture())
        rel = ap.RelevanceList.model_validate(
            relevance_fixture({"eq1": 0.05, "fig1": 0.1, "tab1": 0.0, "c1": 0.9})
        )
        result = ap.merge_relevance(draft, rel, "Q?")
        self.assertEqual({o.id for o in result.objects}, {"eq1", "fig1", "tab1", "c1"})
        self.assertEqual(
            {o.research_relevance_level for o in result.objects if o.id != "c1"},
            {"context"},
        )

    def test_rescore_does_not_change_inventory_identity(self):
        """draft_from_result strips only relevance; ids/pages/labels survive."""
        result = ap.AnalysisResult.model_validate(valid_fixture())
        draft = ap.draft_from_result(result)
        for orig, stripped in zip(result.objects, draft.objects):
            self.assertEqual(stripped.id, orig.id)
            self.assertEqual(stripped.type, orig.type)
            self.assertEqual(stripped.page, orig.page)
            self.assertEqual(stripped.label, orig.label)
            self.assertEqual(stripped.title, orig.title)
        # And a rescore with different scores changes only relevance fields.
        rel = ap.RelevanceList.model_validate(
            relevance_fixture({"eq1": 0.0, "fig1": 0.0, "tab1": 0.0, "c1": 0.0})
        )
        rescored = ap.merge_relevance(draft, rel, "different question?")
        self.assertEqual(rescored.research_question, "different question?")
        self.assertEqual(
            [o.id for o in rescored.objects], [o.id for o in result.objects]
        )
        self.assertTrue(
            all(o.research_relevance_level == "context" for o in rescored.objects)
        )

    def test_merge_rejects_unknown_or_missing_ids(self):
        draft = ap.AnalysisDraft.model_validate(valid_draft_fixture())
        rel = ap.RelevanceList.model_validate(relevance_fixture())
        rel.scores[0].id = "nope"
        with self.assertRaises(ValueError):
            ap.merge_relevance(draft, rel, "Q?")
        rel2 = ap.RelevanceList.model_validate(relevance_fixture())
        rel2.scores.pop()
        with self.assertRaises(ValueError):
            ap.merge_relevance(draft, rel2, "Q?")


class TestHelpers(unittest.TestCase):
    def test_grounding_warnings(self):
        r = ap.AnalysisResult.model_validate(valid_fixture())
        r.objects[0].related_objects = ["nope"]
        self.assertTrue(any("nope" in w for w in ap.check_grounding(r)))

    def test_extract_json_strips_fences(self):
        self.assertEqual(json.loads(ap.extract_json('```json\n{"a": 1}\n```')), {"a": 1})
        self.assertEqual(json.loads(ap.extract_json('{"a": 1}')), {"a": 1})

    def test_json_schema_formats(self):
        rfmt = ap.responses_json_schema_format()
        self.assertEqual(rfmt["type"], "json_schema")
        self.assertIn("objects", rfmt["schema"]["properties"])
        cfmt = ap.chat_json_schema_format()
        self.assertEqual(cfmt["type"], "json_schema")
        self.assertIn("objects", cfmt["json_schema"]["schema"]["properties"])
        self.assertNotIn("json_schema", rfmt)
        self.assertNotIn("schema", cfmt)

    def test_inventory_prompt_requires_nonempty_analysis(self):
        s = ap.build_inventory_text("paper.pdf", "- Equation 1 (page 3)")
        self.assertIn("paper.pdf", s)
        self.assertIn("Equation 1", s)
        self.assertIn("An empty objects array is invalid", s)

    def test_relevance_text_uses_question(self):
        draft = ap.AnalysisDraft.model_validate(valid_draft_fixture())
        s = ap.build_relevance_text("Why is the sky blue?", draft)
        self.assertIn("Why is the sky blue?", s)
        self.assertIn("eq1", s)


if __name__ == "__main__":
    unittest.main()
