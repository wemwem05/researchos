#!/usr/bin/env python3
"""ResearchOS Milestone 5: Persistent Muse Spark Open Research Agent Graph.

Research is not a linear LLM chain. This module wraps the Milestone 4
add-paper step in a LangGraph workflow powered by Muse Spark Open (Baseten):

  load_project_context -> investigator (Spark) -> validate -> critic (Spark)
      -> decision_gate -> finalize | revise_once (Spark) | human_review
      -> apply_final_decision -> update_project_state -> END

Persistence: SQLite checkpoints (survive process/server restart) plus
file-backed run records and telemetry under <project>/agent/. The Milestone 4
project.json format is reused unchanged; only the reasoning path is agentic.

Refinements vs the topology sketch in the milestone request (behavior kept):
- `validate` failure ends the run as failed (no mutation) instead of routing
  to human review: malformed output is not a scientific judgment call.
- `finalize` (clean accept) flows into `apply_final_decision` so there is a
  single mutation point (`update_project_state`).
- `prepare_review` (deterministic, checkpointed) builds the review request
  before `human_review`, so the interrupt node itself has zero side effects
  before interrupt() and re-executes cleanly on resume.
- One thread per investigation run (project/paper/run id), all persisted.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from analyze_paper import AnalysisResult
from project import (
    CONNECTION_TYPES,
    NewOpenQuestion,
    PaperEntry,
    ProjectDelta,
    apply_delta,
    load_project,
    save_project,
    summarize_analysis,
)
from spark_open import SparkOpenClient, SparkOpenError, TelemetryRecord, utc_now_iso

SCHEMA_VERSION = "m5-1"

# Bounded automatic correction: exactly one revision pass, then accept or
# escalate to human review. No endless investigator/critic loops.
MAX_REVISIONS = 1

# Bounded structured-output repair inside deterministic validation.
MAX_VALIDATION_REPAIRS = 1

CriticVerdictValue = Literal["SUPPORTED", "REVISION_REQUIRED", "AMBIGUOUS_REQUIRES_HUMAN"]

# Phrases that claim more than the analyzed corpus allows. Checked
# deterministically (case-insensitive substring) against novelty direction+note.
GLOBAL_NOVELTY_PHRASES = (
    "never been studied",
    "never been researched",
    "never studied",
    "never researched",
    "no prior work",
    "first ever",
    "globally novel",
    "novel to the field",
    "no one has ever",
    "has never been explored",
)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    project_id: str
    thread_id: str
    research_question: str
    paper_id: str
    paper_title: str
    paper_brief: str
    analysis: dict[str, Any]
    is_first_paper: bool
    existing_findings: list[dict[str, Any]]
    new_paper_findings: list[dict[str, Any]]
    candidate_connections: list[dict[str, Any]]
    investigator_assessment: Optional[dict[str, Any]]
    critic_assessment: Optional[dict[str, Any]]
    critic_history: list[dict[str, Any]]
    revision_result: Optional[dict[str, Any]]
    final_proposal: Optional[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    novelty_risks: list[dict[str, Any]]
    needs_human_review: bool
    human_review_request: Optional[dict[str, Any]]
    human_review_response: Optional[dict[str, Any]]
    status: str
    decision: str
    retry_count: int
    revision_count: int
    valid: bool
    validation_errors: list[str]
    telemetry: list[dict[str, Any]]
    run_error: Optional[str]


def initial_state(project_id: str, thread_id: str, paper_id: str) -> AgentState:
    return {
        "project_id": project_id,
        "thread_id": thread_id,
        "research_question": "",
        "paper_id": paper_id,
        "paper_title": "",
        "paper_brief": "",
        "analysis": {},
        "is_first_paper": False,
        "existing_findings": [],
        "new_paper_findings": [],
        "candidate_connections": [],
        "investigator_assessment": None,
        "critic_assessment": None,
        "critic_history": [],
        "revision_result": None,
        "final_proposal": None,
        "open_questions": [],
        "novelty_risks": [],
        "needs_human_review": False,
        "human_review_request": None,
        "human_review_response": None,
        "status": "running",
        "decision": "",
        "retry_count": 0,
        "revision_count": 0,
        "valid": False,
        "validation_errors": [],
        "telemetry": [],
        "run_error": None,
    }


# ---------------------------------------------------------------------------
# Structured model I/O
# ---------------------------------------------------------------------------


class InvestigatorProposal(ProjectDelta):
    """Milestone 4 delta plus the investigator's comparative reasoning."""

    methodological_differences: list[str] = Field(default_factory=list)
    what_changed: str = ""


class ComparabilityCheck(BaseModel):
    name: str
    passed: bool
    note: str = ""


class CriticConcern(BaseModel):
    claim_ref: str = ""
    question: str = ""
    detail: str = ""
    severity: Literal["blocking", "major", "minor"] = "major"


class HumanQuestionDraft(BaseModel):
    question: str = ""
    investigator_position: str = ""
    critic_concern: str = ""
    affected_connection_indexes: list[int] = Field(default_factory=list)
    options: list[str] = Field(
        default_factory=lambda: [
            "Comparable — keep the connection",
            "Not comparable — drop the connection",
            "Keep unresolved",
        ]
    )


class CriticVerdict(BaseModel):
    verdict: CriticVerdictValue
    summary: str = ""
    checks: list[ComparabilityCheck] = Field(default_factory=list)
    concerns: list[CriticConcern] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)
    human_question: Optional[HumanQuestionDraft] = None


