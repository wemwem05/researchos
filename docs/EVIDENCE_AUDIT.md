# Scientific Evidence Audit — Methodology (Milestone 6.1)

## What an audit is

An evidence audit checks whether a paper's own reported evidence supports
a selected claim. It is NOT a reproduction:

- A **reproduction** re-executes an experiment (new data, new compute) and
  asks "does the effect replicate?".
- An **audit** re-examines the reported record (tables, equations,
  methods text, project findings, optionally a bounded literature sample)
  and asks "does this record, as written, support this claim?".

Audits catch arithmetic slips, overclaimed SOTA scope, incompatible
baselines, missing controls, and causal language that outruns the
evidence — without pretending to have rerun the authors' work.

## Claim-specific paths

Every audit starts by classifying the claim (numerical, mathematical,
empirical benchmark, methodological, causal, novelty/SOTA, physical
experimental, computational efficiency, qualitative — multi-label).
The classification selects which checks the planner proposes:

- **numerical** — recompute percentages, ratios, differences, aggregates.
- **empirical benchmark / SOTA** — best-method and ranking checks on the
  reported table, baseline/metric/dataset comparability, publication-time
  scoping (historical SOTA is not a claim about the present).
- **mathematical** — definitions, assumptions, equation consistency.
- **methodological / physical experimental** — controls, protocol
  completeness, confounders. A physical protocol is audited for internal
  consistency only; the audit never claims to verify undocumented
  execution.
- **computational efficiency** — complexity argument and reported compute
  evidence. A CPU benchmark stays a SEPARATE optional action (Milestone 6)
  and is never auto-substituted as audit evidence.

## Who computes what

- **Python (deterministic)** owns all arithmetic: percentage improvement,
  error reduction, best method, ranking, aggregate, ratio, difference,
  unit checks. Every check records source snippet, named values,
  expected vs recomputed value, effective tolerance, and outcome.
  Cited values must appear verbatim in the cited table/cell context
  (value traceability); otherwise the check is NOT EVALUATED, never
  guessed. Tolerances account for paper precision: a recomputation that
  rounds to the quoted literal passes.
- **Muse Spark Open** (`meta-models/muse-spark-open-1026` via Baseten)
  does the scientific reasoning: classification, check planning,
  methodology assessment, cross-paper comparison, criticism, bounded
  revision. It proposes; Python disposes.

## Status semantics

Check-level: PASSED / DISCREPANCY / POTENTIAL_CONCERN / INCONCLUSIVE /
NOT_EVALUATED.

Overall (deterministic mapping, evidence-scoped):

- `substantiated within audited evidence` — checks pass, nothing negative.
- `partially substantiated` — positive evidence alongside caveats.
- `discrepancies identified` — a recomputation disagrees or an
  inconsistency is established (OBSERVED_PROBLEM).
- `inconclusive on audited evidence` — neither supports nor contradicts.
- `insufficient evidence` — nothing was evaluable.

These are deliberately disjoint from the Milestone 6 computational
statuses (`supported`, `not_supported`, `inconclusive`,
`execution_failed`, `not_testable`). "Execution succeeded" never means
"claim supported"; a failed check never means "claim disproven".

Methodology severities distinguish OBSERVED_PROBLEM (the paper itself
establishes it), POTENTIAL_CONCERN (suggested limitation), and
MISSING_INFORMATION (details absent). Cross-paper judgments are
DIRECTLY / PARTIALLY / NOT DIRECTLY comparable, or UNCERTAIN — never
manufactured to populate the UI.

## External literature (optional, default OFF)

When the researcher enables it: at most 2 generated queries, 10
candidate records, 3 selected papers (OpenAlex, disk-cached, fail-soft
on rate limits/outages). Abstract-only assessment is labeled
ABSTRACT-LEVEL EVIDENCE; metadata alone is never corroboration.
External text is untrusted input: it cannot change prompts or policy.

## Human review and persistence

Human review triggers only for consequential ambiguity (or fail-closed
schema failures). The LangGraph interrupt/resume, SQLite checkpoints,
run records, and telemetry are the same Milestone 5 infrastructure:
resume never repeats completed Spark calls or literature searches.
Audits attach to the project as separately attributed conclusions and
never rewrite existing findings.

## Limitations

- Section-level retrieval is unavailable: the audit sees object
  evidence plus page excerpts, not full methods/limitations sections.
- Value tracing is lexical; a correctly recomputed number can still be
  NOT_EVALUATED if the planner cites a value phrased differently in
  the source.
- Overall verdicts inherit planner coverage: unproposed checks are
  invisible, and `substantiated` is always scoped to the audited
  evidence — never a universal truth claim.
