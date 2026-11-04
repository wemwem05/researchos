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

Secrets are read from the environment (a local `.env` file works too;
never commit it). See `.env.example` for the full list: `MODEL_API_KEY`
(PDF analysis), `BASETEN_API_KEY` (agentic reasoning, model
`meta-models/muse-spark-open-1026`), `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`
(computational verification), and optional `OPENALEX_API_KEY`/`OPENALEX_MAILTO`
(literature discovery, works keyless with lower rate limits).

### Demo data & PDFs on a fresh clone

Paper PDFs are intentionally git-ignored (redistribution rights for published
papers are unclear). A fresh clone ships all code plus the model-generated JSON
demo fixtures under `web/public/data/`, so audits, agent runs, Modal reports
and cross-paper evidence are inspectable without keys or PDFs — but the Reader
shows overlays without underlying PDF pages until you supply the papers.

To restore the full EOT + Deep Shells demo, download from the official open
sources and place them at these exact local paths (or upload any PDF through
the browser in a running project — no CLI needed):

- `samples/Le_Integrating_Efficient_Optimal_Transport_and_Functional_Maps_For_Unsupervised_Shape_CVPR_2024_paper.pdf`
  — CVPR 2024 open access: "Integrating Efficient Optimal Transport and
  Functional Maps for Unsupervised Shape Correspondence"
- `samples/NeurIPS-2020-deep-shells-unsupervised-shape-correspondence-with-optimal-transport-Paper.pdf`
  — NeurIPS 2020 proceedings: "Deep Shells: Unsupervised Shape Correspondence
  with Optimal Transport"
- `web/public/paper.pdf` and `web/public/papers/eot-maps.pdf`,
  `web/public/papers/deep-shells.pdf` — local copies of the same two papers
  for the legacy reader fixtures (same content, see `docs/FINAL_REPO_AUDIT.md`).

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

## Milestone 6: Autonomous Computational Verification

A researcher-selected claim is planned, executed in an isolated Modal
Sandbox, and honestly interpreted (`prototype/verify_graph.py`, same
Spark Open model via Baseten, same LangGraph/checkpoint/telemetry
architecture as Milestone 5):

```bash
source .venv/bin/activate
python prototype/agent.py verify-claim \
  --project-dir prototype/projects/ot-correspondence \
  --paper-id eot-maps --claim-id claim-3
# answer any human review (expensive/unsafe/unavailable-data only):
python prototype/agent.py review --project-dir prototype/projects/ot-correspondence \
  --thread-id <id> --choice "Proceed anyway (record limitation)"
python prototype/agent.py stage-web --project-dir prototype/projects/ot-correspondence --web-dir web
```

Nothing runs automatically: verification starts explicitly per claim from
the CLI or the Reader's "Verify computationally" action (via the local
agent server, refs only). CPU-only, network-blocked sandboxes with
pinned dependencies; bounded repair (max 3); verdicts are supported /
not supported in tested conditions, inconclusive, execution failed, or
not computationally testable — never a universal "reproduced" badge.
Evidence (code + hash, env, params/seeds, measurements, plot, logs,
telemetry, interpretation, limitations) attaches to the originating
paper and shows in the Project view's Verification section.

## Milestone 6.1: Scientific Evidence Audit

The default claim check is an audit, not a reproduction
(`prototype/audit_graph.py`, same Spark Open model and LangGraph/
checkpoint/telemetry architecture as Milestones 5–6):

```bash
source .venv/bin/activate
python prototype/agent.py audit-claim \
  --project-dir prototype/projects/ot-correspondence \
  --paper-id eot-maps --claim-id claim-1 --external
# external literature is optional and OFF unless --external is passed
python prototype/agent.py review --project-dir prototype/projects/ot-correspondence \
  --thread-id <id> --choice "Accept audit as-is"
python prototype/agent.py stage-web --project-dir prototype/projects/ot-correspondence --web-dir web
```

Spark Open classifies the claim, plans checks, and assesses
methodology/cross-paper/critic concerns; deterministic Python executes
every numerical recomputation (with value traceability and
paper-precision tolerance). External literature is bounded (≤2
queries, ≤10 records, ≤3 OpenAlex papers, abstract-level labels).
Verdicts are evidence-scoped (substantiated / partially substantiated
/ discrepancies identified / inconclusive / insufficient evidence) —
a different vocabulary from computational statuses, on purpose.
Methodology and limitations: `docs/EVIDENCE_AUDIT.md`. The Reader's
primary claim action is now "Audit scientific claim"; "Verify
computationally" remains as the distinct optional test.

## Milestone 7: Real Research Workspace

Multi-project application layer around the preserved M1–M6.1 intelligence.
Create projects, edit the research question (versioned), upload PDFs from the
browser, watch real pipeline jobs, and read papers in the live Reader — the
backend is authoritative and everything persists across restarts:

```bash
./dev.sh
# backend  http://127.0.0.1:8765/api/health
# frontend http://localhost:3000
```

