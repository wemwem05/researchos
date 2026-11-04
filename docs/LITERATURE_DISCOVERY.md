# Milestone 8 — Autonomous Literature Discovery

A researcher with a research question — and zero or more papers — clicks
**Discover papers**. A bounded LangGraph agent (Spark Open, exact model
`meta-models/muse-spark-open-1026`, no fallback) plans a search strategy,
queries OpenAlex, deduplicates, evaluates relevance against the live
project, reflects on whether a second round is worthwhile, passes a critic
gate, and persists grounded recommendations. The researcher approves what
to read; approved papers are imported through the existing M7 ingest
pipeline (download → validate → analyze → relevance → localize →
investigate → persist) and project findings update. Nothing is ingested
before approval.

## Graph topology

`prototype/discovery_graph.py`, durable on the shared SQLite checkpointer
(same file as M5/M6/M6.1 runs, distinct `discover-*` thread ids):

```
load_context
→ plan_search            (Spark: ≤3 round-1 queries + strategy notes)
→ retrieve_candidates    (OpenAlex; per-query fail-soft)
→ normalize_and_deduplicate  (deterministic; project/prior/run dedup)
→ shortlist              (deterministic round-robin ≤8, abstracts first)
→ evaluate_relevance     (Spark: verdict + 5 grounded reasons per paper)
→ reflect                (Spark: stop, or ONE focused round-2)
→ [round 2 → retrieve_candidates]
→ discovery_critic       (Spark: SUPPORTED / NEEDS_REVISION)
→ discovery_decision_gate
→ [NEEDS_REVISION → bounded_revision (once) → discovery_critic]
→ prepare_recommendations (deterministic rank + novelty clamp, ≤5)
→ persist_recommendations (atomic project.json upsert + retrieval log)
→ END
```

No `interrupt()`: researcher approval happens after completion through
plain API calls (`decide`, `import`), so the graph never blocks. Every
node records transitions on the `RunRecord` (kind `discover`); Spark and
provider usage accumulates in `run.usage`.

## How Spark Open decides

Spark makes every judgment call; Python does everything deterministic:

| Spark (judgment) | Python (deterministic) |
|---|---|
| search strategy + queries | query hygiene (length, dup, cap) |
| relevance verdicts + reasons | ID normalization, dedup, shortlist |
| novelty-overlap flags | novelty-overclaim clamp |
| stop / continue reflection | round + budget enforcement |
| critic verdict + fixes | known-id gates, revision bound |
| revised evaluations | ranking, persistence |

The planner sees a compact context (question, focus, paper titles,
finding statements, methods, open questions, novelty risks, prior
discovery decisions) — never full PDFs. Strategy codes (A–F: direct,
method, related-work, alternative, novelty-overlap, recent) steer
coverage; bare single-concept queries are rejected. The evaluator must
ground every reason in the shown abstract/metadata and is forbidden from
claiming full-text knowledge, experimental results, or global novelty.
The critic re-checks grounding, metadata consistency, comparison with
existing papers, and fabricated ids.

## Budgets and stopping

Defaults per run (researcher may lower, never exceed hard caps):

2 rounds · ≤3 queries/round · ≤50 records · ≤8 shortlisted ·
≤5 recommended · ≤2 expansions · ≤3 deep inspections · ≤8 Spark calls.

The agent stops early when coverage is adequate; round 2 must respond to
round-1 verdicts (its queries/expansions are validated against round-1
output). Expansions reference concrete retrieved OpenAlex work ids —
candidate labels are dropped with a logged note. Provider calls are
throttled (≥0.5 s apart), retried with bounded backoff (honoring
`Retry-After`), and cached on disk per project.

## Sources and grounding

Primary and only provider: OpenAlex (`prototype/openalex.py`, extended —
keyless or `OPENALEX_API_KEY`, `mailto` UA for the polite pool).
Semantic Scholar was evaluated and rejected: negligible gain for real
failure-mode cost. No generic web scraping.

Every candidate carries an evidence level:

- `metadata_only` — title/authors/venue only, explicit caveat
- `title_and_abstract` — abstract-grounded relevance, never full-paper claims
- `full_text_available` — lawful OA PDF advertised (not yet read)
- `full_text_analyzed` — ingested through M7 (only set post-ingest)

`pdf_url` comes exclusively from OpenAlex `pdf_url` fields (primary /
best-OA location) — landing-page URLs are never mislabeled as PDFs.

## Novelty limits

Discovery can flag *possible* prior work / overlap / alternative
approaches. It cannot prove global novelty: absence from a ≤50-record
retrieval is absence of evidence, not evidence of absence. Overclaim
phrases ("no one has studied…", "entirely novel", "first ever", …) are
rewritten deterministically to *"This search found no direct match within
the retrieved literature, but coverage is limited."* — defense in depth
behind the prompt-level ban — and shared terminology (e.g. two papers
mentioning "optimal transport") is never treated as equivalence.

