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


def base_object(obj_id, obj_type):
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
        "research_relevance": "x",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.1",
    }


def valid_fixture():
    return {
        "paper_title": "Test Paper",
        "research_question": "Q?",
        "objects": [
            base_object("eq1", "equation"),
            base_object("fig1", "figure"),
            base_object("tab1", "table"),
            base_object("c1", "claim"),
        ],
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

    def test_prompt_requires_nonempty_analysis(self):
        s = ap.build_question_text("paper.pdf", "Q?")
        self.assertIn("paper.pdf", s)
        self.assertIn("Q?", s)
        self.assertIn("An empty objects array is invalid", s)


if __name__ == "__main__":
    unittest.main()
