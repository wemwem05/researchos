#!/usr/bin/env python3
"""ResearchOS Milestone 4 prototype: Research Project + Cross-Paper Memory.

A project persists ONE research question across manually added papers. Each
paper is first analyzed independently with the frozen Milestone 1/3.1 pipeline
(analyze_paper.py, unchanged); then a single text-only Muse call interprets
the new paper RELATIVE to the project's existing findings:

- extracts a small set of grounded project findings (<=5),
- proposes cross-paper connections (only supported ones; possibly none),
- answers what the paper adds / supports-or-challenges / overlaps /
  which open questions it raises,
- maintains grounded, corpus-scoped open questions and novelty risks.

Usage:
    python prototype/project.py init \
        --question "Can alternative OT costs improve non-rigid shape correspondence?" \
        --project-dir prototype/projects/ot-correspondence

    python prototype/project.py add-paper \
        --project-dir prototype/projects/ot-correspondence \
        --paper-id deep-shells \
        --pdf samples/deep_shells.pdf \
        --analysis prototype/deep_shells_output.json \
        --localized prototype/deep_shells_localized.json

    python prototype/project.py stage-web \
        --project-dir prototype/projects/ot-correspondence \
        --web-dir web

State is file-backed JSON (project.json). No database.

Auth: API key ONLY via the MODEL_API_KEY environment variable (or a local,
never-committed .env file containing it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from analyze_paper import (
    API_BASE_URL,
    API_KEY_ENV_VAR,
    DEFAULT_MODEL,
    AnalysisResult,
    extract_json,
)
from discovery_models import DiscoveryRun

SCHEMA_VERSION = "m4-1"
MAX_FINDINGS_PER_PAPER = 5

ConnectionType = Literal[
    "supports",
    "contradicts",
    "extends",
    "overlaps_with",
    "uses_same_method",
    "uses_different_assumption",
    "leaves_open",
]

CONNECTION_TYPES: tuple[str, ...] = (
    "supports",
    "contradicts",
    "extends",
    "overlaps_with",
    "uses_same_method",
    "uses_different_assumption",
    "leaves_open",
)


# ---------------------------------------------------------------------------
# Project state models
# ---------------------------------------------------------------------------


class PaperEntry(BaseModel):
    paper_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    filename: str  # local PDF filename (never redistributed by this tool)
    added_order: int = Field(ge=1)
    n_objects: int = Field(ge=0)
    n_localized: int = Field(ge=0)
    pdf_path: Optional[str] = None  # local repo-relative path, for stage-web
    localized_path: Optional[str] = None
    web_pdf: Optional[str] = None  # public URL path once staged, e.g. /papers/x.pdf
    web_data: Optional[str] = None
    # Milestone 7: content hash (dedupe) + question version the stored
    # relevance scores were computed against (staleness detection).
    sha256: str = ""
    question_version: int = Field(default=1, ge=1)


class Finding(BaseModel):
    id: str
    paper_id: str
    statement: str = Field(min_length=1)
    evidence: str = Field(min_length=1)  # grounded ref, e.g. "Table 2, p.7"
    page: Optional[int] = None
    object_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    # Milestone 7: which research-question version this conclusion belongs to.
    question_version: int = Field(default=1, ge=1)
    status: Literal["current", "historical"] = "current"


class Connection(BaseModel):
    id: str
    from_ref: str  # finding id or "paper:<paper_id>"
    to_ref: str
    type: ConnectionType
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    # Milestone 7: which research-question version this conclusion belongs to.
    question_version: int = Field(default=1, ge=1)
    status: Literal["current", "historical"] = "current"


class OpenQuestion(BaseModel):
    id: str
    text: str = Field(min_length=1)
    raised_by: str  # paper_id
    finding_refs: list[str] = Field(default_factory=list)
    # Milestone 7: which research-question version this conclusion belongs to.
    question_version: int = Field(default=1, ge=1)
    status: Literal["current", "historical"] = "current"


class NoveltyRisk(BaseModel):
    """Corpus-scoped overlap candidate. The UI always renders these with a
    fixed 'Within the papers currently analyzed...' prefix, so the model must
    never phrase them as global novelty claims."""

    id: str
    direction: str = Field(min_length=1)
    overlapping_finding_refs: list[str] = Field(min_length=1)
    note: str = Field(min_length=1)
    raised_by: str  # paper_id
    # Milestone 7: which research-question version this conclusion belongs to.
    question_version: int = Field(default=1, ge=1)
    status: Literal["current", "historical"] = "current"


class QuestionVersionRecord(BaseModel):
    """Milestone 7: one entry of the research-question history.

    When the question changes, the previous question-dependent conclusions
    move into the superseded record's snapshot (never deleted); the live
    arrays hold only conclusions for the current question version.
    """

    version: int = Field(ge=1)
    question: str = Field(min_length=1)
    created_at: str = ""
    status: Literal["current", "superseded"] = "superseded"
    findings: list[Finding] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    novelty_risks: list[NoveltyRisk] = Field(default_factory=list)


class PaperContribution(BaseModel):
    paper_id: str
    what_it_adds: str = Field(min_length=1)
    stance_toward_prior: str = Field(min_length=1)
    overlap_with_direction: str = Field(min_length=1)
    new_open_questions: list[str] = Field(default_factory=list)


class VerificationEvidence(BaseModel):
    """Milestone 6: auditable record of one computational verification.

    Heavy blobs (code, CSV, plot, full logs) live under
    agent/verifications/<thread>/; this record carries project-relative
    paths plus the full interpretation. Observed measurements and
    model-generated interpretations are stored in separate fields.
    """

    id: str = ""
    thread_id: str
    paper_id: str
    claim_id: str
    claim_statement: str = ""
    object_refs: list[str] = Field(default_factory=list)
    status: Literal["supported", "not_supported", "inconclusive",
                    "execution_failed", "not_testable"]
    status_label: str = ""
    plan: dict = Field(default_factory=dict)
    testability: dict = Field(default_factory=dict)
    code_sha256: str = ""
    code_path: str = ""
    params: dict = Field(default_factory=dict)
    environment: dict = Field(default_factory=dict)
    measurements_path: str = ""
    plot_path: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    criteria: list[dict] = Field(default_factory=list)
    sanity_failed: list[str] = Field(default_factory=list)
    result_summary: dict = Field(default_factory=dict)
    observed: list[str] = Field(default_factory=list)
    supported_conclusions: list[str] = Field(default_factory=list)
    partial: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scaling_comment: str = ""
    repairs: list[dict] = Field(default_factory=list)
    attempts: int = 0
    modal: dict = Field(default_factory=dict)
    telemetry: list[dict] = Field(default_factory=list)
    created_at: str = ""
    # Milestone 7: originating question version (0 = legacy/unknown).
    question_version: int = Field(default=0, ge=0)


class ScientificAudit(BaseModel):
    """Milestone 6.1: structured evidence-audit report attached to a claim.

    Audit assessments are scientific (evidence-scoped) and deliberately
    distinct from M6 computational statuses. Disagreements are stored as
    separately attributed conclusions; audits never rewrite findings.
    """

    audit_id: str = ""
    thread_id: str
    project_id: str = ""
    paper_id: str
    claim_id: str
    claim_text: str = ""
    claim_type: list[str] = Field(default_factory=list)
    audit_scope: str = ""
    checks: list[dict] = Field(default_factory=list)
    methodology_findings: list[dict] = Field(default_factory=list)
    cross_paper_findings: list[dict] = Field(default_factory=list)
    external_sources: list[dict] = Field(default_factory=list)
    external_queries: list[str] = Field(default_factory=list)
    external_notes: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    overall_assessment: str = ""
    assessment_rationale: str = ""
    limitations: list[str] = Field(default_factory=list)
    provenance: dict = Field(default_factory=dict)
    model_telemetry: list[dict] = Field(default_factory=list)
    created_at: str = ""
    # Milestone 7: originating question version (0 = legacy/unknown).
    question_version: int = Field(default=0, ge=0)


class Project(BaseModel):
    schema_version: str = "m4-1"
    research_question: str = Field(min_length=1)
    papers: list[PaperEntry] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    contributions: list[PaperContribution] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    novelty_risks: list[NoveltyRisk] = Field(default_factory=list)
    verifications: list[VerificationEvidence] = Field(default_factory=list)
    audits: list[ScientificAudit] = Field(default_factory=list)
    # Milestone 8: autonomous literature discovery runs. Default-empty so all
    # pre-M8 project.json files load unchanged.
    discoveries: list[DiscoveryRun] = Field(default_factory=list)
    # Milestone 7: workspace metadata. All optional so M4-M6.1 files load
    # unchanged; only live arrays above hold CURRENT-version conclusions.
    title: str = ""
    question_version: int = Field(default=1, ge=1)
    created_at: str = ""
    updated_at: str = ""
    question_history: list[QuestionVersionRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_integrity(self) -> "Project":
        paper_ids = {p.paper_id for p in self.papers}
        if len(paper_ids) != len(self.papers):
            raise ValueError("paper ids must be unique")
        finding_ids = {f.id for f in self.findings}
        if len(finding_ids) != len(self.findings):
            raise ValueError("finding ids must be unique")
        for f in self.findings:
            if f.paper_id not in paper_ids:
                raise ValueError(f"finding {f.id} references unknown paper '{f.paper_id}'")
        for c in self.connections:
            for ref in (c.from_ref, c.to_ref):
                if ref.startswith("paper:"):
                    if ref[6:] not in paper_ids:
                        raise ValueError(f"connection {c.id} references unknown {ref}")
                elif ref not in finding_ids:
                    raise ValueError(f"connection {c.id} references unknown finding '{ref}'")
        for q in self.open_questions:
            if q.raised_by not in paper_ids:
                raise ValueError(f"question {q.id} references unknown paper '{q.raised_by}'")
            for ref in q.finding_refs:
                if ref not in finding_ids:
                    raise ValueError(f"question {q.id} references unknown finding '{ref}'")
        for r in self.novelty_risks:
            if r.raised_by not in paper_ids:
                raise ValueError(f"risk {r.id} references unknown paper '{r.raised_by}'")
            for ref in r.overlapping_finding_refs:
                if ref not in finding_ids:
                    raise ValueError(f"risk {r.id} references unknown finding '{ref}'")
        verify_ids = {v.id for v in self.verifications if v.id}
        if len(verify_ids) != len([v for v in self.verifications if v.id]):
            raise ValueError("verification ids must be unique")
        for v in self.verifications:
            if v.paper_id not in paper_ids:
                raise ValueError(f"verification {v.id} references unknown paper '{v.paper_id}'")
        audit_ids = {a.audit_id for a in self.audits if a.audit_id}
        if len(audit_ids) != len([a for a in self.audits if a.audit_id]):
            raise ValueError("audit ids must be unique")
        for a in self.audits:
            if a.paper_id not in paper_ids:
                raise ValueError(f"audit {a.audit_id} references unknown paper '{a.paper_id}'")
        run_ids = [r.run_id for r in self.discoveries if r.run_id]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("discovery run ids must be unique")
        for r in self.discoveries:
            cand_ids = [c.candidate_id for c in r.candidates]
            if len(set(cand_ids)) != len(cand_ids):
                raise ValueError(f"discovery run {r.run_id} has duplicate candidate ids")
        return self


# ---------------------------------------------------------------------------
# Model-output (delta) models for one add-paper call
# ---------------------------------------------------------------------------


class NewFinding(BaseModel):
    statement: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    page: Optional[int] = None
    object_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class NewConnection(BaseModel):
    from_ref: str  # existing finding id, paper:<id>, or new[i]
    to_ref: str
    type: ConnectionType
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class NewOpenQuestion(BaseModel):
    text: str = Field(min_length=1)
    finding_refs: list[str] = Field(default_factory=list)  # existing ids or new[i]


class NewNoveltyRisk(BaseModel):
    direction: str = Field(min_length=1)
    overlapping_finding_refs: list[str] = Field(min_length=1)
    note: str = Field(min_length=1)


class ContributionBody(BaseModel):
    what_it_adds: str = Field(min_length=1)
    stance_toward_prior: str = Field(min_length=1)
    overlap_with_direction: str = Field(min_length=1)
    new_open_questions: list[str] = Field(default_factory=list)


class ProjectDelta(BaseModel):
    findings: list[NewFinding] = Field(min_length=1, max_length=MAX_FINDINGS_PER_PAPER)
    connections: list[NewConnection] = Field(default_factory=list)
    contribution: ContributionBody
    open_questions: list[NewOpenQuestion] = Field(default_factory=list)
    novelty_risks: list[NewNoveltyRisk] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Delta application (deterministic; no model calls)
# ---------------------------------------------------------------------------

_NEW_REF_RE = re.compile(r"^new\[(\d+)\]$")


def _next_id(prefix: str, taken: set[str]) -> str:
    i = 1
    while f"{prefix}{i}" in taken:
        i += 1
    taken.add(f"{prefix}{i}")
    return f"{prefix}{i}"


def _resolve_new_refs(
    ref: str, new_ids: list[str], context: str
) -> str:
    m = _NEW_REF_RE.match(ref)
    if not m:
        return ref
    i = int(m.group(1))
    if i < 0 or i >= len(new_ids):
        raise ValueError(f"{context}: ref '{ref}' out of range ({len(new_ids)} new findings)")
    return new_ids[i]


def apply_delta(
    project: Project,
    paper: PaperEntry,
    analysis: AnalysisResult,
    delta: ProjectDelta,
) -> Project:
    """Append one paper's delta to the project. Raises ValueError on any
    ungrounded reference (unknown object id, finding id, or paper)."""
    if any(p.paper_id == paper.paper_id for p in project.papers):
        raise ValueError(f"paper '{paper.paper_id}' is already in the project")
    known_objects = {o.id for o in analysis.objects}
    for nf in delta.findings:
        for oid in nf.object_ids:
            if oid not in known_objects:
                raise ValueError(f"finding references unknown object id '{oid}'")

    taken_f = {f.id for f in project.findings}
    new_ids = [_next_id("f", taken_f) for _ in delta.findings]
    qv = project.question_version or 1
    findings = [
        Finding(
            id=fid,
            paper_id=paper.paper_id,
            statement=nf.statement,
            evidence=nf.evidence,
            page=nf.page,
            object_ids=nf.object_ids,
            confidence=nf.confidence,
            question_version=qv,
            status="current",
        )
        for fid, nf in zip(new_ids, delta.findings)
    ]

    # First paper in a project has nothing to connect to.
    if not project.papers and delta.connections:
        raise ValueError("first paper in a project must not propose connections")

    taken_c = {c.id for c in project.connections}
    connections = [
        Connection(
            id=_next_id("c", taken_c),
            from_ref=_resolve_new_refs(nc.from_ref, new_ids, "connection"),
            to_ref=_resolve_new_refs(nc.to_ref, new_ids, "connection"),
            type=nc.type,
            reason=nc.reason,
            confidence=nc.confidence,
            question_version=qv,
            status="current",
        )
        for nc in delta.connections
    ]

    taken_q = {q.id for q in project.open_questions}
    questions = [
        OpenQuestion(
            id=_next_id("q", taken_q),
            text=nq.text,
            raised_by=paper.paper_id,
            finding_refs=[
                _resolve_new_refs(r, new_ids, "open question") for r in nq.finding_refs
            ],
            question_version=qv,
            status="current",
        )
        for nq in delta.open_questions
    ]

    taken_n = {r.id for r in project.novelty_risks}
    risks = [
        NoveltyRisk(
            id=_next_id("n", taken_n),
            direction=nr.direction,
            overlapping_finding_refs=[
                _resolve_new_refs(r, new_ids, "novelty risk")
                for r in nr.overlapping_finding_refs
            ],
            note=nr.note,
            raised_by=paper.paper_id,
            question_version=qv,
            status="current",
        )
        for nr in delta.novelty_risks
    ]

    contribution = PaperContribution(paper_id=paper.paper_id, **delta.contribution.model_dump())

    merged = Project(
        schema_version=project.schema_version,
        research_question=project.research_question,
        papers=[*project.papers, paper],
        findings=[*project.findings, *findings],
        connections=[*project.connections, *connections],
        contributions=[*project.contributions, contribution],
        open_questions=[*project.open_questions, *questions],
        novelty_risks=[*project.novelty_risks, *risks],
        # Everything below must be preserved, never reset by an add-paper.
        # (M7 fix: verifications/audits used to be silently dropped here.)
        verifications=project.verifications,
        audits=project.audits,
        discoveries=project.discoveries,
        title=project.title,
        question_version=project.question_version,
        created_at=project.created_at,
        updated_at=project.updated_at,
        question_history=project.question_history,
    )
    return merged  # Project validator re-checks every reference


# ---------------------------------------------------------------------------
# Summaries + prompt
# ---------------------------------------------------------------------------


def summarize_analysis(analysis: AnalysisResult) -> str:
    """Compact paper brief: high/related objects in full, context as a list."""
    salient = [
        o for o in analysis.objects if o.research_relevance_level in ("high", "related")
    ]
    ctx = [o for o in analysis.objects if o.research_relevance_level == "context"]
    lines = [f"Title: {analysis.paper_title}", ""]
    for o in salient:
        lines.append(
            f"- [{o.id}] {o.label or o.type}: {o.title} "
            f"(relevance: {o.research_relevance_level})\n"
            f"  what: {o.what_it_is}\n"
            f"  claim: {o.claim_supported}\n"
            f"  does NOT establish: {o.limitations}\n"
            f"  relevance: {o.research_relevance} ({o.research_relevance_reason})"
        )
    if ctx:
        lines.append("")
        lines.append(
            "Context objects (labels only): "
            + ", ".join(f"{o.label or o.id}" for o in ctx)
        )
    return "\n".join(lines)


def summarize_findings(project: Project) -> str:
    if not project.findings:
        return "(no prior findings — this is the first paper in the project)"
    titles = {p.paper_id: p.title for p in project.papers}
    return "\n".join(
        f"- [{f.id}] ({titles.get(f.paper_id, f.paper_id)}): {f.statement} "
        f"[evidence: {f.evidence}]"
        for f in project.findings
    )


ADD_PAPER_SYSTEM_PROMPT = """You are ResearchOS cross-paper memory. A researcher adds ONE new paper to a \
research project with a persistent research question. You receive summaries of \
the project's EXISTING findings plus a brief of the NEW paper. Respond with a \
project delta (JSON, matching schema):

