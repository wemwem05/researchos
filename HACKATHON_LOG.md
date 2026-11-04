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

## Milestone 5 — Persistent Muse Spark Open Research Agent Graph (2026-11-03)

**Goal:** research is not a linear chain — pursue an objective across
reasoning steps that challenge their own conclusions, branch on evidence,
pause for genuine human judgment, persist, and resume without repeating
expensive work.

**Built (backend, all new; M1–M4 code untouched):**

- `prototype/spark_open.py` — Baseten client pinning exactly
  `meta-models/muse-spark-open-1026` (OpenAI-compatible, `BASETEN_API_KEY`
  only, generous token budget for the reasoning model, latency/usage/
  request-id telemetry, one bounded schema-repair attempt). No fallback to
  any other model: failures raise `SparkOpenError` carrying failure
  telemetry. Verified with a real call before building the graph.
- `prototype/agent_graph.py` — LangGraph topology: load_project_context →
  investigator (Spark) → validate → critic (Spark) → decision_gate →
  finalize | revise_once (Spark, max 1) | prepare_review + human_review
  (interrupt) → apply_final_decision → update_project_state (single
  mutation point reusing M4 `ProjectDelta`/`apply_delta`). Refinements:
  validate-fail ends failed without mutation; finalize converges on
  apply_final_decision; prepare_review checkpoints the request so the
  interrupt node has zero pre-interrupt side effects.
- Deterministic gates (unknown paper/finding/object ids, connection enum,
  ≤5 findings, confidence bounds, evidence presence, page integrity,
  corpus-scoped novelty denylist, duplicate ids, malformed state) with one
  bounded repair; anything invalid fails closed.
- SQLite checkpoints per project + file run records (transitions,
  decisions, validation failures, revision path, human request/response)
  + `telemetry.jsonl`. One thread per investigation run.
- `prototype/agent.py` CLI (investigate/status/review/stage-web/serve/
  verify) + `prototype/agent_server.py` (stdlib-only API for live HITL
  resume from the UI; resume path uses a model-call guard, no key needed).
- `prototype/synthetic_demo.py` — explicitly labeled canned HITL fixture
  (imaginary SYN-A/SYN-B, `[SYNTHETIC DEMO]` banner, provider
  `synthetic-demo`); never touches real project output.

**Live run (demo project = M4 project truncated to Deep Shells, EOT
re-interpreted through the graph; original project untouched):**
investigator 62.4s / 10,337 tokens (1 schema repair) → 5 grounded findings
+ 5 connections; deterministic validation clean; critic 28.3s / 7,893
tokens (1 schema repair) → 5 passed comparability checks, 3 minor
concerns, verdict SUPPORTED; clean accept, no revision, no human review —
no contradiction fabricated. Run record + telemetry staged under
`web/public/data/agent/`.

**HITL proof:** unit tests interrupt + resume after simulated restart with a
refusing model double (zero re-calls); real CLI `review` resumed the
synthetic run across processes (connection dropped per researcher choice);
live API server POST `/review` verified end to end.

**Built (frontend, `web/`):** `/project` gains an Agent Run timeline, a
Researcher Review card (question, why-stopped, both positions, evidence,
options, free text, live resume or CLI fallback), an AI Research Agent
provenance section (exact model id, provider, roles, calls/latency), and a
labeled synthetic walkthrough. Static-first (staged JSON), live when the
agent server runs. Reader routes unchanged.

**Verification:** 112/112 backend tests (27 new); 23/23 frontend tests
(9 new); `tsc` clean; `next build` green (webpack — sandbox blocks
Turbopack, as before); production smoke 200 on `/`, `/project`, both
readers, all fixtures.

**Explicitly out of scope:** Modal execution (Milestone 6), Glimmer,
literature search, vector DB, auth, Supabase, React Flow, deployment infra.

**Caveats:** both live Spark calls needed one schema repair (JSON shape
drift, fixed in-budget); project.json stays single-writer; in-browser
visual check left to the demo.

## Milestone 6 — Autonomous Computational Verification (2026-11-04)

**Goal:** researcher-selected claims get planned, executed (Modal Sandbox),
and honestly interpreted through a bounded subgraph on the M5 architecture.