class HumanReviewRequest(BaseModel):
    kind: Literal["ambiguity", "revision_disagreement", "novelty_risk", "verification"] = "ambiguity"
    question: str
    why_stopped: str
    investigator_position: str
    critic_concern: str
    evidence_a: str
    evidence_b: str
    finding_refs: list[str] = Field(default_factory=list)
    object_refs: list[str] = Field(default_factory=list)
    affected_connection_indexes: list[int] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)
    allow_free_text: bool = True


class HumanReviewResponse(BaseModel):
    choice: str
    free_text: str = ""
    decided_at: Optional[str] = None
    decided_by: str = "researcher"


# ---------------------------------------------------------------------------
# Run records + telemetry persistence
# ---------------------------------------------------------------------------


class RunTransition(BaseModel):
    node: str
    at: str
    detail: str = ""


class RunRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    thread_id: str
    project_id: str
    kind: str = "investigate"  # M6 adds "verify"; old records default correctly
    paper_id: str
    claim_id: str = ""  # M6 verify runs only; empty for investigations
    research_question: str = ""
    status: str = "running"
    synthetic: bool = False
    created_at: str = ""
    updated_at: str = ""
    paper: dict[str, Any] = Field(default_factory=dict)
    analysis_path: str = ""
    transitions: list[RunTransition] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    validation_failures: list[str] = Field(default_factory=list)
    revision_path: list[str] = Field(default_factory=list)
    human_review_request: Optional[dict[str, Any]] = None
    human_review_response: Optional[dict[str, Any]] = None
    final_status: str = ""
    error: Optional[str] = None
    telemetry: list[dict[str, Any]] = Field(default_factory=list)


def agent_dir(project_dir: Path) -> Path:
    return project_dir / "agent"


def runs_dir(project_dir: Path) -> Path:
    return agent_dir(project_dir) / "runs"


def checkpoints_path(project_dir: Path) -> Path:
    return agent_dir(project_dir) / "checkpoints.sqlite"


def telemetry_path(project_dir: Path) -> Path:
    return agent_dir(project_dir) / "telemetry.jsonl"


def run_record_path(project_dir: Path, thread_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)
    return runs_dir(project_dir) / f"{safe}.json"


def new_thread_id(project_id: str, paper_id: str) -> str:
    return f"{project_id}--{paper_id}--{uuid.uuid4().hex[:8]}"


