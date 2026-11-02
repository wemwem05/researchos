"""Offline tests for inventory.py + relevance.py (no API key needed).

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inventory import discover_labels, format_checklist
from relevance import (
    HIGH_THRESHOLD,
    RELATED_THRESHOLD,
    importance_label,
    level_for_score,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDF = REPO_ROOT / "samples" / "eot_shape_correspondence.pdf"


class TestDiscoverLabels(unittest.TestCase):
    def test_eot_inventory_finds_all_major_labels(self):
        found = {(c.type, c.number) for c in discover_labels(SAMPLE_PDF)}
        for n in map(str, range(1, 15)):
            self.assertIn(("equation", n), found)
        for n in ("1", "2", "3", "4"):
            self.assertIn(("figure", n), found)
            self.assertIn(("table", n), found)
        # Substantially more than the 5 research-relevant objects of M1.
        self.assertGreater(len(found), 10)

    def test_no_duplicate_labels(self):
        found = [(c.type, c.number) for c in discover_labels(SAMPLE_PDF)]
        self.assertEqual(len(found), len(set(found)))

    def test_labels_sorted_by_type_then_number(self):
        found = discover_labels(SAMPLE_PDF)
        kinds = [c.type for c in found]
        self.assertEqual(
            kinds, sorted(kinds, key={"equation": 0, "figure": 1, "table": 2}.__getitem__)
        )

    def test_checklist_mentions_labels(self):
        text = format_checklist(discover_labels(SAMPLE_PDF))
        self.assertIn("Equation 10", text)
        self.assertIn("Figure 1", text)
        self.assertIn("Table 4", text)


class TestRelevanceThresholds(unittest.TestCase):
    def test_cutoffs_are_spec_values(self):
        self.assertEqual(HIGH_THRESHOLD, 0.75)
        self.assertEqual(RELATED_THRESHOLD, 0.40)

    def test_boundary_mapping(self):
        self.assertEqual(level_for_score(0.75), "high")
        self.assertEqual(level_for_score(0.7499), "related")
        self.assertEqual(level_for_score(0.40), "related")
        self.assertEqual(level_for_score(0.3999), "context")

    def test_importance_labels(self):
        self.assertEqual(importance_label(1.0), "High")
        self.assertEqual(importance_label(0.66), "High")
        self.assertEqual(importance_label(0.5), "Medium")
        self.assertEqual(importance_label(0.33), "Medium")
        self.assertEqual(importance_label(0.1), "Low")


if __name__ == "__main__":
    unittest.main()