**Built (backend):**

- `prototype/modal_exec.py` — Modal Sandbox wrapper: CPU-only (2 CPU /
  4 GiB), 10-min exec cap, `block_network=True`, pinned
  numpy==2.1.3/matplotlib==3.9.2 image, `filesystem` transfers, env probe
  (pip freeze + runtime), artifact pull before terminate. Auth via stock
  SDK resolution (env or `~/.modal.toml`); credentials never enter the
  sandbox. `FakeModal` double with scripted outcomes.
- `prototype/verify_graph.py` — select → classify → plan → validate →
  codegen → preflight → execute → validate → interpret | diagnose(≤3) →
  evidence → attach, with durable interrupt/resume for budget/unsafe/data
  escalations. Spark Open does classify/plan/code/diagnose/interpret;
  deterministic gates own budgets, imports, CSV schema, criteria math, and
  a status clamp (exec success alone ⇒ never "supported"; unmet criteria
  ⇒ at most inconclusive).
- `project.py`: `VerificationEvidence` + `verifications[]` (additive),
  atomic `save_project` (tmp + replace), idempotent attach.
- `agent.py`: `verify-claim`, kind-routed `review`/`status`/`stage-web`
  (evidence + code/CSV/plot/logs staged). `agent_server.py`: validated
  `verifications/start` (refs only, localhost), kind-routed review,
  allowlisted live artifact downloads.

**Live demo (EOT claim-3, real Modal runs):** classifier correctly judged
the accuracy numbers untestable without FAUST/SHREC19; researcher scoped
to the SW efficiency motivation via review. v3: SW 2.7–5.1 ms vs Sinkhorn
0.33–28.0 s (n=256–2048, 5 reps + warm-up, 40 rows, real plot) — but the
pre-registered symmetry sanity criterion failed, so the verdict is
honestly NOT SUPPORTED with numbers under "partially tested". v1 shows
not_testable, v2 execution_failed (live repair exhaustion). One
mislabeled v2 from an earlier run was withdrawn with an audit note after
a status-label bugfix (live2 run record preserves the story).

**Bugs found live and fixed (each with regression tests):** data
escalation now precedes not_testable; failed Spark nodes route to END
instead of falling through; diagnose exhaustion routes to evidence (was
a potential loop); codegen budget 32k (16k truncated); Modal exec
timeout surfaces as rc=-1 without raising (now inferred); sanity flags
are findings for interpretation, not repair fodder.

**Built (frontend):** "Verify computationally" on Reader claims (confirm →
live timeline/review/evidence), Project Verification section (observed
vs interpreted, criteria, table, plot, artifacts, limitations). UI start
+ abort proven live against the API on a scratch project.

**Verification:** 142/142 backend (30 new); 28/28 frontend (5 new);
`tsc` clean; `next build` green (webpack); production smoke 200 on all
routes, staged artifacts, and live API/file endpoints.

**Caveats:** symmetry sanity tolerances in generated code can be
stricter than Monte-Carlo noise allows (drove the honest not_supported);
shared repair budget covers codegen + exec (per spec); project.json
stays single-writer; in-browser visual check left to the demo.

## Milestone 6.1 — Scientific Evidence Audit (2026-11-04)

**Goal:** verify claims even when reproduction is impractical: audit the
paper's own record (recomputed numbers, methodology, project findings,
optional bounded literature) without claiming to have rerun the work.
Default "verify" becomes EVIDENCE AUDIT; M6 Modal testing stays a
separate optional action, never auto-substituted.

**Built (backend):** `prototype/openalex.py` (bounded OpenAlex client:
≤2 queries/≤10 records/≤3 papers, disk cache, fail-soft, abstract-only
labels, `FakeOpenAlex` double); `prototype/audit_graph.py` (LangGraph
audit workflow on the M5/M6 checkpoint/telemetry/review infrastructure:
classify → collect → plan → deterministic numerical checks → methodology
→ cross-paper → external gate → critic → finalize/revise/review →
report → attach); `ScientificAudit` project model (atomic, idempotent
attach, separately attributed conclusions); `audit-claim` CLI;
`audits/start` + `audits` server endpoints (refs only); staging into
`web/public/data/agent/audits(.json)`. Spark Open
(`meta-models/muse-spark-open-1026` via Baseten) reasons; Python owns
all arithmetic with value traceability and paper-precision rounding
tolerance. Overall assessments are evidence-scoped and disjoint from
M6 statuses. Methodology: `docs/EVIDENCE_AUDIT.md`.