def save_run_record(project_dir: Path, record: RunRecord) -> None:
    runs_dir(project_dir).mkdir(parents=True, exist_ok=True)
    record.updated_at = utc_now_iso()
    run_record_path(project_dir, record.thread_id).write_text(
        record.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def load_run_record(project_dir: Path, thread_id: str) -> RunRecord:
    return RunRecord.model_validate_json(run_record_path(project_dir, thread_id).read_text())


def list_run_records(project_dir: Path) -> list[RunRecord]:
    d = runs_dir(project_dir)
    if not d.is_dir():
        return []
    records = []
    for f in sorted(d.glob("*.json")):
        try:
            records.append(RunRecord.model_validate_json(f.read_text()))
        except Exception:  # noqa: BLE001 - skip unreadable run files
            continue
    records.sort(key=lambda r: r.updated_at or r.created_at)
    return records


def record_transition(project_dir: Path, thread_id: str, node: str, detail: str = "") -> None:
    record = load_run_record(project_dir, thread_id)
    record.transitions.append(RunTransition(node=node, at=utc_now_iso(), detail=detail))
    if node == "human_review_requested":
        record.status = "awaiting_human"
    save_run_record(project_dir, record)


def record_telemetry(project_dir: Path, thread_id: str, tel: TelemetryRecord) -> None:
    agent_dir(project_dir).mkdir(parents=True, exist_ok=True)
    with telemetry_path(project_dir).open("a", encoding="utf-8") as f:
        f.write(tel.model_dump_json() + "\n")
    try:
        record = load_run_record(project_dir, thread_id)
    except FileNotFoundError:
        return
    record.telemetry.append(tel.model_dump())
    save_run_record(project_dir, record)


def read_telemetry(project_dir: Path) -> list[dict[str, Any]]:
    p = telemetry_path(project_dir)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def telemetry_summary(project_dir: Path) -> dict[str, Any]:
    rows = read_telemetry(project_dir)
    ok = [r for r in rows if r.get("success")]
    lat = [r["latency_ms"] for r in ok if isinstance(r.get("latency_ms"), int)]
    by_node: dict[str, int] = {}
    for r in rows:
        by_node[str(r.get("node", "?"))] = by_node.get(str(r.get("node", "?")), 0) + 1
    models = {str(r.get("model", "?")) for r in rows}
    providers = {str(r.get("provider", "?")) for r in rows}

    def _label(values: set[str]) -> str:
        if len(values) == 1:
            return next(iter(values))
        return "none" if not values else "mixed"

    return {
        "model": _label(models),
        "provider": _label(providers),
        "calls": len(rows),
        "successful_calls": len(ok),
        "failed_calls": len(rows) - len(ok),
        "avg_latency_ms": int(sum(lat) / len(lat)) if lat else None,
        "calls_by_node": by_node,
        "latest_at": rows[-1].get("timestamp") if rows else None,
    }


# ---------------------------------------------------------------------------
# Deterministic validation (the LLM is never the sole success criterion)
# ---------------------------------------------------------------------------


@dataclass
class ValidationContext:
    paper_ids: set[str]
    finding_ids: set[str]
    object_ids: set[str]
    is_first_paper: bool


def _ref_ok(ref: Any, ctx: ValidationContext, n_new: int) -> bool:
    if not isinstance(ref, str) or not ref:
        return False
    if ref.startswith("paper:"):
        return ref[6:] in ctx.paper_ids
    if ref.startswith("new[") and ref.endswith("]"):
        try:
            i = int(ref[4:-1])
        except ValueError:
            return False
        return 0 <= i < n_new
    return ref in ctx.finding_ids


def _confidence_ok(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0


def validate_proposal(proposal: Any, ctx: ValidationContext) -> list[str]:
    """Deterministic gates for an investigator/revision proposal.

    Accepts a dict (raw state) or a validated InvestigatorProposal. Returns a
    list of error strings; empty means the proposal may proceed. Any error
    blocks project mutation.
    """
    errors: list[str] = []
    if isinstance(proposal, BaseModel):
        proposal = proposal.model_dump()
    if not isinstance(proposal, dict):
        return ["malformed proposal: not a JSON object"]
    for key in ("findings", "connections", "contribution", "open_questions", "novelty_risks"):
        if key not in proposal:
            errors.append(f"malformed proposal: missing key '{key}'")
    if errors:
        return errors

    findings = proposal["findings"]
    if not isinstance(findings, list) or not (1 <= len(findings) <= 5):
        errors.append(f"findings must be a list of 1..5 items (got {len(findings) if isinstance(findings, list) else type(findings).__name__})")
        findings = []
    for i, f in enumerate(findings):
        tag = f"finding[{i}]"
        if not isinstance(f, dict):
            errors.append(f"{tag}: not an object")
            continue
        if not isinstance(f.get("statement"), str) or not f["statement"].strip():
            errors.append(f"{tag}: empty statement")
        if not isinstance(f.get("evidence"), str) or not f["evidence"].strip():
            errors.append(f"{tag}: missing evidence")
        if not _confidence_ok(f.get("confidence")):
            errors.append(f"{tag}: confidence out of bounds 0..1")
        page = f.get("page")
        if page is not None and (not isinstance(page, int) or isinstance(page, bool) or page < 1):
            errors.append(f"{tag}: page must be a positive integer or null")
        oids = f.get("object_ids", [])
        if not isinstance(oids, list) or any(not isinstance(o, str) for o in oids):
            errors.append(f"{tag}: object_ids must be a list of strings")
        else:
            if len(set(oids)) != len(oids):
                errors.append(f"{tag}: duplicate object ids")
            for oid in oids:
                if oid not in ctx.object_ids:
                    errors.append(f"{tag}: unknown object id '{oid}'")

    connections = proposal["connections"]
    if not isinstance(connections, list):
        errors.append("connections must be a list")
        connections = []
    if ctx.is_first_paper and connections:
        errors.append("first paper in a project must not propose connections")
    seen_links: set[tuple[str, str, str]] = set()
    for i, c in enumerate(connections):
        tag = f"connection[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{tag}: not an object")
            continue
        ctype = c.get("type")
        if ctype not in CONNECTION_TYPES:
            errors.append(f"{tag}: invalid connection type '{ctype}'")
        for end in ("from_ref", "to_ref"):
            if not _ref_ok(c.get(end), ctx, len(findings)):
                errors.append(f"{tag}: unknown {end} '{c.get(end)}'")
        if not isinstance(c.get("reason"), str) or not c["reason"].strip():
            errors.append(f"{tag}: missing reason")
        if not _confidence_ok(c.get("confidence")):
            errors.append(f"{tag}: confidence out of bounds 0..1")
        link = (str(c.get("from_ref")), str(c.get("to_ref")), str(ctype))
        if link in seen_links:
            errors.append(f"{tag}: duplicate connection {link}")
        seen_links.add(link)

    for key in ("contribution",):
        contrib = proposal[key]
        if not isinstance(contrib, dict):
            errors.append(f"{key}: not an object")
            continue
        for field in ("what_it_adds", "stance_toward_prior", "overlap_with_direction"):
            if not isinstance(contrib.get(field), str) or not contrib[field].strip():
                errors.append(f"{key}.{field}: empty")

    questions = proposal["open_questions"]
    if not isinstance(questions, list):
        errors.append("open_questions must be a list")
        questions = []
    for i, q in enumerate(questions):
        tag = f"open_question[{i}]"
        if not isinstance(q, dict):
            errors.append(f"{tag}: not an object")
            continue
        if not isinstance(q.get("text"), str) or not q["text"].strip():
            errors.append(f"{tag}: empty text")
        for ref in q.get("finding_refs", []) or []:
            if not _ref_ok(ref, ctx, len(findings)):
                errors.append(f"{tag}: unknown finding ref '{ref}'")

    risks = proposal["novelty_risks"]
    if not isinstance(risks, list):
        errors.append("novelty_risks must be a list")
        risks = []
    for i, r in enumerate(risks):
        tag = f"novelty_risk[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{tag}: not an object")
            continue
        if not isinstance(r.get("direction"), str) or not r["direction"].strip():
            errors.append(f"{tag}: empty direction")
        if not isinstance(r.get("note"), str) or not r["note"].strip():
            errors.append(f"{tag}: empty note")
        refs = r.get("overlapping_finding_refs", [])
        if not isinstance(refs, list) or not refs:
            errors.append(f"{tag}: overlapping_finding_refs must be a non-empty list")
        else:
            for ref in refs:
                if not _ref_ok(ref, ctx, len(findings)):
                    errors.append(f"{tag}: unknown finding ref '{ref}'")
        blob = f"{r.get('direction', '')} {r.get('note', '')}".lower()
        for phrase in GLOBAL_NOVELTY_PHRASES:
            if phrase in blob:
                errors.append(
                    f"{tag}: novelty wording exceeds the analyzed corpus ('{phrase}')"
                )
    return errors


