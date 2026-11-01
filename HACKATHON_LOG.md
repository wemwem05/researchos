# ResearchOS Hackathon Log

## Milestone 1 — Minimal technical prototype (2026-11-01)

**Goal:** test one assumption — given a scientific PDF + a research question,
Muse can produce structured, grounded JSON describing the paper's important
scientific objects (equations, figures, tables, experimentally testable claims).

**Built:**

- `prototype/analyze_paper.py` — CLI that sends the PDF directly to
  `muse-spark-1.3` (Meta Model API, `https://api.meta.ai/v1`) with the research
  question, requests structured output (`json_schema` from Pydantic models),
  validates the response with Pydantic, and writes `prototype/output.json`.
  Fallback chain: Responses API → Chat Completions + `json_schema` → Chat
  Completions plain JSON. Prompt forbids inventing evidence; grounding gaps
  (missing page, unknown `related_objects` id, missing per-type details) are
  reported as warnings.
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`.
- `.env.example` + `.gitignore` — key only via `MODEL_API_KEY`; `.env`,
  `.venv/`, and `prototype/output.json` never committed.
- `README.md` — setup + run instructions.
- `samples/eot_shape_correspondence.pdf` — symlink to the CVPR 2024 EOT +
  functional maps paper, so the documented command works as-is.

**Validation (no API key in this environment):** compile check, `--help`,
clean missing-key error, Pydantic validation of a representative fixture
(incl. rejection of bad `type` / out-of-range `confidence`), JSON-schema
generation for the API request, and `extract_json` fence-stripping. Live
end-to-end run pending `MODEL_API_KEY`.

**Explicitly out of scope:** auth, databases, Next.js, Modal, literature
search, vector DBs, and other product features.

### Milestone 1 hardening (2026-11-01, after first live run)

First live run succeeded at the API level but returned `paper_title: null`
with `objects: []`. Hardened the contract: `paper_title` is now required
non-empty, `objects` requires ≥4 entries with at least one equation, one
figure, one table, and one claim (Pydantic model validator + post-validation
sanity check), Responses `input_file` uses `"detail": "high"`, and the user
instruction explicitly forbids empty analyses. Added maintained offline tests
in `prototype/test_analyze_paper.py` covering the empty/missing-type rejections.

## Milestone 2 — multimodal scientific object localization (2026-11-02)

**Goal:** for every equation/figure/table in `output.json` with a page + label,
locate its visual region on that PDF page as a normalized 0..1000 box, for
later clickable overlays. (Live Milestone 1 result on the EOT paper: 1
equation, 1 figure, 2 tables, 1 claim.)

**Built:**

- `prototype/localize_objects.py` — renders ONLY referenced pages (PyMuPDF, 3x
  scale), sends each page image to `muse-spark-1.3` with high image detail,
  asking it to locate ONLY the known objects on that page (no rediscovery).
  Boxes validated with Pydantic (`BoundingBox`, `ObjectLocalization`):
  0..1000 range, `x_min < x_max`, `y_min < y_max`, unknown ids dropped with a
  warning, page attached from the analysis (guaranteed match), unconfident
  objects recorded as `bbox: null` (never invented). Output
  `prototype/localized_output.json` embeds the original analysis unchanged
  plus localizations. `--debug-dir` saves page PNGs with red box overlays for
  visual inspection.
- `prototype/test_localize_objects.py` — offline tests for box schema,
  coordinate rules, selection/merge logic, rendering, and debug drawing.
- `prototype/requirements.txt` += `pymupdf`, `pillow`; `.gitignore` +=
  `prototype/localized_output.json`, `prototype/debug/`.

**Explicitly out of scope:** frontend, Next.js, databases, authentication,
literature search, Modal, experiment execution, evidence graph.

**Live run (2026-11-02, EOT paper):** 2/4 boxes found. Figure 1 (p4) and
Equation 10 (p5) boxes verified correct against debug overlays. Table 1 (p7)
and Table 4 (p8) returned `bbox: null` even though both tables are visible on
their pages — a model recall miss, not a grounding error. Note: the null boxes
came back with confidence 0.96, so null-box confidence is currently unreliable
and should not drive UI decisions. Debug overlays in `prototype/debug/`
(git-ignored).

### Milestone 2 follow-up: hybrid table localization (2026-11-02)

Tables missed by vision are now resolved deterministically: `find_tables()`
candidates matched to the caption paragraph (label text search + title word
overlap), box = padded union of grid + caption (`pdf_native`, conf 0.9).
Ambiguous cases fall back to Muse vision on a caption-anchored crop (`hybrid`,
crop coords mapped back to page coords); figures/equations keep full-page
vision (`muse_vision`). Confidence semantics fixed: null bbox requires
confidence ≤ 0.3, enforced by Pydantic (non-compliant model values are clamped
with a warning). Debug overlays now color- and label-code the method per box.

**Live re-run:** 4/4 boxes found, both table boxes verified correct against
overlays. Note: full-page vision is nondeterministic across runs — one
intermediate run returned null for Figure 1 and Equation 10 (previously and
subsequently located correctly), so vision hits should be re-verified per run
while native table boxes are stable.
