# ResearchOS

ResearchOS is a research-question-aware workspace for understanding and
investigating scientific literature.

## Core idea

Researchers usually do not read papers without a purpose.

They are trying to answer questions such as:

"Can alternative optimal transport costs improve non-rigid shape correspondence?"

ResearchOS therefore interprets every paper through the context of the
researcher's question.

## Product flow

Research Question
    ↓
Relevant Papers
    ↓
Interactive Scientific Reader
    ↓
Equations / Figures / Tables / Claims
    ↓
Research-aware explanations
    ↓
Cross-paper evidence and connections
    ↓
Potential contradictions / gaps / novelty risks
    ↓
Computational verification when possible

## Interactive Reader

ResearchOS detects major scientific objects in papers:

- equations
- figures
- architecture diagrams
- tables
- major claims

Every explainable object provides:

- what it is
- why it exists in the paper
- what claim it supports
- what it does NOT establish
- how it relates to the user's research question
- evidence from the paper

## Research Lens

Scientific objects are separated from their relevance.

An object may be important to the paper without being important to the user's
research question.

ResearchOS therefore tracks:

- paper importance
- research relevance

Research relevance levels:

HIGH
RELATED
CONTEXT

The reader supports:

FOCUS MODE
Prioritizes objects important to the research question.

SHOW ALL MODE
Makes all explainable scientific objects visible.

## Long-term differentiation

ResearchOS should not behave like independent "chat with PDF" sessions.

A Research Project persists across papers.

When the researcher opens another paper, ResearchOS should understand:

- what is already known
- what evidence supports it
- which papers disagree
- which assumptions differ
- whether a proposed research direction has already been explored
- what remains uncertain

Eventually, computationally testable claims can be executed and independently
checked using an agent + sandbox.