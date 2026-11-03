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

## Milestone 4: Research Project + Cross-Paper Memory

One project, one persistent question, multiple manually added papers.
Per-paper analysis is unchanged; `prototype/project.py` adds the memory layer:

```bash
source .venv/bin/activate
python prototype/project.py init \
  --question "Can alternative optimal transport costs improve non-rigid shape correspondence?" \
  --project-dir prototype/projects/ot-correspondence
python prototype/project.py add-paper \
  --project-dir prototype/projects/ot-correspondence \
  --paper-id deep-shells \
  --pdf samples/<paper>.pdf \
  --analysis prototype/deep_shells_output.json \
  --localized prototype/deep_shells_localized.json
python prototype/project.py stage-web \
  --project-dir prototype/projects/ot-correspondence \
  --web-dir web
```

Each `add-paper` interprets the new paper relative to existing findings
(grounded findings, typed connections, contribution, open questions,
corpus-scoped novelty risks) and appends to `project.json` — no database.
The web app gains `/project` (question, papers, findings, connections,
questions, risks) and `/read/[paperId]` (any paper in the existing Reader);
`/` is unchanged.

Paper PDFs are local-only fixtures and are never committed (see `.gitignore`).
Only model-generated JSON is committed.

## Milestone 5: Persistent Muse Spark Open Research Agent Graph

The add-paper step is a LangGraph workflow
(`prototype/agent_graph.py`) powered by Muse Spark Open through Baseten
(exact model `meta-models/muse-spark-open-1026`, key via `BASETEN_API_KEY`;
see `docs/OPEN_WEIGHT_USAGE.md`):

```bash
source .venv/bin/activate
python prototype/agent.py verify   # one real Baseten call (no fallback model)
python prototype/agent.py investigate \
  --project-dir prototype/projects/ot-correspondence \
  --paper-id new-paper --pdf samples/<paper>.pdf \
  --analysis prototype/<analysis>.json --localized prototype/<localized>.json
python prototype/agent.py status --project-dir prototype/projects/ot-correspondence
# if the run waits on human review (genuine ambiguity only):
python prototype/agent.py review --project-dir prototype/projects/ot-correspondence \
  --thread-id <id> --choice "Not comparable — drop the connection"
python prototype/agent.py stage-web --project-dir prototype/projects/ot-correspondence --web-dir web
# optional: live HITL resume from the Project view:
python prototype/agent.py serve --project-dir prototype/projects/ot-correspondence --port 8765
```

Investigator → deterministic validation → Critic → finalize / one bounded
revision / human review (`interrupt()`) → single project mutation.
Checkpoints are SQLite per project, so a run survives process restarts and
delayed reviews without re-running completed model calls. PDF perception
still uses hosted Muse; only investigator/critic/revision use Spark Open.

## Layout

- `prototype/analyze_paper.py` — Milestone 1: CLI, prompt, API calls,
  Pydantic models, validation
- `prototype/localize_objects.py` — Milestone 2: page rendering, visual
  localization, debug overlays
- `prototype/project.py` — Milestone 4: project memory CLI + models
- `prototype/spark_open.py` — Milestone 5: Baseten Spark Open client (exact
  model ID, telemetry, no fallback)
- `prototype/agent_graph.py` — Milestone 5: LangGraph investigator/critic/
  revision/HITL workflow, SQLite checkpoints, run records, validation gates
- `prototype/agent.py` — Milestone 5: agent CLI (investigate/status/review/
  stage-web/serve/verify)
- `prototype/agent_server.py` — Milestone 5: stdlib API for live HITL resume
- `prototype/synthetic_demo.py` — Milestone 5: labeled synthetic HITL fixture
- `prototype/test_analyze_paper.py`, `prototype/test_localize_objects.py`,
  `prototype/test_inventory.py`, `prototype/test_project.py`,
  `prototype/test_agent_graph.py` — offline tests (no API key):
  `.venv/bin/python -m unittest discover -s prototype -p "test_*.py"`
- `web/app/lib/relevance.test.ts`, `web/app/lib/project.test.ts`,
  `web/app/lib/agent.test.ts` — frontend unit tests (vitest):
  `cd web && npm test`
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`,
  `pymupdf`, `pillow`, `langgraph`, `langgraph-checkpoint-sqlite`
- `web/` — Milestone 3 Next.js reader (`app/`, `public/paper.pdf`,
  `public/data/localized_output.json`)
- `samples/` — sample paper PDF
- `HACKATHON_LOG.md` — milestone log
