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

## Milestone 3 — Interactive research-aware PDF reader (2026-11-02)

**Goal:** first interactive ResearchOS reader — read the real PDF with
clickable overlays on localized scientific objects and a Research Lens panel
explaining each object against the research question.

**Built (`web/`, Next.js + TypeScript + Tailwind, no backend):**

- Header with ResearchOS branding + paper title; prominent research-question
  bar; object navigator chips (Equation 10, Figure 1, Table 1, Table 4) that
  scroll to the page and select the object.
- Left (65%): full 11-page PDF via react-pdf, fixtures from `web/public/`
  (`paper.pdf` + `data/localized_output.json`, copied from the prototype).
  Overlays use percent positioning from the 0..1000 boxes, so they stay
  aligned on resize. Subtle teal by default, highlight + label chip on hover,
  persistent highlight when selected — no red debug boxes.
- Right (35%): Research Lens. Default state shows paper title, question,
  object counts, and the select-an-object instruction. Selection shows the
  five required sections plus type-specific detail (KaTeX equation + symbols
  + role; figure components + ordered flow + role; table metric + better
  direction + comparisons + interpretation), evidence, and related-object
  chips.
- pdf.js worker is staged locally by an npm `postinstall` script (no CDN
  dependency); the reader is client-only (`ssr: false`) since pdf.js needs
  `window`.

**Verification:** `next build` green (TypeScript clean); served the production
build locally and confirmed HTTP 200 for `/`, `/paper.pdf`,
`/data/localized_output.json`, and `/pdf.worker.min.mjs`. Note: this sandbox
blocks Turbopack's loader workers and port binding, so verification used the
webpack builder and an escalated local server; standard `npm run dev` applies
on a normal machine. In-browser visual check left to the demo.

**Explicitly out of scope:** authentication, databases, literature search,
evidence graphs, Modal, experiment execution, production infrastructure.

## Milestone 3.1 — Research Lens: complete scientific-object inventory with research-question-aware relevance ranking (2026-11-02)

**Goal:** understand ALL major labeled objects in the paper while the research
question only determines visual priority — Focus mode (default) vs Show All
mode.

**Built (backend):**

- `prototype/inventory.py` — deterministic label discovery (caption-anchored
  figures/tables, display-anchored equations) producing a completeness
  checklist; the model finalizes from the PDF itself. Finds all 22 EOT labels
  (Eq 1–14, Fig 1–4, Tab 1–4), no duplicates.
- `prototype/relevance.py` — centralized thresholds (high ≥ 0.75, related ≥
  0.40, else context) + paper-importance labels; single source of truth.
- `prototype/analyze_paper.py` — two-stage pipeline. Stage 1 (PDF + checklist)
  inventories every labeled equation/figure/table with full explanations +
  `paper_importance`; claims stay selective. Stage 2 (cheap text-only call)
  scores each object against the research question; levels derive
  deterministically from scores at validation time. `--rescore` re-ranks a new
  question without rediscovery/relocalization.
- `prototype/localize_objects.py` — same strategies (native/hybrid tables,
  vision figures/equations, null-box confidence ≤ 0.3) plus a success-only
  cache (`prototype/loc_cache.json`, git-ignored) keyed by PDF hash + object +
  page — never relevance. `--no-cache` disables it.

**Live run (EOT paper):** 25 objects (14 eq + 4 fig + 4 tab + 3 claims);
relevance 7 high / 13 related / 5 context. High: Eq 4 (SW), Eq 6 (EBSW),
Eq 10, Eq 11, Eq 14 (refinement), Table 4 (ablation), OT-efficacy claim —
Figure 1 scored related (0.50) as architecture context rather than an
alternative-cost object. Localization 22/22 after cache-assisted retries
(pages 3 and 6 needed re-runs — the known vision nondeterminism); every box
verified correct against debug overlays.

**Built (frontend, `web/`):**

- Focus (default) / Show-all toggle; Focus summary line
  ("ResearchOS found X scientific objects. Y are highly relevant…").
- Overlays colored by level (indigo = high, blue = related, gray = context;
  no red/green). Focus: high persistent, related faint, context invisible but
  still clickable/hoverable; selection always accented. Show All: every object
  visibly overlaid.
- Navigator covers all 22 localized objects with All/Equations/Figures/Tables
  filters, high-first ordering, relevance dots, and a restrained legend.
