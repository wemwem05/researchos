# ResearchOS — Current State

## Milestone 1 — Scientific Object Understanding

Working.

Input:
- scientific PDF
- research question

Output:
structured scientific objects including:
- equations
- figures
- tables
- major claims

Objects include grounded explanations, limitations, research relevance,
evidence, and type-specific metadata.

Meta Muse receives the PDF directly.

## Milestone 2 — Scientific Object Localization

Working.

Scientific objects are mapped back to their locations in the PDF.

Localization strategy:

Tables:
- PyMuPDF native table detection
- caption matching
- Muse crop fallback when needed

Figures / equations:
- Muse vision grounding

Successful localizations are cached.

Null localization cannot have high confidence.

## Milestone 3 — Interactive Research Reader

Working.

Next.js frontend shows:
- real PDF
- scientific-object overlays
- Research Lens panel
- object navigator

Clicking an object displays its scientific explanation.

## Milestone 3.1 — Research Lens

Working.

The system now separates:

1. document inventory
2. scientific explanation
3. research-question relevance

Current EOT benchmark paper:

25 objects total:
- 14 equations
- 4 figures
- 4 tables
- 3 claims

22 visual objects localized.

Research relevance:
- high
- related
- context

Reader supports:

FOCUS
SHOW ALL

Localization is cached.

Backend tests:
69 passing

Frontend tests:
8 passing

TypeScript:
clean

Production build:
passing

## Current benchmark research question

"Can alternative optimal transport costs improve non-rigid shape correspondence?"

## Known caveats

Muse vision localization can be nondeterministic.
Successful localization caching and retries mitigate this.

Scientific explanations should still be treated as model-generated
interpretations grounded in paper evidence, not unquestionable truth.

## Milestone 4 — Research Project + Cross-Paper Memory

Working.

One persistent research question spans manually added papers
(`prototype/project.py`: `init` / `add-paper` / `stage-web`, file-backed
`project.json`, no database). The per-paper pipeline is unchanged; each new
paper is additionally interpreted relative to existing project findings.

Each paper contributes:

- ≤5 grounded findings (paper evidence + analysis object ids)
- a contribution record: what it adds, stance toward prior findings,
  overlap with the research direction, new open questions raised

Cross-paper connections use exactly these types (only supported instances
are created; zero of any type is valid):

- supports
- contradicts
- extends
- overlaps_with
- uses_same_method
- uses_different_assumption
- leaves_open

Open questions and novelty risks carry paper/finding references. Novelty is
always corpus-scoped ("Within the papers currently analyzed…"); global
novelty is never claimed.

Demo project (`ot-correspondence`, Deep Shells added first, EOT second):

- 2 papers, 8 findings, 3 connections, 5 open questions, 4 novelty risks
- Deep Shells: 23 objects, 20/20 localized

Frontend:

- `/project` — question, papers, findings, connections, open questions,
  novelty risks
- `/read/[paperId]` — any staged paper in the existing Reader
- `/` — unchanged EOT reader
- findings deep-link into the reader via `?select=`

Paper PDFs are local-only fixtures (git-ignored, never committed).

Backend tests:
85 passing

Frontend tests:
14 passing

TypeScript:
clean

Production build:
passing

## Milestone 5 — Persistent Muse Spark Open Research Agent Graph

Working.

The add-paper step is now a LangGraph workflow powered by Muse Spark Open
(`meta-models/muse-spark-open-1026`) through Baseten:

load_project_context
→ investigator (Spark Open: findings, connections, methods, novelty, what changed)
→ deterministic validation (ids, types, bounds, corpus-scoped wording; fail closed)
→ critic (Spark Open: SUPPORTED / REVISION_REQUIRED / AMBIGUOUS_REQUIRES_HUMAN)
→ decision gate
→ finalize | one bounded revise_once | human_review via interrupt()
→ apply_final_decision
→ update_project_state (single mutation point, reuses Milestone 4 apply_delta)

Persistence:

- SQLite checkpoints per project (`agent/checkpoints.sqlite`)
- one thread per investigation run; survives process/server restart and
  delayed human review without re-running investigator/critic
- file-backed run records (`agent/runs/`) with transitions, decisions,
  validation failures, revision path, human request/response, final status
- per-call telemetry (`agent/telemetry.jsonl`): provider, exact model,
  node, latency, success, retries, tokens, request id — never secrets

Human review triggers only on consequential ambiguity (or critic disagreement
after the single revision). The interrupt node has no model side effects; a
deterministic prepare step checkpoints the request first.

Live demo (Deep Shells → EOT, real Spark Open calls): investigator proposed
5 grounded findings + 5 connections; critic verified with 5 passed
comparability checks and 3 minor concerns, verdict SUPPORTED; clean accept,
no human review, no fabricated contradiction. The interrupt/resume path is
additionally exercised by an explicitly labeled synthetic fixture
(`synthetic_demo.py`, canned responses, never merged into real projects).

Frontend `/project` gains: Agent Run timeline, Researcher Review card with
response options + free text (live resume via the stdlib agent server, staged
fallback otherwise), AI Research Agent model/provenance section, and a
labeled synthetic walkthrough. Reader routes unchanged.

PDF perception still uses hosted Muse; only investigator/critic/revision use
Spark Open. See `docs/OPEN_WEIGHT_USAGE.md`.

Backend tests:
112 passing (27 new: validation gates, revision bound, interrupt, restart
resume with zero model-call re-runs, telemetry, key hygiene, M4 compat)

Frontend tests:
23 passing (9 new)

TypeScript:
clean

Production build:
passing (webpack builder — sandbox blocks Turbopack, as before)

## Next milestone

TBD (Modal execution is explicitly out of scope for Milestone 5).
