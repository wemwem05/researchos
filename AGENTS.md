# ResearchOS

ResearchOS is a solo Meta AI Hackathon project.

## Product

ResearchOS is a research-question-aware scientific research workspace.

Core flow:

Research question
→ scientific papers
→ interactive Research Lens
→ grounded findings
→ cross-paper reasoning
→ contradictions / gaps / novelty risks
→ agentic investigation
→ human review when necessary
→ computational verification when possible

ResearchOS is NOT a generic PDF chatbot.

## Current state

Milestones 1–4 are working.

### Milestone 1
PDF + research question → grounded scientific objects.

### Milestone 2
Scientific-object localization.

Tables:
- PyMuPDF native / hybrid localization.

Figures and equations:
- Muse vision grounding.

Successful localizations are cached.

### Milestone 3
Interactive PDF Research Lens.

### Milestone 3.1
Complete scientific-object inventory separated from research relevance.

Reader supports:
- Focus
- Show All

### Milestone 4
Persistent Research Project across multiple papers.

Each paper contributes grounded findings.

New papers are interpreted relative to existing project knowledge.

Cross-paper relationships include:
- supports
- contradicts
- extends
- overlaps_with
- uses_same_method
- uses_different_assumption
- leaves_open

Novelty statements are always corpus-scoped.

Read docs/CURRENT_STATE.md and HACKATHON_LOG.md for implementation details.

## Scientific rules

- Never invent evidence.
- Ground findings to paper/page/object references.
- Distinguish model interpretation from verification.
- Never claim global novelty from the current project corpus.
- Never call a result reproduced unless code/data was actually executed.
- Do not infer causality from benchmark improvement alone.
- Prefer explicit uncertainty or human review over unsupported certainty.
- Synthetic/demo evidence must never be presented as real scientific evidence.

## Engineering rules

- Preserve working milestones.
- Extend existing components instead of rewriting working systems unnecessarily.
- Keep API keys in environment variables only.
- Never commit .env or secrets.
- Run backend tests after backend changes.
- Run frontend tests, TypeScript checks, and production build after frontend changes.
- Do not declare a milestone complete while validations fail.
- Avoid unrelated refactors.

## Model strategy

Existing PDF analysis/localization may continue using Meta Model API where it
already works.

The core agentic research workflow must use Muse Spark Open through Baseten.

Hackathon Baseten configuration:

Base URL:
https://inference.baseten.co/v1

Exact model ID:
meta-models/muse-spark-open-1026

IMPORTANT:
Do NOT replace this with meta-models/muse-spark-open.
The hackathon Baseten key is scoped specifically to
meta-models/muse-spark-open-1026.

Muse Spark Open should make meaningful scientific decisions rather than being
used as a cosmetic integration.

## UX

ResearchOS should feel like a scientific workspace, not a chatbot.

Research relevance:
- indigo/purple = high
- blue = related
- gray = context

Scientific state:
- green = supported / verified
- amber = uncertain / conditional
- red = contradiction / failed verification / warning

Human-review states must be explicit and auditable.