- Research Lens panel: level badge, human-readable Importance/Relevance
  (no raw scores), relevance reason under "Why it matters to your research";
  existing type-specific details preserved.
- `web/app/lib/relevance.ts` — TS mirror of thresholds + pure visibility /
  ordering / filter helpers.

**Verification:** 69/69 backend tests green (incl. new threshold, bounds,
merge/rescore-identity, no-dupe, cache, and null-confidence tests); 8/8
frontend vitest tests green (thresholds, Focus filtering, Show All, navigator
ordering/filters); `tsc --noEmit` clean; `next build` green (webpack builder —
sandbox blocks Turbopack loader workers and port binding, same as M3);
production server smoke: HTTP 200 for `/`, `/paper.pdf`, `/pdf.worker.min.mjs`
and the 25-object/22-box fixture. In-browser visual check left to the demo.

**Explicitly out of scope:** authentication, databases, Supabase, literature
search, cross-paper memory, evidence graph, Modal, experiment execution,
deployment infrastructure, chat interface, unrelated refactors.

**Caveats:** vision localization stays nondeterministic per run (cache +
retries mitigate); Figure 1 is related rather than high by model judgment;
raw relevance scores are hidden from the UI by design (kept in JSON).

## Milestone 4 — Research Project + Cross-Paper Memory (2026-11-03)

**Goal:** stop treating each paper independently — one persistent research
question across manually added papers, with findings, typed connections, open
questions, and corpus-scoped novelty risks.

**Built (backend, `prototype/project.py`, new; `analyze_paper.py` untouched):**

- File-backed project state (`prototype/projects/<slug>/project.json`, no DB):
  `init` creates a project with one question; `add-paper` runs one text-only
  Muse call that interprets the new paper relative to existing findings
  (≤5 grounded findings with paper evidence + object ids, connections,
  contribution answering adds/stance/overlap/new-questions, open questions,
  novelty risks); `stage-web` copies per-paper fixtures into `web/public/`.
- Connection enum holds exactly the 7 requested types; only supported
  relationships are created (zero instances valid). All ids canonical
  (`f/c/q/n`); every finding/paper/object reference validated, first paper
  must propose no connections.
- Novelty risks are structured (direction + overlapping finding refs + note)
  and the UI renders them under a fixed "Within the papers currently
  analyzed…" prefix — global novelty is never claimed.

**Live run (question unchanged; Deep Shells added first, EOT second):**
Deep Shells analysis 23 objects (3 high incl. the entropic-OT baseline
energy), localized 20/20 after one cache-assisted retry; EOT reused as-is.
Project: 8 findings, 3 connections (f5 extends f3, f7 uses_same_method f1,
f6 extends f5 — other 5 types zero, as allowed), 5 open questions, 4 risks.
All EOT/Deep-Shells delta content manually reviewed in JSON for grounding.

**Built (frontend, `web/`):**

- Reader extracted to parameterized `components/Reader.tsx`; `/` unchanged
  in behavior (EOT); new `/read/[paperId]` serves any staged paper;
  findings deep-link via `?select=`.
- New `/project` view: question + counts, paper cards opening in the reader,
  key findings with evidence + deep links, connections grouped by present
  type only (restrained semantic colors: green supports, red contradicts,
  amber leaves-open/different-assumption), open questions and novelty risks
  with finding refs.
- `lib/project.ts` pure helpers + `project.test.ts` (6 tests).

**Verification:** 85/85 backend tests (16 new); 14/14 frontend tests;
`tsc` clean; `next build` green (webpack builder — sandbox blocks Turbopack,
as before); production smoke 200 on `/`, `/project`, both readers,
project/paper fixtures, and staged PDFs.

**Asset rights:** paper PDFs are now local-only fixtures (`*.pdf` ignored;
previously tracked PDFs untracked via `git rm --cached`, files kept).
Caveat: earlier commits still contain the EOT PDF bytes; history was
deliberately not rewritten. Only model-generated JSON fixtures are committed.

**Explicitly out of scope:** literature search, vector DB, auth, Supabase,
Modal, experiment execution, graph visualization, deployment infra.

**Caveats:** cross-paper links are model-proposed (validated for reference
integrity, not truth); adding a paper never revises earlier papers' deltas;
project.json is single-writer (no concurrency); in-browser visual check left
to the demo.