`./dev.sh` starts the workspace backend (`agent.py serve-all`) and Next.js
together with clean Ctrl-C shutdown, reusing a healthy backend instead of
starting a duplicate. Separate commands stay available for debugging (see the
script header). Uploads validate size/pages/PDF integrity, dedupe by content
hash, reuse verified document caches, and run the full
analyze → relevance → localize → investigate pipeline as durable SQLite jobs
with honest stage progress — never fake percentages. Question edits snapshot
old conclusions into versioned history, re-score relevance only, and rebuild
findings through the real agent graph after explicit confirmation. Legacy
routes (`/project`, `/read/[paperId]`, `/api/agent/*`) keep working against
the staged demo fixtures and default project.

Architecture: `docs/WORKSPACE_ARCHITECTURE.md`. Local single-user use only:
production would need auth, hosted storage, and job isolation.

## Milestone 8: Autonomous Literature Discovery

From any project — even with zero papers — **Discover papers** starts a
bounded agentic run: Spark Open (`meta-models/muse-spark-open-1026`,
Baseten, no fallback) plans a search strategy, queries OpenAlex, and
evaluates candidates against the live project (findings, methods, open
questions, novelty risks); a critic gates the recommendations. The
researcher approves what to read; approved papers download through an
SSRF-guarded fetcher and ingest via the existing M7 pipeline, updating
findings and connections. Nothing is added without approval; relevance
is always labeled by evidence level (metadata / abstract / full text),
and novelty claims stay corpus-scoped.

Details: `docs/LITERATURE_DISCOVERY.md`.

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
- `prototype/modal_exec.py` — Milestone 6: Modal Sandbox runner (limits,
  network-blocked, pinned image, `FakeModal` double)
- `prototype/verify_graph.py` — Milestone 6: verification subgraph
  (classify/plan/code/execute/validate/interpret/repair/evidence/attach)
- `prototype/openalex.py` — Milestone 6.1: bounded OpenAlex client
  (query/record/paper caps, disk cache, `FakeOpenAlex` double)
- `prototype/audit_graph.py` — Milestone 6.1: evidence-audit subgraph
  (classify/collect/plan/checks/methodology/cross-paper/external/
  critic/revision/HITL/report/attach)
- `prototype/workspace.py` — Milestone 7: project registry, safe paths,
  per-project locks, question versioning
- `prototype/jobs.py` — Milestone 7: SQLite job store (lifecycle, retries,
  idempotency, restart recovery)
- `prototype/paper_pipeline.py` — Milestone 7: upload → validate → inventory
  → analyze → relevance → localize → investigate → persist (reuses M1–M5)
- `prototype/workspace_server.py` — Milestone 7: multi-project API + job
  dispatcher (legacy `/api/agent/*` preserved)
- `dev.sh` — Milestone 7: unified backend + frontend startup
- `prototype/discovery_models.py` — Milestone 8: candidate/run models,
  ID normalization, deterministic dedup, novelty clamp
- `prototype/discovery_graph.py` — Milestone 8: discovery LangGraph
  (plan/retrieve/dedupe/shortlist/evaluate/reflect/critic/revise/
  prepare/persist) on the shared SQLite checkpointer
- `prototype/acquisition.py` — Milestone 8: SSRF-guarded full-text fetch
  (redirect/host/size/MIME/magic guards, proxy-aware)
- `docs/LITERATURE_DISCOVERY.md` — Milestone 8: providers, graph,
  budgets, grounding, approval, import, limitations
- `docs/EVIDENCE_AUDIT.md` — Milestone 6.1: audit methodology,
  status semantics, limitations, and how audits differ from
  reproduction
- `prototype/test_analyze_paper.py`, `prototype/test_localize_objects.py`,
  `prototype/test_inventory.py`, `prototype/test_project.py`,
  `prototype/test_agent_graph.py`, `prototype/test_verify_graph.py`,
  `prototype/test_audit_graph.py`, `prototype/test_workspace.py`,
  `prototype/test_discovery.py` —
  offline tests (no API key):
  `.venv/bin/python -m unittest discover -s prototype -p "test_*.py"`
- `web/app/lib/relevance.test.ts`, `web/app/lib/project.test.ts`,
  `web/app/lib/agent.test.ts`, `web/app/lib/verification.test.ts`,
  `web/app/lib/audit.test.ts`, `web/app/lib/workspace.test.ts`,
  `web/app/lib/discovery.test.ts` —
  frontend unit tests (vitest): `cd web && npm test`
- `prototype/requirements.txt` — `openai`, `pydantic`, `python-dotenv`,
  `pymupdf`, `pillow`, `langgraph`, `langgraph-checkpoint-sqlite`, `modal`
- `web/` — Milestone 3 Next.js reader (`app/`, `public/paper.pdf`
  (local-only, git-ignored), `public/data/localized_output.json`)
- `samples/` — sample paper PDF
- `HACKATHON_LOG.md` — milestone log

## License

MIT — see `LICENSE`. Third-party paper PDFs stay local-only and are never
committed (see `.gitignore`); only model-generated JSON fixtures ship in
`web/public/data/`.
