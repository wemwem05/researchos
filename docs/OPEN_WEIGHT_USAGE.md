# Open-Weight Model Usage (Milestone 5)

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

## Where telemetry is stored

- `<project>/agent/telemetry.jsonl` — one JSON object per Spark Open call.
- `<project>/agent/runs/<thread>.json` — per-run audit trail mirroring its
  calls (transitions, decisions, validation failures, revision path, human
  request/response, final status).
- `<project>/agent/checkpoints.sqlite` — LangGraph SQLite checkpoints
  (durable interrupt/resume across process restarts).

API keys are never written to any of these files (covered by test).