1. findings: 1-5 NEW grounded findings this paper contributes to the PROJECT \
(the question decides what counts). Each needs a concise evidence string with \
a paper location (e.g. "Table 2, p.7") and the analysis object ids it rests \
on (use ONLY ids from the brief; [] if none).
2. connections: typed links between findings, or FEWER, or NONE. Allowed \
endpoint refs: existing finding ids ("f3"), "paper:<paper_id>" for \
paper-level links, or "new[i]" for your i-th new finding (0-based). Types:
   - supports: independent evidence for the same statement
   - contradicts: incompatible results or claims (needs a clear reason)
   - extends: generalizes, improves, or builds on the earlier result
   - overlaps_with: same sub-problem, neither clearly builds on the other
   - uses_same_method: shared technique or experimental setup
   - uses_different_assumption: same question, incompatible premises
   - leaves_open: earlier finding poses what this paper still does not resolve
   Create ONLY relationships the papers actually support. Zero instances of \
any type is valid. Never force a connection.
3. contribution: what_it_adds (vs prior findings), stance_toward_prior \
(supports/challenges/orthogonal + why), overlap_with_direction (does this \
paper already explore the question's proposed direction?), new_open_questions \
(standalone strings).
4. open_questions: grounded follow-ups (finding refs required when one \
motivates them; use existing ids or new[i]).
5. novelty_risks: candidate overlaps between the research direction and what \
these papers already show. CORPUS-SCOPED ONLY: describe the direction and \
which findings it overlaps. NEVER claim a direction was never researched \
globally — you have only seen these papers.

Grounding rules: never invent evidence, pages, numbers, or results. If the \
brief lacks support for something, omit it. Return JSON only, no commentary."""


def build_add_paper_text(
    question: str, prior: str, paper_brief: str, is_first: bool
) -> str:
    first_note = (
        "This is the FIRST paper in the project: return findings, contribution, "
        "open questions, and novelty risks, but NO connections (connections: []).\n\n"
        if is_first
        else ""
    )
    return (
        f"Research question: \"{question}\"\n\n"
        f"{first_note}"
        f"EXISTING PROJECT FINDINGS:\n{prior}\n\n"
        f"NEW PAPER BRIEF:\n{paper_brief}\n\n"
        "Return the project delta for this paper."
    )


# ---------------------------------------------------------------------------
# Model API call
# ---------------------------------------------------------------------------


def request_delta(
    client: OpenAI, model: str, question: str, project: Project, analysis: AnalysisResult
) -> ProjectDelta:
    messages = [
        {"role": "system", "content": ADD_PAPER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_add_paper_text(
                question,
                summarize_findings(project),
                summarize_analysis(analysis),
                is_first=not project.papers,
            ),
        },
    ]
    attempts = [
        (
            "Chat Completions + json_schema",
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "project_delta",
                        "schema": ProjectDelta.model_json_schema(),
                    },
                }
            },
        ),
        ("Chat Completions plain JSON", {}),
    ]
    errors = []
    for name, kwargs in attempts:
        try:
            print(f"[attempt] add-paper {name} ...", file=sys.stderr)
            resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
            content = resp.choices[0].message.content or ""
            return ProjectDelta.model_validate_json(extract_json(content))
        except Exception as e:  # noqa: BLE001 - prototype: fall through to next attempt
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[attempt] add-paper {name} failed: {e}", file=sys.stderr)
    raise RuntimeError("add-paper attempts failed:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Project dir helpers + stage-web
# ---------------------------------------------------------------------------


def project_path(project_dir: Path) -> Path:
    return project_dir / "project.json"


def load_project(project_dir: Path) -> Project:
    return Project.model_validate_json(project_path(project_dir).read_text())


def save_project(project_dir: Path, project: Project) -> None:
    """Atomic write (temp file + os.replace) so interrupted writes cannot
    corrupt the existing project."""
    import os
    import tempfile

    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_path(project_dir)
    fd, tmp = tempfile.mkstemp(dir=str(project_dir), prefix="project.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(project.model_dump_json(indent=2) + "\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def stage_web(project_dir: Path, web_dir: Path) -> None:
    """Copy per-paper fixtures + project.json into the Next.js public dir.

    PDFs are copied for LOCAL use only (git-ignored); this tool never commits.
    """
    project = load_project(project_dir)
    repo_root = Path(__file__).resolve().parent.parent
    data_dir = web_dir / "public" / "data" / "papers"
    pdf_dir = web_dir / "public" / "papers"
    data_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    papers = []
    for p in project.papers:
        if not p.localized_path or not p.pdf_path:
            raise ValueError(f"paper '{p.paper_id}' misses local paths; cannot stage")
        src_data = (repo_root / p.localized_path).resolve()
        src_pdf = (repo_root / p.pdf_path).resolve()
        if not src_data.is_file() or not src_pdf.is_file():
            raise ValueError(f"paper '{p.paper_id}' local files missing; cannot stage")
        shutil.copyfile(src_data, data_dir / f"{p.paper_id}.json")
        shutil.copyfile(src_pdf, pdf_dir / f"{p.paper_id}.pdf")
        papers.append(
            p.model_copy(
                update={
                    "web_pdf": f"/papers/{p.paper_id}.pdf",
                    "web_data": f"/data/papers/{p.paper_id}.json",
                }
            )
        )
    staged = project.model_copy(update={"papers": papers})
    out = web_dir / "public" / "data" / "project.json"
    out.write_text(staged.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"[done] staged {len(papers)} papers + project.json into {web_dir}/public", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ResearchOS Milestone 4: project memory CLI.")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="Create an empty project with a research question.")
    pi.add_argument("--question", required=True)
    pi.add_argument("--project-dir", required=True)

    pa = sub.add_parser("add-paper", help="Analyze one paper relative to project memory.")
    pa.add_argument("--project-dir", required=True)
    pa.add_argument("--paper-id", required=True, help="URL-safe slug, e.g. deep-shells.")
    pa.add_argument("--pdf", required=True, help="Local PDF path.")
    pa.add_argument("--analysis", required=True, help="output.json from analyze_paper.py.")
    pa.add_argument("--localized", required=True, help="localized_output.json.")
    pa.add_argument("--model", default=DEFAULT_MODEL)
    pa.add_argument("--base-url", default=API_BASE_URL)

    ps = sub.add_parser("stage-web", help="Copy fixtures + project.json into web/public.")
    ps.add_argument("--project-dir", required=True)
    ps.add_argument("--web-dir", default="web")
    return p.parse_args(argv)


def cmd_init(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    if project_path(project_dir).exists():
        print(f"error: project already exists: {project_path(project_dir)}", file=sys.stderr)
        return 2
    save_project(project_dir, Project(research_question=args.question))
    print(f"[done] initialized project in {project_dir}", file=sys.stderr)
    return 0


def cmd_add_paper(args: argparse.Namespace) -> int:
    load_dotenv()
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        print(f"error: {API_KEY_ENV_VAR} is not set.", file=sys.stderr)
        return 2
    project_dir = Path(args.project_dir)
    if not project_path(project_dir).is_file():
        print(f"error: no project in {project_dir} (run init first)", file=sys.stderr)
        return 2
    for label, path in (("PDF", args.pdf), ("analysis", args.analysis), ("localized", args.localized)):
        if not Path(path).is_file():
            print(f"error: {label} not found: {path}", file=sys.stderr)
            return 2
    try:
        analysis = AnalysisResult.model_validate_json(Path(args.analysis).read_text())
        n_localized = sum(
            1 for o in json.loads(Path(args.localized).read_text())["localizations"] if o["bbox"]
        )
    except Exception as e:
        print(f"error: invalid analysis/localized file: {e}", file=sys.stderr)
        return 2
    project = load_project(project_dir)
    client = OpenAI(base_url=args.base_url, api_key=api_key)
    try:
        delta = request_delta(client, args.model, project.research_question, project, analysis)
    except Exception as e:
        print(f"error: add-paper failed: {e}", file=sys.stderr)
        return 1
    repo_root = Path(__file__).resolve().parent.parent

    def rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(repo_root))
        except ValueError:
            return p

    paper = PaperEntry(
        paper_id=args.paper_id,
        title=analysis.paper_title,
        filename=Path(args.pdf).name,
        added_order=len(project.papers) + 1,
        n_objects=len(analysis.objects),
        n_localized=n_localized,
        pdf_path=rel(args.pdf),
        localized_path=rel(args.localized),
    )
    try:
        project = apply_delta(project, paper, analysis, delta)
    except ValueError as e:
        print(f"error: model delta failed validation: {e}", file=sys.stderr)
        return 1
    save_project(project_dir, project)
    print(
        f"[done] added '{args.paper_id}': {len(delta.findings)} findings, "
        f"{len(delta.connections)} connections, {len(delta.open_questions)} questions, "
        f"{len(delta.novelty_risks)} risks",
        file=sys.stderr,
    )
    return 0


def cmd_stage_web(args: argparse.Namespace) -> int:
    try:
        stage_web(Path(args.project_dir), Path(args.web_dir))
    except Exception as e:
        print(f"error: stage-web failed: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "init":
        return cmd_init(args)
    if args.command == "add-paper":
        return cmd_add_paper(args)
    if args.command == "stage-web":
        return cmd_stage_web(args)
    raise AssertionError(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
