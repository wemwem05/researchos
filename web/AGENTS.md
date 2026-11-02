# ResearchOS — Development Instructions

ResearchOS is a solo Meta AI Hackathon project.

## Goal

Build a research-question-aware scientific research workspace.

Researchers should be able to:
- understand scientific papers deeply
- interact with equations, figures, diagrams, and tables
- see how each element relates to their research question
- connect evidence across multiple papers
- identify possible gaps, contradictions, and already-explored directions
- later computationally verify claims when verification is actually possible

## Hackathon constraints

- All work must be clearly created during the hackathon.
- Preserve clear Git history.
- Repository will be public.
- Never commit API keys or secrets.
- `.env` must remain ignored.
- Do not use assets/data/code without appropriate rights.

## Engineering principles

- Preserve working milestones unless explicitly changing them.
- Prefer extending existing code over rewriting working components.
- Keep architecture practical for a solo hackathon.
- Avoid unrelated refactors.
- Do not add features outside the current milestone.
- Ground scientific explanations in the source paper.
- Never invent evidence, results, page numbers, or experimental verification.
- A claim is only "reproduced" if code/data was actually executed.
- Distinguish interpretation from verification.

## Current stack

Backend:
- Python
- Pydantic
- Meta Model API / Muse
- PyMuPDF

Frontend:
- Next.js
- TypeScript
- Tailwind
- PDF.js / react-pdf

## Validation

After backend changes:
- run relevant Python tests

After frontend changes:
- run tests
- run TypeScript checks
- run production build

Do not declare a milestone complete with failing tests/builds.

## Product UX

ResearchOS is NOT a generic PDF chatbot.

The right-side panel is a scientific Research Lens / inspector.

Research relevance colors:
- indigo/purple = high relevance
- blue = related
- gray = context

Reserve:
- green = verified / supported
- amber = conditional / uncertain
- red = contradiction / failed verification / warning