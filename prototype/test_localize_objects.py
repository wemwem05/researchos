"""Offline tests for localize_objects.py (no API key needed).

Run from the repo root:
    .venv/bin/python -m unittest discover -s prototype -p "test_*.py"
"""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import localize_objects as loc
import pymupdf
from analyze_paper import AnalysisResult, SciObject
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDF = REPO_ROOT / "samples" / "eot_shape_correspondence.pdf"


def base_object(obj_id, obj_type, page=4, label="Figure 1"):
    return {
        "id": obj_id,
        "type": obj_type,
        "page": page,
        "label": label,
        "title": f"Test {obj_type}",
        "what_it_is": "x",
        "why_it_is_here": "x",
        "claim_supported": "x",
        "limitations": "x",
        "paper_importance": 0.8,
        "paper_importance_reason": "core",
        "research_relevance": "x",
        "research_relevance_score": 0.9,
        "research_relevance_reason": "directly about the question",
        "related_objects": [],
        "confidence": 0.9,
        "evidence": "p.1",
    }


def analysis_fixture():
    return AnalysisResult.model_validate(
        {
            "paper_title": "Test Paper",
            "research_question": "Q?",
            "objects": [
                base_object("eq1", "equation", page=5, label="Equation 10"),
                base_object("fig1", "figure", page=4, label="Figure 1"),
                base_object("tab1", "table", page=7, label="Table 1"),
                base_object("c1", "claim", page=7, label=None),
            ],
        }
    )


