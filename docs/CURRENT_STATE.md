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

## Milestone 6 — Autonomous Computational Verification

Working.

A researcher-selected claim flows through a bounded LangGraph subgraph
(`prototype/verify_graph.py`) reusing the M5 architecture (same checkpointer
file, telemetry log, run records, review UI, server):

select_claim
→ classify_testability (Spark Open)
→ experiment_planner (Spark Open: hypothesis, variables, algorithms, seeds,
  reps + warm-up, documented Sinkhorn params, pre-registered criteria,
  budget, limitations, scope notes)
→ validate_plan (deterministic caps; over-budget escalates)
→ generate_code (Spark Open under a strict checked contract)
→ code_preflight (AST import allowlist + banned patterns; syntax slips
  auto-repair, security failures escalate — sandbox isolation is the real
  boundary, never host execution)
→ modal_execute (Modal Sandbox: CPU-only, 2 CPU / 4 GiB, 10-min exec cap,
  network blocked, pinned numpy/matplotlib image, artifacts pulled before
  terminate)
→ validate_result (returncode, CSV schema/rows/labels, finite values,
  artifacts; failed sanity flags route to interpretation as FINDINGS, not
  repair fodder)
→ interpret_result (Spark Open) | diagnose_and_repair (Spark Open, max 3)
→ evidence_report (deterministic honesty clamp) → attach_to_project
  (atomic, idempotent)

Verdicts (no universal "reproduced" badge): supported / not supported in
tested conditions, inconclusive, execution failed, not computationally
testable. Execution success alone can never yield "supported".

Live demo (EOT claim-3, efficiency-motivation proxy on synthetic point
sets, researcher-scoped via two human reviews): SW median 2.7–5.1 ms vs
Sinkhorn 0.33–28.0 s across n=256–2048 (5 reps + warm-up, tight IQRs);
~5500x gap at n=2048. Pre-registered sanity criterion failed (symmetry
tolerances), so the system reported NOT SUPPORTED with the numbers
preserved under "partially tested" — the clamp working as designed.
Three attached evidence records demonstrate not_testable (v1),
execution_failed (v2, repair budget exhausted live), and not_supported
(v3). A fourth run's mislabeled evidence was withdrawn with an audit note
after a status-label bugfix.

UI: "Verify computationally" action on claims in the Reader (confirm →
start via agent server → live Agent Run timeline + review + evidence),
Verification section in the Project view (status, observed vs
interpreted, criteria, results table, plot, artifacts, limitations), live
artifact downloads from the server (strict filename allowlist).

Backend tests:
142 passing (30 new: Modal mock, bounded repair, timeout/infra handling,
artifact preservation, escalation, restart resume with zero repeated
model calls, no-false-verified clamp, idempotent attach)

Frontend tests:
28 passing (5 new)

TypeScript:
clean

Production build:
passing (webpack builder — sandbox blocks Turbopack, as before)

## Milestone 6.1 — Scientific Evidence Audit

Working.

The default "verify" action is now an EVIDENCE AUDIT: a
researcher-selected claim is checked against its own paper record
(recomputed numbers, methodology, project findings, optional bounded
literature) without claiming reproduction. Computational testing
(Milestone 6) stays a separate optional action and is never
auto-substituted.

New workflow (`prototype/audit_graph.py`, same LangGraph/SQLite/
telemetry/review infrastructure as M5–M6, same Spark Open model):

load_claim_and_project
→ classify_claim (Spark Open, multi-label claim types)
→ collect_internal_evidence (deterministic: claim + objects + page
  excerpts; gaps reported, never invented)
→ plan_audit_checks (Spark Open proposes structured checks)
→ execute_numerical_checks (deterministic Python owns ALL arithmetic:
  percentage improvement, error reduction, best method, ranking,
  aggregate, ratio, difference, unit checks; value traceability to
  cited table/cell context; paper-precision rounding tolerance;
  untraceable checks are NOT EVALUATED)
→ methodology_assessment (Spark Open: observed vs potential vs missing)
→ compare_project_evidence (Spark Open + id validation: directly /
  partially / not directly comparable / uncertain)
→ external_evidence_gate (deterministic: skipped unless the researcher
  enabled it AND the planner justifies it)
→ [bounded_literature_search (OpenAlex: ≤2 queries, ≤10 records,
  ≤3 papers, disk-cached, fail-soft) → assess_sources
  (abstract-level labels, no full-text pretense)]
→ scientific_critic (Spark Open) → decision_gate
→ finalize | bounded_revision (max 1) | human_review (interrupt)
→ evidence_report (deterministic overall mapping) → attach_to_project
  (atomic, idempotent, separately attributed — findings never rewritten)

Overall assessments (evidence-scoped, disjoint from M6 statuses):
substantiated / partially substantiated / discrepancies identified /
inconclusive on audited evidence / insufficient evidence.

Live demo (EOT + Deep Shells project, 34 real Spark Open calls on
`meta-models/muse-spark-open-1026`, ~229k tokens): claim-1 SOTA audit
with live OpenAlex search (2 queries, 6 records, real 2026 source);
claim-3 math/efficiency audits corpus-only. 6 audits attached (a1–a6);
all current overalls are "discrepancies identified" driven by
established methodology observations (e.g. single-protocol ablation,
necessity language outrunning Table 4), with numerical checks passing
after the rounding fix. Two live fail-closed human reviews (critic
schema slips) resumed without repeating any Spark call — once via CLI,
once via the UI review endpoint after an HTTP-started run; the final
run took the clean SUPPORTED → finalize path after the critic prompt
fix.

UI: primary "Audit scientific claim" action on Reader claims (confirm
dialog shows scope + external toggle default OFF with the 2/10/3
budget → live audit timeline → review → full report: checks table,
methodology, project comparison, external sources, unresolved
questions, limitations, telemetry); "Verify computationally" kept as
the distinct optional second action. Project view gained an Evidence
audits section (live + staged).

Backend tests:
173 passing (31 new audit: classification, numerical ops incl.
rounding/paper-precision, traceability, methodology/cross-paper
schemas, external budget/rate-limit/abstract-labeling, interrupt/
resume with zero repeated calls, idempotent attach, audit-vs-M6
status disjointness, M1–M6 data preservation)

Frontend tests:
34 passing (6 new: assessment/check labels, comparability labels,
external budget pins, audit timeline incl. waiting-review)

TypeScript:
clean

Production build:
passing (webpack builder)

## Next milestone

TBD.
