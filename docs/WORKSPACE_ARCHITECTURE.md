# ResearchOS Workspace Architecture (Milestone 7)

Milestone 7 turns ResearchOS from a static, preconfigured demo into a usable
research application: a new researcher creates projects, edits the research
question, uploads PDFs, and reads them in the interactive Reader — all through
the browser, backed by the real M1–M6.1 pipeline. No JSON editing, no CLI.

## Design principle

The M1–M6.1 intelligence is preserved untouched in behavior: PDF analysis,
inventory/relevance, localization, the Reader, cross-paper project logic, and
the investigator/critic, verification, and audit graphs. Milestone 7 builds an
application layer *around* them and reuses the working Python modules. The only
M1–M6.1 source changes are backward-compatible extensions:

- `project.py` — optional workspace fields (title, timestamps, question
  version/history), `question_version`/`status` on conclusions, `sha256` on
  papers; `apply_delta` now preserves verifications/audits/metadata (it
  previously dropped them — a latent wipe bug) and stamps new conclusions.
- `agent.py` — `_paper_entry` accepts `sha256`/`question_version` extras and
  resolves the localized file from CWD-then-repo-root; new `serve-all` command.
- `agent_graph.py` — exact JSON shape specs added to the investigator and
  critic prompts (prompt text only; the Baseten path has no server-side schema
  enforcement, and field-name drift caused live failures — see HACKATHON_LOG).
- `verify_graph.py` / `audit_graph.py` — attached records stamp the
  originating `question_version` (legacy records read as 0/unknown).

## Multi-project storage

```
prototype/projects/                 # git-ignored; backend state is authoritative
  ot-correspondence/                # existing demo project (never migrated)
    project.json                    # scientific state (file-backed, atomic writes)
    papers/<paper-id>/              # NEW pipeline papers only
      source.pdf                    # original upload, byte-identical
      original_name.txt             # display name (never a path)
      draft.json                    # question-independent inventory (AnalysisDraft)
      analysis.json                 # relevance-scored analysis (AnalysisResult)
      localized.json                # analysis + boxes (LocalizedOutput)
      checklist.txt / pipeline.json # deterministic labels / stage markers
    agent/                          # checkpoints.sqlite, runs/, telemetry.jsonl,
                                    # verifications/, audits/, openalex_cache/
  <new-project>/                    # same layout, created via API
  workspace.sqlite                  # jobs ONLY (WAL mode); never science
```

Legacy M4 CLI papers keep their `pdf_path`/`localized_path` repo-relative
pointers and resolve through the same endpoints. Static fixtures under
`web/public/` remain as legacy/demo fallbacks but are never written by the
workspace; newly created projects live only in the backend.

`prototype/loc_cache.json` (keyed by PDF hash + object + page, never
relevance) is shared across projects: identical bytes reuse verified boxes.

## API architecture

`prototype/workspace_server.py` (stdlib-only, like its predecessor) binds
`127.0.0.1:8765` by default, with an explicit CORS allowlist for the local
frontend origins (`http://localhost:3000`, `http://127.0.0.1:3000`) — never
`*`. New routes use a `{ok, data} / {ok:false, error, details?}` envelope;
the two Reader bytes endpoints intentionally return raw bodies:

- `GET /api/health`, `GET/POST /api/projects`, `GET/PATCH /api/projects/<id>`
- `GET /api/projects/<id>/papers`, `POST .../papers` (multipart upload),
  `GET .../papers/<pid>`, `DELETE .../papers/<pid>` (staged/failed papers
  only; attached papers are protected), `GET .../papers/<pid>/file` (raw PDF),
  `GET .../papers/<pid>/data` (raw localized JSON)
- `GET /api/projects/<id>/jobs`, `GET /api/jobs/<job>`,
  `POST /api/jobs/<job>/retry|cancel`
- `GET /api/projects/<id>/question/preview`,
  `POST /api/projects/<id>/question` (`confirm` required to apply),
  `POST /api/projects/<id>/rebuild` (continue an incomplete rebuild)
- Project-scoped agent routes: `runs`, `runs/<t>`, `runs/<t>/review`,
  `telemetry`, `verifications[/start]`, `verifications/<t>/files/<n>`,
  `audits[/start]`
- Legacy `/api/agent/*` routes keep their exact M5/M6/M6.1 shapes and map to
  the default project (`ot-correspondence`).

No endpoint accepts client filesystem paths. Project ids and paper ids are
validated slugs; resolution rejects traversal and symlink escape. Every agent
action is scoped to its project: reviews resume the thread in the correct
project's checkpoint file, and start endpoints validate claim refs first, then
probe Baseten/Modal and fail with an honest 503 (never a silent 202).