# ---------------------------------------------------------------------------
# Spark Open prompts (the consequential reasoning lives here)
# ---------------------------------------------------------------------------


INVESTIGATOR_SYSTEM_PROMPT = """You are the ResearchOS scientific investigator, powered by an open-weight \
reasoning model. A researcher adds ONE new paper to a research project with a \
persistent research question. You receive the project's EXISTING findings plus \
a brief of the NEW paper. You decide what this paper contributes to the \
PROJECT (the question decides what counts). Respond with ONE JSON object:

1. findings: 1-5 NEW grounded findings (statement, evidence with a paper \
location like "Table 2, p.7", page or null, object_ids using ONLY ids from \
the brief, confidence 0..1).
2. connections: typed links (possibly NONE — zero of any type is valid). \
Endpoint refs: existing finding ids ("f3"), "paper:<paper_id>", or "new[i]" \
for your i-th new finding (0-based). Types: supports, contradicts, extends, \
overlaps_with, uses_same_method, uses_different_assumption, leaves_open. \
Create ONLY relationships the papers actually support; never force one.
3. contribution: what_it_adds, stance_toward_prior, overlap_with_direction, \
new_open_questions (standalone strings).
4. open_questions: grounded follow-ups (finding refs use existing ids or new[i]).
5. novelty_risks: candidate overlaps between the research direction and what \
these papers already show. CORPUS-SCOPED ONLY — never claim a direction was \
never researched globally; you have only seen these papers.
6. methodological_differences: concrete ways the new paper's setup differs \
from prior findings (datasets, splits, metrics, backbones, assumptions).
7. what_changed: what this paper changes in the project's understanding.

Grounding rules: never invent evidence, pages, numbers, or results. If the \
brief lacks support, omit the claim. Return JSON only, no commentary."""

CRITIC_SYSTEM_PROMPT = """You are the ResearchOS scientific critic, a skeptical reviewer with a distinct \
role from the investigator. You receive the research question, the \
investigator's proposal, and the underlying evidence briefs. Actively inspect \
the reasoning:

- Is the cited evidence sufficient for each claim?
- Are the compared papers actually measuring the same thing?
- Are datasets, train/test splits, and metrics comparable?
- Do different experimental assumptions undermine a comparison?
- Is an apparent contradiction a real contradiction?
- Is a conclusion causal when the evidence is only correlational?
- Is a novelty statement stronger than the analyzed corpus allows?
- Is the investigator ignoring limitations stated in the paper?
- Are method comparisons confounded by architecture/backbone differences?

Return ONE JSON object: verdict (SUPPORTED, REVISION_REQUIRED, or \
AMBIGUOUS_REQUIRES_HUMAN), summary, checks (named comparability checks with \
pass/fail + note), concerns (claim_ref, question, detail, severity \
blocking/major/minor), required_fixes (only for REVISION_REQUIRED: concrete \
corrections achievable from the available evidence), and human_question (only \
for AMBIGUOUS_REQUIRES_HUMAN: the exact question needing researcher judgment, \
both positions, affected connection indexes, response options).

Be genuinely skeptical but fair: do NOT reject statements artificially merely \
to create agent activity. Minor nits are SUPPORTED with minor concerns. Use \
REVISION_REQUIRED only when the available evidence can fix the problem, and \
AMBIGUOUS_REQUIRES_HUMAN only for consequential ambiguity a researcher must \
resolve. Return JSON only, no commentary."""

REVISE_SYSTEM_PROMPT = """You are the ResearchOS investigator issuing a corrected proposal. You receive \
your original proposal, the critic's required fixes and concerns, and the \
evidence briefs. Produce a corrected full proposal (same JSON shape as the \
original: findings, connections, contribution, open_questions, novelty_risks, \
methodological_differences, what_changed) that addresses every required fix \
using ONLY the available evidence. Keep the parts the critic did not \
challenge. Never invent evidence. Return JSON only, no commentary."""

REPAIR_SYSTEM_PROMPT = """You are fixing a malformed ResearchOS proposal. The proposal failed \
deterministic validation with the listed errors (unknown ids, bad refs, \
out-of-range values, or overbroad novelty wording). Rewrite it as a fully \
valid proposal with the same JSON shape, changing as little substance as \
possible: fix ONLY the errors, using only the allowed ids. Never invent \
evidence. Return JSON only, no commentary."""


def build_investigator_text(
    question: str, prior: str, paper_brief: str, paper_id: str, is_first: bool
) -> str:
    first_note = (
        "This is the FIRST paper in the project: return findings, contribution, "
        "open questions, and novelty risks, but NO connections (connections: []).\n\n"
        if is_first
        else ""
    )
    return (
        f'Research question: "{question}"\n\n'
        f"{first_note}"
        f"New paper id: {paper_id}\n\n"
        f"EXISTING PROJECT FINDINGS:\n{prior}\n\n"
        f"NEW PAPER BRIEF:\n{paper_brief}\n\n"
        "Return the project delta for this paper."
    )