class TestBoundingBox(unittest.TestCase):
    def test_valid_box(self):
        b = loc.BoundingBox.model_validate(
            {"x_min": 10, "y_min": 20, "x_max": 900, "y_max": 950}
        )
        self.assertEqual((b.x_min, b.y_max), (10, 950))

    def test_full_range_boundaries_ok(self):
        loc.BoundingBox.model_validate(
            {"x_min": 0, "y_min": 0, "x_max": 1000, "y_max": 1000}
        )

    def test_x_order_rejected(self):
        for bad in ({"x_min": 50, "x_max": 50}, {"x_min": 60, "x_max": 50}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    loc.BoundingBox.model_validate(
                        {"y_min": 0, "y_max": 10, **bad}
                    )

    def test_y_order_rejected(self):
        for bad in ({"y_min": 50, "y_max": 50}, {"y_min": 60, "y_max": 50}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    loc.BoundingBox.model_validate(
                        {"x_min": 0, "x_max": 10, **bad}
                    )

    def test_out_of_range_rejected(self):
        for bad in (
            {"x_min": -1},
            {"y_min": -1},
            {"x_max": 1001},
            {"y_max": 1001},
        ):
            with self.subTest(bad=bad):
                good = {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10}
                good.update(bad)
                with self.assertRaises(ValidationError):
                    loc.BoundingBox.model_validate(good)

    def test_confidence_range(self):
        with self.assertRaises(ValidationError):
            loc.ObjectLocalization.model_validate(
                {"object_id": "x", "page": 1, "bbox": None, "confidence": 1.5}
            )

    def test_null_bbox_allowed(self):
        m = loc.ObjectLocalization.model_validate(
            {"object_id": "x", "page": 1, "bbox": None, "confidence": 0.1}
        )
        self.assertIsNone(m.bbox)


class TestSelectionAndMerge(unittest.TestCase):
    def test_select_localizable(self):
        targets = loc.select_localizable(analysis_fixture().objects)
        self.assertEqual({o.id for o in targets}, {"eq1", "fig1", "tab1"})

    def test_unlabeled_or_unpaged_excluded(self):
        objs = [
            SciObject.model_validate(base_object("a", "figure", page=None)),
            SciObject.model_validate(base_object("b", "table", label=None)),
        ]
        self.assertEqual(loc.select_localizable(objs), [])

    def test_build_localizations_merges_and_attaches_page(self):
        known = [
            SciObject.model_validate(base_object("fig1", "figure", page=4)),
            SciObject.model_validate(base_object("tab1", "table", page=4)),
        ]
        resp = loc.PageLocalizationResponse.model_validate(
            {
                "localizations": [
                    {
                        "object_id": "fig1",
                        "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
                        "confidence": 0.8,
                    },
                    {
                        "object_id": "ghost",
                        "bbox": None,
                        "confidence": 0.1,
                    },
                ]
            }
        )
        out = loc.build_localizations(4, known, resp)
        self.assertEqual([m.object_id for m in out], ["fig1", "tab1"])
        self.assertTrue(all(m.page == 4 for m in out))  # page from analysis
        self.assertIsNotNone(out[0].bbox)
        self.assertIsNone(out[1].bbox)  # missing from response -> null
        self.assertEqual(out[1].confidence, 0.0)

    def test_prompt_lists_only_known_objects(self):
        known = [
            SciObject.model_validate(
                base_object("eq1", "equation", page=5, label="Equation 10")
            )
        ]
        prompt = loc.build_page_prompt(5, known)
        self.assertIn("Equation 10", prompt)
        self.assertIn("eq1", prompt)
        self.assertIn("1000", prompt)
        self.assertNotIn("fig1", prompt)


class TestRenderAndDebug(unittest.TestCase):
    def test_render_page_returns_png(self):
        png = loc.render_page(SAMPLE_PDF, 4)
        self.assertTrue(png.startswith(b"\x89PNG"))
        img = __import__("PIL.Image", fromlist=["Image"]).open(io.BytesIO(png))
        self.assertGreater(img.size[0], 1000)  # high-resolution render

    def test_render_page_out_of_range(self):
        with self.assertRaises(ValueError):
            loc.render_page(SAMPLE_PDF, 9999)

    def test_draw_debug_image(self):
        png = loc.render_page(SAMPLE_PDF, 4)
        locs = [
            loc.ObjectLocalization.model_validate(
                {
                    "object_id": "fig1",
                    "page": 4,
                    "bbox": {"x_min": 100, "y_min": 100, "x_max": 900, "y_max": 900},
                    "confidence": 0.9,
                }
            ),
            loc.ObjectLocalization.model_validate(
                {"object_id": "eq1", "page": 4, "bbox": None, "confidence": 0.0}
            ),
        ]
        out = loc.draw_debug_image(png, locs)
        self.assertTrue(out.startswith(b"\x89PNG"))

    def test_localized_output_round_trip(self):
        out = loc.LocalizedOutput(
            analysis=analysis_fixture(),
            localizations=[
                loc.ObjectLocalization.model_validate(
                    {"object_id": "fig1", "page": 4, "bbox": None, "confidence": 0.0}
                )
            ],
        )
        raw = out.model_dump_json()
        back = loc.LocalizedOutput.model_validate_json(raw)
        self.assertEqual(back.analysis.paper_title, "Test Paper")
        self.assertEqual(len(back.analysis.objects), 4)  # analysis preserved
        self.assertEqual(len(back.localizations), 1)


class TestConfidenceSemantics(unittest.TestCase):
    def test_null_with_high_confidence_rejected(self):
        with self.assertRaises(ValidationError):
            loc.ObjectLocalization.model_validate(
                {"object_id": "x", "page": 1, "bbox": None, "confidence": 0.96}
            )

    def test_null_with_low_confidence_ok(self):
        for conf in (0.0, 0.3):
            m = loc.ObjectLocalization.model_validate(
                {"object_id": "x", "page": 1, "bbox": None, "confidence": conf}
            )
            self.assertEqual(m.confidence, conf)

    def test_box_with_high_confidence_ok(self):
        m = loc.ObjectLocalization.model_validate(
            {
                "object_id": "x",
                "page": 1,
                "bbox": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10},
                "confidence": 0.96,
            }
        )
        self.assertEqual(m.confidence, 0.96)

    def test_invalid_method_rejected(self):
        with self.assertRaises(ValidationError):
            loc.ObjectLocalization.model_validate(
                {
                    "object_id": "x",
                    "page": 1,
                    "bbox": None,
                    "confidence": 0.0,
                    "localization_method": "guess",
                }
            )

    def test_merge_coerces_null_confidence(self):
        known = [SciObject.model_validate(base_object("tab1", "table", page=7))]
        resp = loc.PageLocalizationResponse.model_validate(
            {"localizations": [{"object_id": "tab1", "bbox": None, "confidence": 0.96}]}
        )
        (out,) = loc.build_localizations(7, known, resp)
        self.assertIsNone(out.bbox)
        self.assertLessEqual(out.confidence, 0.3)
        self.assertEqual(out.localization_method, "muse_vision")
        self.assertTrue(out.localization_note)


class TestHybridTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(SAMPLE_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_caption_hits_found(self):
        hits7 = loc.caption_hits(self.doc[6], "Table 1")
        hits8 = loc.caption_hits(self.doc[7], "Table 4")
        self.assertGreaterEqual(len(hits7), 1)
        self.assertGreaterEqual(len(hits8), 1)
        self.assertEqual(loc.caption_hits(self.doc[6], "Table 4"), [])

    def test_caption_block_covers_paragraph(self):
        hit = loc.caption_hits(self.doc[7], "Table 4")[0]
        block = loc.caption_block_rect(self.doc[7], hit)
        self.assertGreater(block.y1, hit.y1)  # multi-line caption
        self.assertIn("Ablation", loc.caption_text(self.doc[7], hit))

    def test_native_candidates_found(self):
        self.assertGreaterEqual(len(loc.native_table_candidates(self.doc[6])), 1)
        self.assertGreaterEqual(len(loc.native_table_candidates(self.doc[7])), 1)

    def test_choose_native_candidate_unique(self):
        page_rect = pymupdf.Rect(0, 0, 612, 792)
        cap = pymupdf.Rect(50, 73, 77, 84)
        near = pymupdf.Rect(58, 110, 537, 328)
        far = pymupdf.Rect(324, 412, 530, 507)
        match = loc.choose_native_candidate([near, far], [cap], page_rect)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], near)

    def test_choose_native_candidate_ambiguous(self):
        page_rect = pymupdf.Rect(0, 0, 612, 792)
        cap = pymupdf.Rect(50, 73, 300, 84)
        c1 = pymupdf.Rect(50, 90, 300, 200)
        c2 = pymupdf.Rect(310, 90, 560, 200)
        self.assertIsNone(loc.choose_native_candidate([c1, c2], [cap], page_rect))

    def test_choose_native_candidate_far(self):
        page_rect = pymupdf.Rect(0, 0, 612, 792)
        cap = pymupdf.Rect(50, 73, 77, 84)
        far = pymupdf.Rect(324, 412, 530, 507)
        self.assertIsNone(loc.choose_native_candidate([far], [cap], page_rect))

    def test_normalize_rect_math(self):
        page_rect = pymupdf.Rect(0, 0, 612, 792)
        b = loc.normalize_rect(pymupdf.Rect(61.2, 79.2, 550.8, 712.8), page_rect)
        self.assertEqual((b.x_min, b.y_min, b.x_max, b.y_max), (100, 100, 900, 900))

    def test_crop_stays_in_page(self):
        page_rect = pymupdf.Rect(0, 0, 612, 792)
        for cap in (pymupdf.Rect(50, 0, 77, 10), pymupdf.Rect(50, 780, 77, 790)):
            crop = loc.crop_around_caption(cap, page_rect)
            self.assertTrue(page_rect.contains(crop))
            self.assertGreater(crop.height, 100)

    def test_map_crop_box_to_page_math(self):
        page_rect = pymupdf.Rect(0, 0, 1000, 1000)
        crop = pymupdf.Rect(0, 100, 1000, 600)  # top half-ish window
        full = {"x_min": 0, "y_min": 0, "x_max": 1000, "y_max": 1000}
        b = loc.map_crop_box_to_page(
            crop, page_rect, loc.BoundingBox.model_validate(full)
        )
        self.assertEqual((b.x_min, b.y_min, b.x_max, b.y_max), (0, 100, 1000, 600))

    def test_title_overlap(self):
        self.assertGreater(
            loc.title_overlap(
                "Quantitative results on near-isometric shape matching",
                "Table 1. Quantitative results on near-isometric shape matching.",
            ),
            0.5,
        )
        self.assertEqual(loc.title_overlap("Ablation study", "Table 1. Foo bar"), 0.0)

    def test_crop_prompt_names_table(self):
        t = SciObject.model_validate(
            base_object("tab1", "table", page=7, label="Table 1")
        )
        prompt = loc.build_crop_prompt(7, t)
        self.assertIn("CROP", prompt)
        self.assertIn("Table 1", prompt)
        self.assertIn("tab1", prompt)

    def test_native_path_resolves_real_tables(self):
        cases = [
            (6, "Table 1", "Quantitative results on near-isometric shape matching"),
            (7, "Table 4", "Ablation study on SHREC'19"),
        ]
        for idx, label, title in cases:
            with self.subTest(label=label):
                t = SciObject.model_validate(
                    {**base_object("t", "table", page=idx + 1, label=label),
                     "title": title}
                )
                # Native path needs no API client.
                m = loc.localize_table(None, "unused", self.doc[idx], idx + 1, t)  # type: ignore[arg-type]
                self.assertIsNotNone(m)
                assert m is not None
                self.assertEqual(m.localization_method, "pdf_native")
                self.assertIsNotNone(m.bbox)
                self.assertIn(label, m.localization_note or "")


