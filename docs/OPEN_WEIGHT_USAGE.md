# Open-Weight Model Usage (Milestones 5–6.1, 8)

## Identity

Model: `meta-models/muse-spark-open-1026`

Provider: Baseten

Base API: `https://inference.baseten.co/v1`

Client: `prototype/spark_open.py` (OpenAI-compatible Python client, key only
via `BASETEN_API_KEY`).

The exact model ID matters: the hackathon Baseten key is scoped specifically
to `meta-models/muse-spark-open-1026`, not to `meta-models/muse-spark-open`.

## Why Spark Open is used

Research is not a linear LLM chain. Milestone 5 replaces the single-shot
add-paper call with a persistent agent graph in which the open-weight model
makes consequential research decisions from grounded project state:

- **Investigator** — decides what a new paper contributes to the project:
  up to 5 grounded findings, cross-paper relationships, methodological
  differences, contradictions, open questions, corpus-scoped novelty risks,
  and what the paper changes in the project's understanding.
- **Scientific Critic** — skeptically re-inspects that proposal with a
  distinct role: evidence sufficiency, dataset/split/metric comparability,
  assumption mismatches, causal overreach, novelty overclaiming, ignored
  limitations, backbone confounds. Returns SUPPORTED, REVISION_REQUIRED, or
  AMBIGUOUS_REQUIRES_HUMAN.
- **Revision** — one bounded correction pass when the critic finds something
  fixable from the available evidence.

The model does not summarize after deterministic code decided: the findings,
connections, verdict, and fixes ARE the decisions, and deterministic gates
only check their grounding (known ids, valid types, bounds, corpus-scoped
wording). Invalid output can never mutate project state.

Milestone 6 adds a second workflow on the same model — computational
verification (`prototype/verify_graph.py`). Spark Open acts as:

- **Testability classifier** — is this claim's computational aspect
  benchmarkable on synthetic data, or does it need unavailable real data?
- **Experiment planner** — hypothesis, variables, algorithms, fixed seeds,
  reps + warm-up, documented Sinkhorn parameters, pre-registered success
  criteria, budget, limitations, and explicit scope notes.
- **Experiment coder** — the full benchmark file under a strict,
  deterministically checked contract (import allowlist, CSV schema,
  sanity flags, artifacts).
- **Failure diagnostician** — bounded repair (max 3) from preflight errors
  or sandbox stdout/stderr.
- **Interpreter** — observed-vs-interpreted report with one of five honest
  statuses. A deterministic clamp re-checks the verdict: successful
  execution alone can never yield "supported".

Milestone 6.1 adds a third workflow on the same model — scientific
evidence audit (`prototype/audit_graph.py`). Spark Open acts as:

- **Claim classifier** — multi-label claim types that select
  claim-specific checks.
- **Audit planner** — proposes structured numerical checks with named
  values, expected results, tolerances, and evidence refs. It proposes
  only: deterministic Python executes every computation.
- **Methodologist** — assumptions, controls, comparability, confounders,
  uncertainty, each graded observed / potential / missing.
- **Cross-paper comparator** — project findings compared with explicit
  comparability grades, validated against known ids.
- **Audit critic** — skeptical re-inspection of the whole audit, with
  the same SUPPORTED / REVISION_REQUIRED / AMBIGUOUS_REQUIRES_HUMAN
  verdicts and one bounded revision.

PDF perception still uses hosted Muse (below); all agentic reasoning in
Milestones 5–6.1 uses Spark Open.

Milestone 7 changes no model assignments: uploads analyze via the Meta Model
API, while every agentic decision (per-paper investigation/criticism during
ingest and question-rebuilds, audits, verifications) runs on the same
`meta-models/muse-spark-open-1026` through the same client, now invoked in a
project-scoped way with per-project telemetry. The M7 acceptance project
recorded 17 Spark calls (~132k tokens), all on the exact hackathon model id —
see `prototype/projects/transport-geometry-investigation/agent/telemetry.jsonl`
(local, git-ignored).

## Milestone 8 — literature discovery