## Job lifecycle

Paper work runs as durable jobs (`QUEUED → RUNNING → COMPLETED`, plus
`FAILED`/`CANCELLED`). A single in-process dispatcher claims the oldest queued
job; progress tracks real pipeline stages (`validating`, `reading_document`,
`identifying_objects`, `analyzing_content`, `ranking_relevance`, `localizing`,
`comparing_with_project`, `finalizing`, …) — never time-derived percentages.

- Idempotency keys (`ingest:<paper>:<sha>`, `rescore:qv<N>`) make double
  submits and refresh-resubmits return the existing job.
- Per-paper stage files let retries resume: completed expensive stages
  (analysis, localization) are never re-run; relevance re-scoring is cheap
  and always fresh.
- Bounded attempts (3); only `FAILED`/`CANCELLED` jobs retry — and only
  when the failure is retryable (document-caused failures such as non-paper
  uploads record `retryable: false` so the UI doesn't offer a pointless
  retry).
- Restart recovery (`reconcile_after_restart` on boot): `RUNNING` rows return
  to `QUEUED` (or `FAILED` when the budget is exhausted); `COMPLETED` rows
  are untouched. `project.json` writes stay atomic (tmp + replace) and each
  project has a single-writer lock.
- Human review is not a failure: a job whose investigation awaits review
  completes with `{awaiting_review}` and the paper attaches on resume — with
  zero repeated model calls (LangGraph checkpoints).

## Question-version semantics

Paper-native state (PDFs, inventory, explanations, boxes) is question
*independent*; relevance, findings, connections, questions, and risks are
question *dependent*. On a confirmed question change:

1. The new version is saved (`question_version` bumps, history grows).
2. Current conclusions move into a `superseded` history snapshot (never
   deleted, never shown as current); live arrays are cleared.
3. Every paper is re-scored for relevance only (draft + boxes reused).
4. Findings rebuild via the real investigator/critic graph in paper order.
5. Audits, verifications, agent runs, and experimental artifacts are preserved
   with their originating versions; measurements stay measurements —
   re-interpretation never rewrites them.

The UI shows a preview of the expected work (papers to re-score, conclusions
to archive, what is preserved vs recomputed) and requires confirmation. While
recomputation runs, an explicit updating banner shows; stale findings are
never displayed as current, and a `POST /rebuild` continuation finishes
rebuilds paused by human review.

## Upload processing

Browser → multipart → `validate_pdf` (size/page limits, `%PDF-` magic,
parseability, encryption refusal) → content-hash dedupe per project →
verified document-cache reuse (same-bytes draft + boxes, provenance recorded)
or full analysis → relevance → localization → agent investigation → atomic
persist. Filenames are display-only; storage uses server-generated ids under
the project's `papers/` dir. Uploaded PDFs are never committed and never
redistributed. Model substitution is forbidden: perception uses the Meta Model
API, agentic reasoning uses exactly `meta-models/muse-spark-open-1026`.

## Backend/frontend communication

The Next.js app calls the workspace API directly (`web/app/lib/workspace.ts`).
Routes:

- `/` — Projects dashboard (create/open; classic demo links preserved)
- `/projects/<id>` — live project page (question editing, upload, jobs,
  findings, history, runs, audits, verifications)
- `/projects/<id>/read/<paperId>` — live Reader (data + PDF from the API,
  project-scoped audit/verify actions)
- `/project`, `/read/<paperId>` — legacy static routes, unchanged in behavior

Polling (3–5 s, only while work is active) was chosen over SSE for simplicity
and restart-safety. Empty states render naturally; one project's findings
never leak into another.

## Project isolation

Isolation is structural: per-project directories, per-project checkpoint
files, per-project locks, per-request id validation, and job rows keyed by
project. The test suite covers cross-project paper/job/thread invisibility.

## Compatibility with prior milestones

The `ot-correspondence` demo (2 papers, 8 findings, 3 connections, 5
questions, 4 risks, 3 verifications, 7 audits) loads unchanged — old
`project.json` files gain defaults, and its bytes are never rewritten by the
workspace. All M1–M6.1 tests still pass unmodified.

## Limitations before production deployment

This milestone is a local single-user application. Production would require:
authentication and authorization (mutation and model-triggering endpoints are
currently unauthenticated localhost-only); durable hosted storage (local
files/SQLite); job isolation and multi-worker dispatch (one in-process
dispatcher, process-local locks); rate/budget limits per user; HTTPS,
CSRF dismissals, and secret management; PDF sandboxing beyond parse-only
handling; and backup/restore for project state.
