# ResearchOS

A research-question-aware scientific paper workspace. (Meta AI hackathon project.)

## Milestone 1: minimal technical prototype

Tests one assumption: given a scientific PDF + a research question, Muse
(`muse-spark-1.3` via the Meta Model API) can produce structured, grounded JSON
describing the paper's important scientific objects — equations, figures,
tables, and experimentally testable claims.

This is a prototype, not a production app. No auth, databases, frontend,
literature search, or vector DB yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r prototype/requirements.txt
cp .env.example .env   # then put your key in .env (never commit it)
```

The only secret is `MODEL_API_KEY`, read from the environment (a local `.env`
file works too). See `.env.example`.

## Run

```bash
source .venv/bin/activate
python prototype/analyze_paper.py \
  --pdf samples/eot_shape_correspondence.pdf \
  --question "Can alternative optimal transport costs improve non-rigid shape correspondence?"
```

Output: `prototype/output.json` (validated against Pydantic models before writing).

Options: `--output PATH`, `--model ID` (default `muse-spark-1.3`),
`--base-url URL` (default `https://api.meta.ai/v1`).

Notes:

- The PDF is sent directly to the model (base64 `input_file`); no text
  extraction step. If the Responses API shape is rejected, the script falls
  back to Chat Completions with `json_schema`, then plain-JSON Chat
  Completions. All paths are validated with the same Pydantic models.
- `samples/eot_shape_correspondence.pdf` is a symlink to the full CVPR 2024
  paper file in the same folder, so the documented command works as-is.
- The prompt forbids inventing evidence: missing pages/labels/numbers come back
  as `null` with lowered confidence, and grounding gaps are printed as warnings.

## Milestone 2: visual localization

For every equation/figure/table in `output.json` that has a page + label,
renders only that PDF page (PyMuPDF, 3x scale) and locates the known object as
a normalized 0..1000 box. Figures/equations use `muse-spark-1.3` high-detail
vision on the page; tables use a hybrid path (PyMuPDF `find_tables()` matched
to the caption paragraph, else vision on a caption-anchored crop). The model
never rediscovers the document: it only locates the listed objects, returning
`bbox: null` (confidence ≤ 0.3) when unsure. Each result records
`localization_method` (`pdf_native`/`muse_vision`/`hybrid`) and a note.

```bash
source .venv/bin/activate
python prototype/localize_objects.py \
  --pdf samples/eot_shape_correspondence.pdf \
  --analysis prototype/output.json \
  --output prototype/localized_output.json \
  --debug-dir prototype/debug
```

Output: `prototype/localized_output.json` (original analysis + localizations).
`--debug-dir` saves per-page PNGs with the returned boxes drawn in red for
visual inspection. Claims are not localized (textual, often label-less).

## Milestone 3: interactive reader

A Next.js + TypeScript + Tailwind single-page app (`web/`): the real PDF on
the left (65%) with subtle clickable overlays for localized objects, and the
Research Lens panel on the right (35%) explaining the selected object in the
context of the research question. Equations render with KaTeX. No backend —
the app reads fixtures from `web/public/`.

```bash
cd web
npm install     # also stages the pdf.js worker via postinstall
npm run dev
```

Then open [http://localhost:3000](http://localhost:3000) in a browser.

After re-running the Python prototype, refresh the fixtures:

```bash
cp prototype/localized_output.json web/public/data/localized_output.json
# web/public/paper.pdf only changes if you analyze a different PDF
```

Desktop width first; mobile layout is not a priority for this milestone.

## Milestone 3.1: Research Lens

Separates "what objects exist" from "what matters to my question":

- **Stage 1 (question-independent):** `analyze_paper.py` inventories EVERY
  labeled equation/figure/table (aided by a deterministic label checklist from
  `prototype/inventory.py`), explains each, and rates `paper_importance`.
- **Stage 2 (cheap text-only):** each object is scored 0..1 against the
  research question; levels derive deterministically (`prototype/relevance.py`:
  high ≥ 0.75, related ≥ 0.40, else context).
- Changing the question re-runs only stage 2 (no rediscovery/relocalization):

```bash
python prototype/analyze_paper.py \
  --rescore prototype/output.json \
  --question "Your new question?" \
  --output prototype/output.json
```

- `localize_objects.py` caches successful boxes in
  `prototype/loc_cache.json` (keyed by PDF + object + page, never relevance),
  so retries and rescoring skip already-localized objects. `--no-cache`
  disables it.
- The reader has Focus (default) and Show-all modes, type filters, a
  relevance legend, and per-level overlay colors (indigo = high, blue =
  related, gray = context). No red/green — reserved for later verification
  states.

## Layout

- `prototype/analyze_paper.py` — Milestone 1: CLI, prompt, API calls,
  Pydantic models, validation
- `prototype/localize_objects.py` — Milestone 2: page rendering, visual
  localization, debug overlays
- `prototype/test_analyze_paper.py`, `prototype/test_localize_objects.py`,
  `prototype/test_inventory.py` — offline tests (no API key):
  `.venv/bin/python -m unittest discover -s prototype -p "test_*.py"`
- `web/app/lib/relevance.test.ts` — frontend unit tests (vitest):
  `cd web && npm test`
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`,
  `pymupdf`, `pillow`
- `web/` — Milestone 3 Next.js reader (`app/`, `public/paper.pdf`,
  `public/data/localized_output.json`)
- `samples/` — sample paper PDF
- `HACKATHON_LOG.md` — milestone log