The same client and exact model ID power the discovery graph
(`prototype/discovery_graph.py`): Spark Open plans the search strategy,
evaluates each shortlisted paper's relevance against live project state
(verdict + five grounded reasons + novelty-overlap flags), decides
whether a second bounded round is worthwhile, and critiques the final
recommendations (with at most one bounded revision). Deterministic work
— ID normalization, dedup, shortlisting, ranking, budget enforcement —
stays in Python. A typical run makes 4–6 Spark calls inside an 8-call
budget; usage (calls, tokens, latency) is persisted per run and visible
in the search-provenance UI.

## What still uses hosted Muse, and why

PDF perception is unchanged and still uses `muse-spark-1.3` via the Meta
Model API (`MODEL_API_KEY`): full-paper inventory + explanation
(`analyze_paper.py`) and figure/equation visual localization
(`localize_objects.py`). Those paths were working, verified milestones; the
agentic reasoning layer is what moved to the open-weight model.

## How judges can verify the open-weight model powers the workflow

1. **Read the client**: `prototype/spark_open.py` hardcodes the base URL and
   exact model ID, reads only `BASETEN_API_KEY`, and raises instead of
   falling back to any other model.
2. **Reproduce a test call**:
   `python prototype/agent.py verify`
   (one real call; prints provider/model/latency, never the key).
3. **Inspect run telemetry**: every Spark Open call is recorded with
   provider `Baseten`, model `meta-models/muse-spark-open-1026`, node name,
   latency, success/failure, retries, token usage, and request id. Live:
   `<project>/agent/telemetry.jsonl`. Staged demo:
   `web/public/data/agent/telemetry.json` (+ `summary.json`).
4. **Replay the demo investigation**: the staged run record
   `web/public/data/agent/runs/ot-agent-demo--eot-maps--live1.json` shows
   the investigator → critic → clean-accept path with real latencies and
   token counts; the checkpoint behind it lives in the (git-ignored) demo
   project dir.
5. **Exercise HITL live**: `synthetic_demo.py` builds a labeled synthetic
   interrupt; `agent.py review` or the Project view resumes it. The
   synthetic fixture is canned (provider `synthetic-demo`) and never mixes
   with real project output.
6. **Inspect a real verification**: `web/public/data/agent/verifications.json`
   plus per-thread `evidence.json`, `experiment.py`, `results.csv`,
   `scaling.png`, and logs show a Spark-planned, Modal-executed benchmark
   with measured numbers and an honest (non-"supported") verdict.
7. **Inspect a real audit**: `web/public/data/agent/audits.json` plus
   per-thread `audit.json` show Spark-classified, Spark-planned,
   Python-recomputed audits with methodology/cross-paper/external
   sections and evidence-scoped verdicts. Audit vs computational
   statuses are deliberately different vocabularies.
8. **Run discovery live**: from any project, Discover papers runs the
   full agent loop against OpenAlex; the run's queries, strategy notes,
   reflection, critic verdict, and token usage are inspectable in the
   project UI and in `project.json` (`discoveries`) plus
   `agent/discovery/<run>.json`.

## Where telemetry is stored

- `<project>/agent/telemetry.jsonl` — one JSON object per Spark Open call.
- `<project>/agent/runs/<thread>.json` — per-run audit trail mirroring its
  calls (transitions, decisions, validation failures, revision path, human
  request/response, final status).
- `<project>/agent/checkpoints.sqlite` — LangGraph SQLite checkpoints
  (durable interrupt/resume across process restarts).
- `<project>/agent/verifications/<thread>/` — per-attempt code, CSV, plot,
  stdout/stderr, env record, and `evidence.json` for each verification.
- `<project>/agent/audits/<thread>/audit.json` — the attached audit
  report; `<project>/agent/openalex_cache/` — cached external records
  (searches never repeat after resume).
- `<project>/agent/discovery/<run>.json` — full discovery retrieval log
  (queries, slim records, duplicates log, usage) behind each persisted
  `discoveries[]` run in `project.json`.

API keys are never written to any of these files (covered by test).
Modal credentials stay on the host (env or `~/.modal.toml`) and are never
passed into sandboxes; sandboxes run network-blocked with pinned packages.