class TestLocalizationCache(unittest.TestCase):
    """Part 9: successful boxes are reused; failures never poison the cache."""

    def test_store_and_lookup_round_trip(self):
        from analyze_paper import SciObject

        obj = SciObject.model_validate(
            base_object("fig1", "figure", page=4, label="Figure 1")
        )
        pdf = b"fake-pdf-bytes"
        loc_box = loc.ObjectLocalization.model_validate(
            {
                "object_id": "fig1",
                "page": 4,
                "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
                "confidence": 0.9,
                "localization_method": "muse_vision",
            }
        )
        cache: dict[str, dict] = {}
        self.assertIsNone(loc.lookup_cached(cache, pdf, obj))  # miss first
        loc.store_for_object(cache, pdf, obj, loc_box)
        hit = loc.lookup_cached(cache, pdf, obj)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.bbox, loc_box.bbox)
        self.assertEqual(hit.localization_method, "muse_vision")

    def test_null_localization_never_cached(self):
        from analyze_paper import SciObject

        obj = SciObject.model_validate(
            base_object("tab9", "table", page=7, label="Table 9")
        )
        pdf = b"fake-pdf-bytes"
        cache: dict[str, dict] = {}
        loc.store_for_object(
            cache,
            pdf,
            obj,
            loc.ObjectLocalization.model_validate(
                {"object_id": "tab9", "page": 7, "bbox": None, "confidence": 0.0}
            ),
        )
        self.assertEqual(cache, {})
        self.assertIsNone(loc.lookup_cached(cache, pdf, obj))

    def test_cache_key_depends_on_pdf_object_page(self):
        from analyze_paper import SciObject

        obj = SciObject.model_validate(
            base_object("fig1", "figure", page=4, label="Figure 1")
        )
        other_page = SciObject.model_validate(
            base_object("fig1", "figure", page=5, label="Figure 1")
        )
        other_obj = SciObject.model_validate(
            base_object("fig2", "figure", page=4, label="Figure 2")
        )
        pdf = b"fake-pdf-bytes"
        cache: dict[str, dict] = {}
        loc.store_for_object(
            cache,
            pdf,
            obj,
            loc.ObjectLocalization.model_validate(
                {
                    "object_id": "fig1",
                    "page": 4,
                    "bbox": {"x_min": 1, "y_min": 2, "x_max": 3, "y_max": 4},
                    "confidence": 0.9,
                }
            ),
        )
        # Same PDF/object/page -> hit; anything else -> miss.
        self.assertIsNotNone(loc.lookup_cached(cache, pdf, obj))
        self.assertIsNone(loc.lookup_cached(cache, b"other-pdf", obj))
        self.assertIsNone(loc.lookup_cached(cache, pdf, other_page))
        self.assertIsNone(loc.lookup_cached(cache, pdf, other_obj))

    def test_corrupt_or_null_entry_is_miss_not_hit(self):
        from analyze_paper import SciObject

        obj = SciObject.model_validate(
            base_object("fig1", "figure", page=4, label="Figure 1")
        )
        pdf = b"fake-pdf-bytes"
        key = loc.cache_key(pdf, obj)
        for bad in (
            {"object_id": "fig1", "page": 4, "bbox": None, "confidence": 0.0},
            {"object_id": "fig1", "page": 4},  # malformed
            "not-a-dict",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(loc.lookup_cached(cache={key: bad}, pdf_bytes=pdf, obj=obj))

    def test_save_and_load_round_trip(self):
        import tempfile

        cache = {"k": {"object_id": "x", "page": 1, "bbox": None, "confidence": 0.0}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            loc.save_cache(p, cache)
            self.assertEqual(loc.load_cache(p), cache)
            self.assertEqual(loc.load_cache(Path(d) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
