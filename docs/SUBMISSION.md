# ResearchOS — Hackathon Submission

## Project name

ResearchOS — an autonomous scientific research workspace.

## One-line pitch

ResearchOS turns a research question into a living workspace: it discovers
literature, reads papers with you, connects findings across papers, audits
evidence, and runs computational checks — with persistent AI agents and
honest uncertainty.

## Target user and problem

Researchers drowning in papers and disconnected tools: search in one place,
PDFs in another, notes in a third, and no durable memory of what the
evidence actually supports. Generic AI chatbots summarize plausibly but
ground nothing, persist nothing, and verify nothing.

## Product solution

A question-centered workspace where every artifact stays linked to the
research question and its evidence: discovered papers, localized
equations/figures/tables, cross-paper findings, audits, and executed
experiments. Autonomous agents do the research legwork; the researcher
reviews, approves, and decides.

## Core features (all implemented and demonstrated)

- **Research projects** — create a project, pose and revise a research
  question with full versioning (stale conclusions are marked historical,
  never silently current).
- **Interactive PDF Research Lens** — equations, figures, tables, and
  claims localized on the page; Focus/Show All navigator; relevance
  explanations grounded in paper and question.
- **Autonomous literature discovery** — the agent plans OpenAlex search
  strategies, evaluates candidates against live project state, reflects,
  passes a critic gate, and recommends what to read; the researcher
  approves, and full text imports through the standard pipeline.
- **Cross-paper reasoning** — findings, typed connections (supports /
  contradicts / extends / …), open questions, corpus-scoped novelty
  risks, all with paper/page/object references.
- **Scientific evidence audits** — numerical recomputation, methodology
  assessment, cross-paper comparison, source traceability, unresolved
  concerns. No code executed; never conflated with experiments.
- **Computational verification** — feasible claims tested by code executed
  in isolated Modal sandboxes, with plans, measurements, artifacts, and
  scoped conclusions. A passing test never counts as reproduction.
- **Persistent agents + human review** — LangGraph workflows on SQLite
  checkpoints survive restarts; the agent pauses with structured review
  requests and resumes from the researcher's judgment.

## Genuine novelty / differentiation

1. Question-aware everything: uploaded analyses are re-interpreted per
   research-question version instead of one-shot summaries.
2. Evidence honesty as architecture: evidence levels, stale marking,
   corpus-scoped novelty, and separated audit-vs-experiment vocabularies
   are enforced in code, not copy.
3. Discovery-to-verification loop in one workspace, with researchers
   approving every ingestion and every judgment call.

## Meta tooling used

- **Muse Spark** (hosted Meta Model API) — PDF scientific-object
  analysis, relevance ranking, figure/equation localization grounding.
- **Muse Spark Open**, exact model id **`meta-models/muse-spark-open-1026`**,
  served through **Baseten** (`https://inference.baseten.co/v1`) — every
  agentic decision: investigation, critic, audits, verification planning,
  and literature discovery. No model substitution anywhere in agent paths.
- Full usage map: `docs/OPEN_WEIGHT_USAGE.md`.

## Autonomous agent architecture

LangGraph + SQLite checkpoints, one durable thread per run:
investigator/critic with human interrupts (M5), Modal verification graph
(M6), evidence-audit graph (M6.1), discovery graph with reflection and a
bounded-revision critic gate (M8). Deterministic validation, voting, and
persistence live in Python around the model calls. See
`docs/WORKSPACE_ARCHITECTURE.md`.

## Modal integration

`prototype/modal_exec.py` runs agent-planned CPU benchmarks in isolated
Modal sandboxes (Debian-slim image, pinned packages, bounded timeouts,
truncated outputs). Auth via `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` or
`modal token new` profile; without credentials the UI reports verification
as unavailable instead of failing.

## Reproducible evidence

- Staged classic demo (optimal-transport project, audits, runs) ships in
  `web/public/data/` and renders with zero API keys.
- Local workspace: `cp .env.example .env`, fill keys, `./dev.sh`.
- Live demonstrations recorded in `HACKATHON_LOG.md` (M7 acceptance, M8
  discovery + import on a safe project copy; originals untouched).
- Tests: 295 backend (unittest) + 50 frontend (vitest), `tsc` clean,
  production build green.

## Limitations

- Single-user local system: no auth, no hosted storage, no job isolation
  — do not expose the backend publicly.
- Abstract-grounded recommendations are not full-paper analysis; some OA
  PDFs are unreachable programmatically (explicit manual-upload fallback).
- Verification covers feasible CPU claims only; nothing here reproduces a
  paper end to end.
- Third-party PDFs are never redistributed: `samples/` and
  `prototype/projects/` stay local-only via `.gitignore`.

## Links

- Repository: https://github.com/wemwem05/researchos
- Demo video: TODO — add link before submission
- Live demo: local only (see README Quickstart); no public deployment
