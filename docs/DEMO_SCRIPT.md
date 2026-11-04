# ResearchOS — 3-Minute Demo Script

Setup: `./dev.sh`, open `http://localhost:3000`. Use the `ot-correspondence`
project (2 papers, 8 findings, real audits, real Modal reports, real agent
runs — all persisted, no live inference needed except where noted).

## 0:00–0:20 — Problem

"Researchers juggle search engines, PDFs, notes, and chatbots — and none of
them remember what the evidence supports. Chatbots summarize plausibly but
ground nothing, persist nothing, and verify nothing. ResearchOS is a
workspace where the research question stays central and every claim stays
linked to its evidence."

## 0:20–0:45 — Research project

Open the optimal-transport project. Point at the Overview tab: the research
question up top, papers analyzed, key findings with paper references,
cross-paper connections, open questions, corpus-scoped novelty risks, and a
recommended next step. "Everything here traces to a paper, a page, and an
object — and when the question changes, old conclusions are marked
historical, never silently current."

## 0:45–1:15 — Interactive PDF

Open EOT in the reader. Toggle Focus / Show All; click an equation —
highlight on the page, Research Lens explains what it means *for this
question*. Select a claim: show its grounding and the two examination
paths — evidence audit vs. computational test — with their different
scopes stated plainly.

## 1:15–1:45 — Literature discovery + cross-paper reasoning

Discover tab: "The agent searched OpenAlex itself — here are its queries,
its stop-or-continue reflection, and why each paper matters, with the exact
evidence available." Expand a recommendation: verdict, novelty note,
abstract-only caveat. Approve one and show it entering the standard
pipeline; back on Overview, new findings and connections cite the imported
paper. (Recorded live: 6 agent-chosen queries, 50 records, 5 grounded
recommendations, +5 findings on import.)

## 1:45–2:15 — Evidence audit

Evidence tab: open a real persisted audit — recomputed numbers, methodology
assessment, cross-paper comparison, unresolved concerns. "An audit checks
reasoning and arithmetic from the paper. It runs no code — which is why a
separate section, with a separate vocabulary, holds the executed
experiments."

## 2:15–2:40 — Modal experiment + human-review agent

Open a real Modal verification report: plan, executed code, measured
numbers, artifacts, scoped conclusion. Then Activity: show a persisted
investigator/critic run, its timeline, and a human-review request — the
ambiguity, both positions, the options. "The agent pauses instead of
guessing, and resumes from the researcher's judgment without recomputing."

## 2:40–3:00 — Architecture and impact

"Underneath: LangGraph agents on SQLite checkpoints, Spark Open for every
research decision, deterministic validation in Python, everything persisted
and inspectable. ResearchOS doesn't summarize papers — it builds a durable,
auditable memory of what your evidence supports."

Close on the Projects page: "One question, one workspace, honest evidence."