def build_critic_text(question: str, proposal_json: str, prior: str, paper_brief: str) -> str:
    return (
        f'Research question: "{question}"\n\n'
        f"INVESTIGATOR PROPOSAL (JSON):\n{proposal_json}\n\n"
        f"EXISTING PROJECT FINDINGS:\n{prior}\n\n"
        f"NEW PAPER BRIEF:\n{paper_brief}\n\n"
        "Return your verdict."
    )


def build_revision_text(
    proposal_json: str, critic_json: str, prior: str, paper_brief: str
) -> str:
    return (
        "ORIGINAL PROPOSAL (JSON):\n"
        f"{proposal_json}\n\n"
        "CRITIC ASSESSMENT (JSON):\n"
        f"{critic_json}\n\n"
        f"EXISTING PROJECT FINDINGS:\n{prior}\n\n"
        f"NEW PAPER BRIEF:\n{paper_brief}\n\n"
        "Return the corrected proposal."
    )


def build_repair_text(proposal_json: str, errors: list[str], allowed: str) -> str:
    return (
        "PROPOSAL (JSON):\n"
        f"{proposal_json}\n\n"
        "VALIDATION ERRORS:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n\nALLOWED IDS AND REFS:\n"
        f"{allowed}\n\n"
        "Return the corrected proposal."
    )


# ---------------------------------------------------------------------------
# Graph runtime context (passed via config["configurable"], never checkpointed)
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    project_dir: Path
    analysis: AnalysisResult
    paper: PaperEntry
    spark: Any  # SparkOpenClient, or a test double with complete_json()
    synthetic: bool = False


def _ctx(config: RunnableConfig) -> RunContext:
    return config["configurable"]["ctx"]  # type: ignore[index]


def _validation_context(state: AgentState, analysis: AnalysisResult) -> ValidationContext:
    paper_ids = {f["paper_id"] for f in state["existing_findings"]}
    paper_ids.add(state["paper_id"])
    return ValidationContext(
        paper_ids=paper_ids,
        finding_ids={f["id"] for f in state["existing_findings"]},
        object_ids={o.id for o in analysis.objects},
        is_first_paper=state["is_first_paper"],
    )


def _current_proposal(state: AgentState) -> tuple[Optional[dict[str, Any]], str]:
    if state.get("revision_result") is not None:
        return state["revision_result"], "revision_result"
    return state.get("investigator_assessment"), "investigator_assessment"


def _proposal_views(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "new_paper_findings": proposal.get("findings", []),
        "candidate_connections": proposal.get("connections", []),
        "open_questions": proposal.get("open_questions", []),
        "novelty_risks": proposal.get("novelty_risks", []),
    }


def _fail(project_dir: Path, thread_id: str, state: AgentState, error: str) -> dict[str, Any]:
    record_transition(project_dir, thread_id, "failed", error[:300])
    try:
        record = load_run_record(project_dir, thread_id)
        record.status = "failed"
        record.final_status = "failed"
        record.error = error
        save_run_record(project_dir, record)
    except FileNotFoundError:
        pass
    return {"status": "failed", "run_error": error, "valid": False}