**Live demo (EOT claim-1 SOTA + claim-3 math/efficiency):** 34 real
Spark calls (~229k tokens, all Baseten `...-1026`); live OpenAlex
search returned a real 2026 source; 6 audits attached (a1–a6).
Numerical checks pass post-rounding-fix; overalls read "discrepancies
identified" from established methodology observations
(single-protocol ablation, necessity language outrunning Table 4).
Two live fail-closed HITL resumes (critic schema slips) repeated zero
Spark calls — one via CLI, one fully through the UI endpoints
(HTTP start → interrupt → HTTP review → completed a5). Final run:
clean SUPPORTED → finalize.

**Bugs found live and fixed (each with regression tests):** strict
1e-9 tolerance flagged paper-precision rounding as discrepancies →
rounding half-unit tolerance; planner string-instead-of-list fields →
lenient validators + explicit schema rules in the prompt; critic
`human_question.question=null` → null-normalizing validators on the
shared M5 verdict models (backward compatible); critic emitting
bare-string checks/concerns → shape spec added to the audit critic
prompt (mirrors M5); failed Spark nodes now route to END via
conditional edges instead of falling through.

**Built (frontend):** primary "Audit scientific claim" on Reader
claims (confirm with scope + external toggle default OFF showing the
2/10/3 budget → live audit timeline → review → full report), optional
"Verify computationally" kept distinct; Project "Evidence audits"
section (live + staged); audit timeline steps in the shared Agent Run
UI (no new incompatible system).

**Verification:** 173/173 backend (31 new); 34/34 frontend (6 new);
`tsc` clean; `next build` green (webpack); staged audits
shape-validated; agent-server audit start/review loop proven live;
`/` + `/project` serve 200 from the production build.

**Caveats:** section-level retrieval unavailable (object evidence +
page excerpts only); lexical value tracing can NOT_EVALUATE a
correctly phrased-differently number; overalls inherit planner
coverage; sandbox HTTP proxy blocked Reader-route smoke from the
shell (in-browser visual check left to the demo); M1–M6 preserved
(8 findings, 3 connections, 3 verifications intact).

## Milestone 7 — Real Research Workspace (2026-11-04)

**Goal:** turn ResearchOS from a static demo into a usable application: a new
researcher creates projects, edits the research question, uploads PDFs, and
reads them in the live Reader — all through the browser against the real
M1–M6.1 backend. No JSON editing, no CLI.

**Built (backend, all new; M1–M6.1 reused, not rewritten):**

- `prototype/workspace.py` — project registry under `prototype/projects/`
  (stable ids, titles, questions, timestamps, versions), safe path resolution
  (traversal/symlink rejection), per-project single-writer locks, question
  preview + archival change semantics.
- `prototype/jobs.py` — SQLite job store (`workspace.sqlite`): QUEUED/
  RUNNING/COMPLETED/FAILED/CANCELLED, real stage tracking, idempotency keys,
  bounded attempts, retry/cancel, restart reconciliation.
- `prototype/paper_pipeline.py` — upload → validate (size/pages/magic/parse/
  encryption) → inventory → analysis → relevance → localization → Spark
  investigation → atomic persist. Reuses `inventory`, `analyze_paper`,
  `localize_objects` helpers + shared `loc_cache.json`, and
  `agent.cmd_investigate`; content-hash dedupe plus verified same-bytes
  document-cache reuse (provenance recorded); per-paper stage files make
  retries resume without repeating expensive stages. Model clients injectable
  (`PipelineDeps`) for deterministic tests.
- `prototype/workspace_server.py` — multi-project API (enveloped JSON) +
  single-dispatcher job runner: projects CRUD, multipart upload, paper
  file/data bytes, jobs, question preview/confirm, rebuild continuation, and
  project-scoped runs/reviews/telemetry/audits/verifications. Localhost bind,
  explicit CORS allowlist, no client-supplied paths. Legacy `/api/agent/*`
  routes keep exact M5/M6/M6.1 shapes against `ot-correspondence`. Start
  endpoints validate refs first, then probe Baseten/Modal and fail with an
  honest 503 instead of a silent 202.