## Approval and import

Candidates start `pending`. `POST …/decide` approves/dismisses
individually or in bulk (per-item results; dismissed candidates stay
recorded and can never be imported without re-approval). `POST …/import`
creates one `import_discovery` job for ≤8 candidates with per-item
results — never a mid-list abort.

Per candidate: duplicate checks (title + content hash) → resolve OA PDF
→ SSRF-guarded download → stage → existing `run_ingest` → findings and
connections update → candidate `imported` with paper link and
`full_text_analyzed`. If the investigator requests human review, the
candidate waits as `importing` and finalizes on the next import call
once the paper attaches (self-healing). Unavailable full text yields an
explicit `full_text_unavailable` state with Open-source / manual-upload
options; the metadata-only recommendation stays in history.

## Full-text acquisition rules

`prototype/acquisition.py`, fail closed:

- http/https only, no credentials, default ports only
- every redirect hop re-validated (≤5, no HTML/JS following)
- direct connections: every resolved A/AAAA must be globally routable
  (private/loopback/link-local/multicast/reserved rejected); IP literals
  are rejected without DNS, proxy or not
- proxied egress: local DNS validation is skipped (the proxy resolves),
  all other guards still apply
- size capped before and during streaming (same `RESEARCHOS_MAX_PDF_MB`
  as uploads), PDF magic required, PDF content-type required
- no cookies/auth/paywall bypass; license metadata preserved; downloads
  are never auto-committed to git

Known friction: some publishers (observed: MDPI) serve bot filters or
HTML interstitials to programmatic clients instead of the advertised
PDF — these fail explicitly as non-PDF and fall back to manual upload.

## Question versions

Runs stamp the originating `research_question_version`. A mid-search
question change marks the run `stale` with an explicit note; older runs
are shown with stale badges after any question change and never reused
as current. Provenance (queries, verdicts, PDFs) is preserved; unchanged
documents are never re-downloaded on question change.

## API

```
POST /api/projects/<id>/discoveries/start   {focus?, budget?, request_key?} → 202 {job}
GET  /api/projects/<id>/discoveries          → {discoveries, active_job, question_version}
GET  /api/projects/<id>/discoveries/<run>    → {run, retrieval_log, record}
POST /api/projects/<id>/discoveries/<run>/decide  {decisions:[{candidate_id, action}]} → {items}
POST /api/projects/<id>/discoveries/<run>/import  {candidate_ids:[...]} → 202 {job}
```

Import reuses `run_ingest` with the same `PipelineDeps` injection point
as uploads, so staging, validation, caching, and review flows are
identical. `SERVER_VERSION` 8.0.

## UI

`Discover papers` section on the project page: focus input, explicit
budget line, live progress, run selector with stale badges, filters
(All / Highly relevant / Possible novelty overlap / Open access /
Already in project), expandable candidate cards (verdict, evidence,
novelty, five grounded reasons, abstract, identifiers, query
provenance), per-paper Approve / Dismiss / Open source / Import, bulk
selection, import results, and a search-provenance disclosure (strategy,
queries, reflection, critic, usage). Imported papers link straight into
the existing Reader; no redesign, no chatbot, no graph animation.

## Telemetry and provenance

Persisted per run: run/project ids, question + version, focus,
timestamps, strategy notes, per-query records (strategy, rationale,
counts, errors), shortlist decision, reflection + critic outputs,
revision flag, limitations, per-candidate evaluations with verdicts,
usage (Spark calls, provider calls, records, tokens), decisions, and
import states. `agent/discovery/<run_id>.json` holds the full retrieval
log (capped slim records + duplicates log); no API keys are ever stored.

## Limitations

- OpenAlex metadata quality bounds everything: weak abstracts, missing
  DOIs, and noisy keyword search require the evaluate/critic gates.
- Abstract-grounded relevance is not full-paper analysis; confident
  claim-level assessment needs import.
- Some OA PDFs are unreachable programmatically (bot filters,
  interstitials, >32 MB defaults) — explicit fallback, not silent skip.
- Single-user local tool: no auth, no hosted storage, no job isolation
  (same production caveats as M7).
- DNS rebinding (resolve→connect TOCTOU) is documented but unmitigated
  beyond the per-hop checks; public deployment needs a pinning proxy.
```

## Tests (see HACKATHON_LOG for counts)

`prototype/test_discovery.py`: normalization, stable-id + arXiv-version +
exact-title dedup, evidence levels, novelty clamp, budget validation,
query hygiene, shortlist round-robin, prepare ranking, full graph runs
(stop/continue/revision/fail-closed/fail-soft/project-match), expansion
id grounding, persist scope, stale marking, decision locks, acquisition
SSRF/redirect/size/MIME/proxy guards, server start/decide/import/retry/
restart/self-heal. `web/app/lib/discovery.test.ts`: filters, sorting,
labels, source links, staleness.