def _record_spark(
    project_dir: Path, thread_id: str, state: AgentState, tel: TelemetryRecord
) -> dict[str, Any]:
    record_telemetry(project_dir, thread_id, tel)
    return {"telemetry": [*state.get("telemetry", []), tel.model_dump()]}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def node_load_project_context(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    project = load_project(ctx.project_dir)
    brief = summarize_analysis(ctx.analysis)
    record_transition(
        ctx.project_dir,
        state["thread_id"],
        "load_project_context",
        f"{len(project.findings)} prior findings, paper '{ctx.paper.paper_id}'",
    )
    return {
        "research_question": project.research_question,
        "paper_title": ctx.analysis.paper_title,
        "paper_brief": brief,
        "analysis": ctx.analysis.model_dump(),
        "is_first_paper": len(project.papers) == 0,
        "existing_findings": [f.model_dump() for f in project.findings],
        "status": "running",
    }


def node_investigator(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    prior = _prior_text(state)
    messages = [
        {"role": "system", "content": INVESTIGATOR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_investigator_text(
                state["research_question"], prior, state["paper_brief"],
                state["paper_id"], state["is_first_paper"],
            ),
        },
    ]
    try:
        proposal, tel = ctx.spark.complete_json(
            messages, InvestigatorProposal, node="investigator", thread_id=state["thread_id"]
        )
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return _fail(ctx.project_dir, state["thread_id"], state, str(e))
    except Exception as e:  # noqa: BLE001 - test doubles raise plain errors; same fail-closed path
        return _fail(ctx.project_dir, state["thread_id"], state, f"investigator failed: {e}")
    update = _record_spark(ctx.project_dir, state["thread_id"], state, tel)
    record_transition(ctx.project_dir, state["thread_id"], "investigator",
                       f"{len(proposal.findings)} findings, {len(proposal.connections)} connections")
    _append_revision_path(ctx.project_dir, state["thread_id"], "investigator")
    return {
        **update,
        "investigator_assessment": proposal.model_dump(),
        **_proposal_views(proposal.model_dump()),
    }


def _prior_text(state: AgentState) -> str:
    if not state["existing_findings"]:
        return "(no prior findings — this is the first paper in the project)"
    return "\n".join(
        f"- [{f['id']}] ({f['paper_id']}): {f['statement']} [evidence: {f['evidence']}]"
        for f in state["existing_findings"]
    )


def _append_revision_path(project_dir: Path, thread_id: str, entry: str) -> None:
    try:
        record = load_run_record(project_dir, thread_id)
    except FileNotFoundError:
        return
    record.revision_path.append(entry)
    save_run_record(project_dir, record)


def _try_validation_repair(
    state: AgentState, config: RunnableConfig, proposal: dict[str, Any], errors: list[str]
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """One bounded Spark repair for deterministically invalid output."""
    ctx = _ctx(config)
    vctx = _validation_context(state, ctx.analysis)
    allowed = (
        f"finding ids: {sorted(vctx.finding_ids) or '(none yet)'}\n"
        f"paper refs: {[f'paper:{p}' for p in sorted(vctx.paper_ids)]}\n"
        f"new-finding refs: new[0..{len(proposal.get('findings', [])) - 1}]\n"
        f"object ids: {sorted(vctx.object_ids)}\n"
        f"connection types: {list(CONNECTION_TYPES)}\n"
        f"is_first_paper: {vctx.is_first_paper} (no connections allowed when true)"
    )
    messages = [
        {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
        {"role": "user", "content": build_repair_text(
            json.dumps(proposal), errors, allowed)},
    ]
    try:
        fixed, tel = ctx.spark.complete_json(
            messages, InvestigatorProposal, node="validate_repair",
            thread_id=state["thread_id"],
        )
    except Exception as e:  # noqa: BLE001 - repair failure fails the run closed
        record_transition(ctx.project_dir, state["thread_id"], "validate_repair_failed", str(e)[:200])
        return None, {"retry_count": state["retry_count"] + 1}
    update = _record_spark(ctx.project_dir, state["thread_id"], state, tel)
    record_transition(ctx.project_dir, state["thread_id"], "validate_repair", "1 repair attempt used")
    return fixed.model_dump(), {**update, "retry_count": state["retry_count"] + 1}


def node_validate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    proposal, slot = _current_proposal(state)
    if proposal is None:
        return _fail(ctx.project_dir, state["thread_id"], state, "no proposal to validate")
    vctx = _validation_context(state, ctx.analysis)
    errors = validate_proposal(proposal, vctx)
    update: dict[str, Any] = {}
    if errors and state["retry_count"] < MAX_VALIDATION_REPAIRS:
        fixed, repair_update = _try_validation_repair(state, config, proposal, errors)
        update.update(repair_update)
        if fixed is not None:
            proposal = fixed
            update[slot] = fixed
            update.update(_proposal_views(fixed))
            errors = validate_proposal(proposal, vctx)
    if errors:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.validation_failures.extend(errors)
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "validate_failed",
                          f"{len(errors)} errors; project state untouched")
        failed = _fail(ctx.project_dir, state["thread_id"], state,
                       "proposal failed deterministic validation: " + "; ".join(errors[:5]))
        return {**update, **failed}
    record_transition(ctx.project_dir, state["thread_id"], "validate", "all deterministic gates passed")
    return {**update, "valid": True, "validation_errors": []}


def node_critic(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    proposal, _ = _current_proposal(state)
    assert proposal is not None  # validate passed before critic
    node_name = "critic" if state["revision_count"] == 0 else "critic_after_revision"
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_critic_text(
                state["research_question"], json.dumps(proposal),
                _prior_text(state), state["paper_brief"],
            ),
        },
    ]
    try:
        verdict, tel = ctx.spark.complete_json(
            messages, CriticVerdict, node=node_name, thread_id=state["thread_id"]
        )
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return _fail(ctx.project_dir, state["thread_id"], state, str(e))
    except Exception as e:  # noqa: BLE001 - fail closed like investigator
        return _fail(ctx.project_dir, state["thread_id"], state, f"critic failed: {e}")
    update = _record_spark(ctx.project_dir, state["thread_id"], state, tel)
    record_transition(ctx.project_dir, state["thread_id"], node_name, f"verdict={verdict.verdict}")
    _append_revision_path(ctx.project_dir, state["thread_id"], f"{node_name}:{verdict.verdict}")
    return {
        **update,
        "critic_assessment": verdict.model_dump(),
        "critic_history": [*state.get("critic_history", []), verdict.model_dump()],
    }


def node_decision_gate(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    verdict = (state.get("critic_assessment") or {}).get("verdict", "")
    if verdict == "SUPPORTED":
        decision = "clean"
    elif verdict == "REVISION_REQUIRED" and state["revision_count"] < MAX_REVISIONS:
        decision = "revise"
    else:
        decision = "ambiguous"
    record_transition(ctx.project_dir, state["thread_id"], "decision_gate",
                      f"verdict={verdict} revisions_used={state['revision_count']} -> {decision}")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.decisions.append(f"decision_gate: {verdict} -> {decision}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    return {"decision": decision}


def node_revise_once(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    proposal, _ = _current_proposal(state)
    critic = state.get("critic_assessment") or {}
    messages = [
        {"role": "system", "content": REVISE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_revision_text(
                json.dumps(proposal), json.dumps(critic),
                _prior_text(state), state["paper_brief"],
            ),
        },
    ]
    try:
        fixed, tel = ctx.spark.complete_json(
            messages, InvestigatorProposal, node="revise_once",
            thread_id=state["thread_id"],
        )
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return _fail(ctx.project_dir, state["thread_id"], state, str(e))
    except Exception as e:  # noqa: BLE001 - fail closed
        return _fail(ctx.project_dir, state["thread_id"], state, f"revision failed: {e}")
    update = _record_spark(ctx.project_dir, state["thread_id"], state, tel)
    record_transition(ctx.project_dir, state["thread_id"], "revise_once", "bounded revision applied")
    _append_revision_path(ctx.project_dir, state["thread_id"], "revise_once")
    return {
        **update,
        "revision_result": fixed.model_dump(),
        "revision_count": state["revision_count"] + 1,
        "valid": False,
        **_proposal_views(fixed.model_dump()),
    }


def build_review_request(state: AgentState) -> HumanReviewRequest:
    critic = state.get("critic_assessment") or {}
    draft = critic.get("human_question") or {}
    verdict = critic.get("verdict", "")
    proposal, _ = _current_proposal(state)
    connections = (proposal or {}).get("connections", [])
    affected = [int(i) for i in draft.get("affected_connection_indexes", []) if isinstance(i, int)]
    affected = [i for i in affected if 0 <= i < len(connections)]
    finding_refs: list[str] = []
    for i in affected:
        for end in (connections[i].get("from_ref", ""), connections[i].get("to_ref", "")):
            if end and end not in finding_refs:
                finding_refs.append(end)
    object_refs: list[str] = []
    for f in state.get("new_paper_findings", []):
        for oid in f.get("object_ids", []) or []:
            if oid not in object_refs:
                object_refs.append(oid)
    if verdict == "REVISION_REQUIRED":
        kind: Literal["ambiguity", "revision_disagreement", "novelty_risk"] = "revision_disagreement"
        question = draft.get("question") or (
            "The investigator and critic still disagree after the bounded revision. "
            "Should the revised proposal be accepted into the project?"
        )
        options = draft.get("options") or ["Accept revised proposal", "Discard run"]
        why = "One automatic revision was already applied; the critic still requires changes."
    else:
        kind = "ambiguity"
        question = draft.get("question") or (
            "The critic flagged consequential ambiguity. Should this proposal be accepted?"
        )
        options = draft.get("options") or ["Accept proposal", "Discard run"]
        why = "The scientific critic judged this proposal ambiguous; researcher judgment is required."
    return HumanReviewRequest(
        kind=kind,
        question=question,
        why_stopped=why,
        investigator_position=draft.get("investigator_position") or _proposal_stance(state),
        critic_concern=draft.get("critic_concern") or critic.get("summary", ""),
        evidence_a=_prior_text(state)[:2000],
        evidence_b=_new_findings_text(state)[:2000],
        finding_refs=finding_refs,
        object_refs=object_refs,
        affected_connection_indexes=affected,
        options=options,
        allow_free_text=True,
    )


def _proposal_stance(state: AgentState) -> str:
    proposal, _ = _current_proposal(state)
    if not proposal:
        return ""
    contrib = proposal.get("contribution", {})
    return str(contrib.get("stance_toward_prior", ""))


def _new_findings_text(state: AgentState) -> str:
    out = []
    for i, f in enumerate(state.get("new_paper_findings", [])):
        out.append(f"- [new[{i}]]: {f.get('statement', '')} [evidence: {f.get('evidence', '')}]")
    for c in state.get("candidate_connections", []):
        out.append(f"- connection {c.get('from_ref')} --{c.get('type')}--> {c.get('to_ref')}: {c.get('reason', '')}")
    return "\n".join(out) or "(no proposed findings)"


def node_prepare_review(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Deterministic + checkpointed: build the review request BEFORE the wait."""
    ctx = _ctx(config)
    request = build_review_request(state)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.human_review_request = request.model_dump()
        record.decisions.append(f"human review requested ({request.kind}): {request.question[:200]}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "human_review_requested", request.kind)
    return {
        "needs_human_review": True,
        "human_review_request": request.model_dump(),
        "status": "awaiting_human",
    }


def node_human_review(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Dedicated interrupt node: no model/API side effects before interrupt()."""
    request = state.get("human_review_request") or {}
    resume_value = interrupt(request)
    response = HumanReviewResponse.model_validate(resume_value)
    if not response.decided_at:
        response.decided_at = utc_now_iso()
    ctx = _ctx(config)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.human_review_response = response.model_dump()
        record.decisions.append(f"human decision: {response.choice[:200]}")
        record.status = "running"
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "human_review_resumed", response.choice[:200])
    return {
        "human_review_response": response.model_dump(),
        "status": "running",
    }


def node_finalize(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    proposal, _ = _current_proposal(state)
    assert proposal is not None
    record_transition(ctx.project_dir, state["thread_id"], "finalize", "clean accept")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.decisions.append("finalize: critic SUPPORTED, no revision, no human review")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    return {"final_proposal": proposal}


def node_apply_final_decision(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    proposal, _ = _current_proposal(state)
    if proposal is None:
        return _fail(ctx.project_dir, state["thread_id"], state, "no proposal to finalize")
    response = state.get("human_review_response")
    working = json.loads(json.dumps(proposal))
    if response is None:
        record_transition(ctx.project_dir, state["thread_id"], "apply_final_decision", "clean path")
        return {"final_proposal": working}
    choice = str(response.get("choice", ""))
    free_text = str(response.get("free_text", ""))
    lowered = choice.lower()
    request = state.get("human_review_request") or {}
    if "discard" in lowered:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.status = "discarded"
            record.final_status = "discarded"
            record.decisions.append(f"researcher discarded the run: {choice[:200]}")
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "apply_final_decision", "discarded by researcher")
        return {"status": "discarded", "final_proposal": None}
    affected = request.get("affected_connection_indexes", []) or []
    if "not comparable" in lowered or "unresolved" in lowered or "keep unresolved" in lowered:
        kept = [c for i, c in enumerate(working.get("connections", [])) if i not in set(affected)]
        dropped = len(working.get("connections", [])) - len(kept)
        working["connections"] = kept
        note = f"dropped {dropped} contested connection(s) per researcher choice '{choice[:120]}'"
        if "unresolved" in lowered:
            refs = [r for r in request.get("finding_refs", []) if isinstance(r, str)]
            working.setdefault("open_questions", []).append(
                NewOpenQuestion(
                    text=f"Unresolved by researcher ({choice[:80]}): {request.get('question', '')[:300]}"
                    + (f" Note: {free_text[:300]}" if free_text else ""),
                    finding_refs=[r for r in refs if not r.startswith("paper:")][:4],
                ).model_dump()
            )
            note += "; recorded as an open question"
        record_transition(ctx.project_dir, state["thread_id"], "apply_final_decision", note)
    else:
        record_transition(ctx.project_dir, state["thread_id"], "apply_final_decision",
                          f"researcher accepted: {choice[:200]}")
    # Deterministic re-validation after the human edit; never mutate on failure.
    vctx = _validation_context(state, ctx.analysis)
    errors = validate_proposal(working, vctx)
    if errors:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.validation_failures.extend(errors)
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        return _fail(ctx.project_dir, state["thread_id"], state,
                     "post-review proposal invalid: " + "; ".join(errors[:5]))
    return {"final_proposal": working}


def node_update_project_state(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    final = state.get("final_proposal")
    if final is None:
        return _fail(ctx.project_dir, state["thread_id"], state, "no final proposal to apply")
    try:
        delta = ProjectDelta.model_validate(final)
    except Exception as e:  # noqa: BLE001 - fail closed, nothing written
        return _fail(ctx.project_dir, state["thread_id"], state, f"final proposal malformed: {e}")
    try:
        project = load_project(ctx.project_dir)
        project = apply_delta(project, ctx.paper, ctx.analysis, delta)
    except ValueError as e:
        return _fail(ctx.project_dir, state["thread_id"], state, f"delta rejected: {e}")
    save_project(ctx.project_dir, project)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.status = "completed"
        record.final_status = "completed"
        record.decisions.append(
            f"project updated: +{len(delta.findings)} findings, +{len(delta.connections)} connections"
        )
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "update_project_state",
                      f"paper '{ctx.paper.paper_id}' added")
    return {"status": "completed"}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _route_after_investigator(state: AgentState) -> str:
    return END if state.get("status") == "failed" else "validate"


def _route_after_validate(state: AgentState) -> str:
    return "critic" if state.get("valid") else END


def _route_after_gate(state: AgentState) -> str:
    decision = state.get("decision", "ambiguous")
    if decision == "clean":
        return "finalize"
    if decision == "revise":
        return "revise_once"
    return "prepare_review"


def _route_after_apply(state: AgentState) -> str:
    if state.get("status") in ("discarded", "failed"):
        return END
    return "update_project_state"


def build_graph(checkpointer: SqliteSaver):
    graph = StateGraph(AgentState)
    graph.add_node("load_project_context", node_load_project_context)
    graph.add_node("investigator", node_investigator)
    graph.add_node("validate", node_validate)
    graph.add_node("critic", node_critic)
    graph.add_node("decision_gate", node_decision_gate)
    graph.add_node("revise_once", node_revise_once)
    graph.add_node("prepare_review", node_prepare_review)
    graph.add_node("human_review", node_human_review)
    graph.add_node("finalize", node_finalize)
    graph.add_node("apply_final_decision", node_apply_final_decision)
    graph.add_node("update_project_state", node_update_project_state)

    graph.add_edge(START, "load_project_context")
    graph.add_edge("load_project_context", "investigator")
    graph.add_conditional_edges("investigator", _route_after_investigator,
                               {"validate": "validate", END: END})
    graph.add_conditional_edges("validate", _route_after_validate,
                               {"critic": "critic", END: END})
    graph.add_edge("critic", "decision_gate")
    graph.add_conditional_edges("decision_gate", _route_after_gate, {
        "finalize": "finalize",
        "revise_once": "revise_once",
        "prepare_review": "prepare_review",
    })
    graph.add_edge("revise_once", "validate")
    graph.add_edge("prepare_review", "human_review")
    graph.add_edge("human_review", "apply_final_decision")
    graph.add_edge("finalize", "apply_final_decision")
    graph.add_conditional_edges("apply_final_decision", _route_after_apply, {
        "update_project_state": "update_project_state", END: END})
    graph.add_edge("update_project_state", END)
    return graph.compile(checkpointer=checkpointer)


def resume_command(choice: str, free_text: str = "") -> Command:
    return Command(resume={"choice": choice, "free_text": free_text,
                           "decided_at": utc_now_iso(), "decided_by": "researcher"})