- `agent.py serve-all` — workspace server CLI. `dev.sh` (+ `npm run dev:all`)
  starts backend + frontend together with clean shutdown and
  duplicate-backend reuse.

**Question versioning:** paper-native state (PDFs, inventory, boxes) is
preserved; relevance re-scores only; old conclusions snapshot into versioned
history (never shown as current); findings rebuild through the real agent
graph after preview + confirmation; audits/verifications/runs keep
originating versions. Rebuilds paused by review continue via `POST /rebuild`.

**Built (frontend):** `/` is now the Projects dashboard (create/open, classic
demo links); `/projects/<id>` is the live project page (question editing,
multi-PDF upload, job progress + retry, findings, history, reviews, audits,
verifications); `/projects/<id>/read/<paperId>` is the live Reader fed by the
API with project-scoped audit/verify actions. `Reader`/`ResearchLens`/
claim components accept optional live/project props; legacy `/project` and
`/read/[paperId]` behavior is unchanged.

**Live acceptance ("Transport Geometry Investigation", real API + real
models):** project created via API; EOT uploaded (25 objects, 22/22 boxes, 5
live findings); Deep Shells uploaded (9 findings, 4 connections total);
question changed to sliced-vs-entropic efficiency/stability (PDFs/boxes
byte-identical, both papers re-scored live for the new question — EOT 3 high /
6 related / 16 context, Deep Shells 4 / 3 / 16 — v1's 9 findings archived,
v2 rebuilt to 10 findings + 6 connections after one fail-closed review
answered via API); one live corpus-only audit attached ("discrepancies
identified"); verification action confirmed available with an honest
Modal-unavailable 503 (no credentials here); restart recovery proven
(requeued 1, persisted 2/10/6/1). 17 Spark Open calls, all on
`meta-models/muse-spark-open-1026` (~132k tokens). Demo project bytes
identical before/after (sha `0e2368b0…`).

**Bugs found live and fixed (each with regression tests):** investigator +
critic JSON shape drift (`from`/`to`, `question`, bare strings — the Baseten
path has no schema enforcement, so exact shape specs went into both prompts,
text only); jobs misreported awaiting-review runs as complete (exit 0 covers
both — attachment now decides, plus the rebuild endpoint); `apply_delta`
silently dropped verifications/audits on add-paper (now preserved, new
conclusions stamped); empty idempotency keys collided (partial unique
index); multipart parser ate significant trailing bytes (exact framing).

**Verification:** 211/211 backend tests (38 new); 39/39 frontend tests (5
new); `tsc` clean; `next build` green (webpack — sandbox blocks Turbopack, as
before); live Reader data/file endpoints return 25/23 objects, 22/20 boxes,
and byte-identical PDFs for both acceptance papers; demo project intact
(2 papers, 8 findings, 3 connections, 3 verifications, 7 audits).

**Explicitly out of scope:** auth, hosted storage, multi-worker dispatch,
rate/budget limits, internet paper discovery, accounts, billing,
collaboration, graph visualization, Milestone 8.

**Caveats:** sandbox forbids port binding, so the acceptance drove the real
server stack in-process (identical handler bytes; `./dev.sh` reproduces over
TCP on a normal machine) and no live browser check was possible; in-browser
visual check left to the demo. Investigator/critic schema drift is mitigated
by shape specs + fail-closed review, not eliminated — a third consecutive
drift would exhaust the job's retry budget and surface honestly. Modal
execution remains unproven in this environment (no credentials).

### Milestone 7 follow-up: duplicate history keys + non-paper uploads (2026-11-04)

Two issues reported from live use, both fixed with regression tests:

- **Duplicate React keys in Question history.** `apply_question_change`
  appended an archived v1 record next to the empty initial v1 record, so two
  children shared `key={h.version}`. The backend now keeps exactly one record
  per version (updates the existing record in place; legacy projects without
  one still get an appended archive), and the frontend dedupes by version
  (snapshot wins) with composite keys — existing duplicated data displays
  correctly without a migration.
- **Non-paper upload burned 3 model calls, then dumped a raw ValidationError.**
  A 6-page Russian student report (no labeled equations/figures/tables —
  deterministic checklist legitimately empty) failed stage-1 coverage on all
  three API-shape attempts. Fixes: (1) `InvalidAnalysisContent` distinguishes
  answered-but-invalid content from transport failures, so the fallback chain
  aborts after 1 call instead of 3 (transport fallback preserved); (2) when
  the empty checklist and the model agree, the job fails honestly as
  non-retryable ("doesn't appear to be a scientific paper… remove it and
  upload a scientific PDF"); model misses on labeled papers stay retryable;
  (3) `PipelineError.retryable` persists on the job and the UI shows "Retry
  won't help" instead of a Retry button; (4) new `DELETE
  /api/projects/<id>/papers/<pid>` removes staged/failed papers (attached
  papers protected, processing papers refused) with a Remove button in the
  project view.

**Verification:** 219/219 backend (8 new), 40/40 frontend (1 new), `tsc`
clean, `next build` green (webpack).

### dev.sh shutdown fix (2026-11-04)

Ctrl-C killed the `npm` wrapper but orphaned the Next.js child holding port
3000, so the next `./dev.sh` died with `EADDRINUSE`. `dev.sh` now runs each
service in its own process group (`set -m`) and shuts down whole trees
(TERM with KILL escalation, zombie-aware so clean exits stay fast), kills a
lingering port holder as a fallback, and fails early with an actionable
message (holder PID + `kill` command) when a port is already busy. Group-kill
and escalation proven with stand-in process trees (`bash -n` clean; live
`./dev.sh` exercising left to a normal machine since this sandbox forbids
binding).

Follow-up fix the same day: the new port guard silently killed the script on
a *free* port — `lsof` exits 1 when nothing listens, and `set -o pipefail` +
`set -e` turned `HOLDER="$(port_listener …)"` into a silent exit right after
the backend-reuse message (exactly the reported transcript). `port_listener`
now always succeeds (empty = free) and `is_dead` got the same hardening;
both paths proven under identical shell flags with a stubbed `lsof`.

Stale-backend follow-up the same day: Remove failed with a bare "Failed to
fetch" because the reused backend predated the DELETE route *and* its CORS
entry, so the preflight blocked the request before it was sent. Fixes: (1)
`/api/health` now reports `SERVER_VERSION` (`"7.2"`) and `dev.sh` refuses to
reuse a version-mismatched backend, printing the holder PID and exact `kill`
command instead; a test pins `dev.sh`'s expectation to the server constant.
(2) OPTIONS now returns a proper empty 204 (no body). (3) The Remove client
translates network failure into "restart the backend" guidance. Unblocks via
`kill $(lsof -ti tcp:8765 -sTCP:LISTEN)` + `./dev.sh`. 222/222 backend,
40/40 frontend, `tsc` clean, `next build` green.

Removal follow-up the same day: the first Remove of the failed `report`
paper *succeeded* (dir gone), but the FAILED job row resurrected its card,
so a second Remove 404'd with "unknown paper 'report'". Fixes: (1)
`paper_status` no longer emits unattached papers whose dir is gone unless a
job is active — removed papers stay removed (history lives in the jobs
list); (2) DELETE is idempotent (`already_gone: true` on re-delete, so
stale UI/double-clicks are harmless); (3) DELETE refuses papers with a
pending human review (would orphan the resume); (4) retrying a removed
paper's job refuses early with "re-upload to try again"; (5) retry-action
errors now surface in the UI instead of an unhandled rejection. Server
version bumped to 7.3 (+ `dev.sh` expectation). 224/224 backend, 40/40
frontend, `tsc` clean, `next build` green.

## Milestone 8 — Autonomous Literature Discovery (2026-11-04)

**Goal:** a researcher with a question (and zero or more papers) gets
agent-discovered, project-grounded reading recommendations — approved by
them, imported through the existing pipeline. Real agent workflow, not a
search box.

**Built (M1–M7 preserved, wrap-don't-rewrite):**

- `prototype/discovery_models.py` — candidate/run/budget/query models,
  DOI/OpenAlex/arXiv/title normalization, `DedupIndex` + `dedupe_records`
  (stable ids, arXiv-version-insensitive, exact-title fallback),
  evidence levels, novelty-overclaim clamp.
- `prototype/discovery_graph.py` — LangGraph workflow on the shared
  SQLite checkpointer: load_context → plan_search → retrieve_candidates
  → normalize_and_deduplicate → shortlist → evaluate_relevance →
  reflect → discovery_critic → decision_gate → bounded_revision? →
  prepare → persist. Spark Open (exact `meta-models/muse-spark-open-1026`,
  no fallback) plans, evaluates, reflects, critiques, revises; Python
  does hygiene/dedup/shortlist/rank/budgets. No `interrupt()` —
  approval is post-completion API. Budgets: 2 rounds, ≤3 queries/round,
  ≤50 records, ≤8 shortlist, ≤5 recommended, ≤2 expansions, ≤3 deep
  inspections, ≤8 Spark calls.
- `prototype/openalex.py` (extended) — richer records (venue, dates,
  arxiv, license, work type, OA flags), `search_filtered`/`get_work`/
  `works_batch`/`referenced_ids`, backoff + throttle + attribution +
  disk cache. `pdf_url` now only from real OpenAlex PDF fields (a
  landing-URL fallback bug fixed). Semantic Scholar evaluated, rejected.
- `prototype/acquisition.py` — SSRF-guarded full-text fetch: scheme/
  port/credential/host rules, per-hop redirect re-validation, global-
  routable DNS for direct connections (literals always checked, proxied
  egress defers to the proxy), size/MIME/magic guards, atomic staging.
- `prototype/workspace_server.py` (extended, v8.0) — `discover_papers`
  + `import_discovery` job types on the M7 dispatcher; routes
  `discoveries/start|list|detail|decide|import` with validated budgets,
  idempotent start keys, per-item decide/import results, deterministic
  paper slugs, duplicate + unavailable handling, review-aware import
  finalization with self-heal, and `pipeline.json` discovery provenance.
  `apply_delta` now preserves `discoveries` (same bug class as M7's
  audit fix).
- `web/app/lib/discovery.ts` + `web/app/components/DiscoverySection.tsx`
  — focus input, budget line, progress, run selector with stale badges,
  five filters, expandable candidate cards (verdict/evidence/novelty,
  five grounded reasons, abstract, ids, query provenance), approve/
  dismiss/source/import per paper + bulk, import results, provenance
  disclosure, Reader deep-links for imports.
- `docs/LITERATURE_DISCOVERY.md` — providers, graph, budgets, grounding,
  novelty limits, approval, acquisition, versioning, telemetry.

**Live acceptance (safe `/tmp` copy of `ot-correspondence`; real Spark +
OpenAlex + Meta APIs; original bytes unchanged):** the agent chose 6
queries over 2 adaptive rounds (strategy notes reference project
findings f1–f8; round 2 pivoted to Gromov-Wasserstein / unbalanced OT /
ground-cost ablations) → 50 retrieved, 1 duplicate, 8 shortlisted, 5
recommended in 147 s (5 Spark calls, ~69k tokens), critic SUPPORTED, no
revision. Approved dc32 ("Partial transport for point-cloud
registration", Springer OA 1.9 MB, doi:10.1007/s43670-025-00097-1):
downloaded, ingested, analyzed (61 objects), localized (34), +5 grounded
findings f9–f13, +7 cross-paper connections (8→13 findings, 3→10). Honest
negatives: 43 MB arXiv PDF rejected by the size cap, MDPI bot-filter /
HTML interstitial rejected as non-PDF, dc-label expansion ids dropped
with logged notes. Restart persistence, Reader files, and provenance
verified in a fresh process.

**Bugs found building/live (each with regression tests):** usage
accounting lost at checkpoints (returned explicitly); checkpointer
context-manager misuse; undefined `DiscoveryFailure`; evidence-case
corruption (re-validation on status update); title-only dedup gap;
premature imported-on-review; 50-record persistence bloat (actionable
subset only); proxy-unaware DNS guard.

**Verification:** 295/295 backend (71 new), 50/50 frontend (10 new),
`tsc` clean, `next build` green (webpack).
