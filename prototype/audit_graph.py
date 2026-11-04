#!/usr/bin/env python3
"""ResearchOS Milestone 6.1: Scientific Evidence Audit graph.

Auditing is NOT reproduction. For a researcher-selected claim, this
LangGraph workflow classifies the claim, collects grounded internal
evidence (claim + referenced objects + PDF page excerpts), plans
claim-specific checks, executes numerical checks deterministically in
Python, assesses methodology, compares against existing project
findings, optionally consults bounded external literature, and submits
the audit to a scientific critic — with durable human review for
genuine ambiguity.

Topology:

  load_claim_and_project -> classify_claim (Spark) -> collect_evidence
  -> plan_checks (Spark) -> execute_numerical_checks (Python)
  -> methodology_assessment (Spark) -> compare_project_evidence (Spark)
  -> external_gate -> [bounded_literature_search -> assess_sources]
  -> scientific_critic (Spark) -> decision_gate
  -> finalize | bounded_revision (max 1) | human_review (interrupt)
  -> evidence_report -> attach_to_project -> END

Reuses the M5/M6 infrastructure: same SQLite checkpointer file,
RunRecord (kind="audit"), telemetry log, review request/response
models, and atomic project saves. The M6 Modal graph is NOT invoked
here; computational testing stays a separate optional action.
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, BeforeValidator, Field

from agent_graph import (
    CriticVerdict,
    HumanReviewRequest,
    HumanReviewResponse,
    RunRecord,
    agent_dir,
    load_run_record,
    record_telemetry,
    record_transition,
    save_run_record,
)
from analyze_paper import AnalysisResult
from openalex import MAX_QUERIES, MAX_RECORDS, MAX_SELECTED, OpenAlexError
from project import PaperEntry, load_project, save_project
from spark_open import SparkOpenError, TelemetryRecord, utc_now_iso

SCHEMA_VERSION = "m6.1-1"

# One bounded audit revision, then accept or escalate (mirrors M5).
MAX_AUDIT_REVISIONS = 1

# Evidence-collection budgets (keeps checkpoints small and prompts focused).
MAX_EVIDENCE_PAGES = 8
MAX_PAGE_CHARS = 4000
MAX_OBJECT_CHARS = 1500
MAX_FINDINGS_BRIEF_CHARS = 6000

ClaimType = Literal[
    "numerical",
    "mathematical",
    "empirical_benchmark",
    "methodological",
    "causal",
    "novelty_sota",
    "physical_experimental",
    "computational_efficiency",
    "qualitative",
]

CHECK_STATUS = ("PASSED", "DISCREPANCY", "POTENTIAL_CONCERN", "INCONCLUSIVE", "NOT_EVALUATED")

OVERALL_ASSESSMENTS = (
    "substantiated within audited evidence",
    "partially substantiated",
    "discrepancies identified",
    "inconclusive on audited evidence",
    "insufficient evidence",
)
# Deliberately disjoint from the M6 computational statuses
# (supported/not_supported/inconclusive/execution_failed/not_testable):
# audit assessments must never be confused with execution verdicts.

# Deterministic numerical-check operations. Unknown ops yield NOT_EVALUATED,
# never a crash and never a guessed verdict.
NUMERICAL_OPS = (
    "percentage_improvement",
    "error_reduction",
    "best_method",
    "ranking",
    "aggregate",
    "ratio",
    "difference",
    "unit_check",
)


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class AuditState(TypedDict):
    project_id: str
    thread_id: str
    research_question: str
    paper_id: str
    claim_id: str
    claim_statement: str
    claim_evidence: str
    object_refs: list[str]
    paper_brief: str
    claim_types: list[str]
    classification_rationale: str
    evidence_bundle: dict[str, Any]
    audit_plan: Optional[dict[str, Any]]
    numerical_results: list[dict[str, Any]]
    methodology_findings: list[dict[str, Any]]
    cross_paper: list[dict[str, Any]]
    external_enabled: bool
    external_queries: list[str]
    external_sources: list[dict[str, Any]]
    external_notes: list[str]
    critic_assessment: Optional[dict[str, Any]]
    revision_count: int
    unresolved_questions: list[str]
    overall_assessment: str
    needs_human_review: bool
    human_review_request: Optional[dict[str, Any]]
    human_review_response: Optional[dict[str, Any]]
    status: str
    decision: str
    telemetry: list[dict[str, Any]]
    run_error: Optional[str]
    audit_report: Optional[dict[str, Any]]


def initial_audit_state(
    project_id: str, thread_id: str, paper_id: str, claim_id: str, external: bool = False
) -> AuditState:
    return {
        "project_id": project_id,
        "thread_id": thread_id,
        "research_question": "",
        "paper_id": paper_id,
        "claim_id": claim_id,
        "claim_statement": "",
        "claim_evidence": "",
        "object_refs": [],
        "paper_brief": "",
        "claim_types": [],
        "classification_rationale": "",
        "evidence_bundle": {},
        "audit_plan": None,
        "numerical_results": [],
        "methodology_findings": [],
        "cross_paper": [],
        "external_enabled": external,
        "external_queries": [],
        "external_sources": [],
        "external_notes": [],
        "critic_assessment": None,
        "revision_count": 0,
        "unresolved_questions": [],
        "overall_assessment": "",
        "needs_human_review": False,
        "human_review_request": None,
        "human_review_response": None,
        "status": "running",
        "decision": "",
        "telemetry": [],
        "run_error": None,
        "audit_report": None,
    }


# ---------------------------------------------------------------------------
# Structured model I/O
# ---------------------------------------------------------------------------


class ClaimClassification(BaseModel):
    types: list[ClaimType] = Field(min_length=1)
    rationale: str = ""


def _lenient_direction(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("lower", "lower is better", "lower-is-better", "min", "smaller is better"):
            return "lower"
        if lowered in ("higher", "higher is better", "higher-is-better", "max", "larger is better"):
            return "higher"
        if lowered in ("na", "n/a", "none", ""):
            return "na"
    return value


def _lenient_float(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").strip()
        try:
            return float(cleaned)
        except ValueError:
            return value
    return value


def _lenient_str_list(value: Any) -> Any:
    if isinstance(value, str):
        return [value]
    return value


class NumericalCheckProposal(BaseModel):
    check_id: str = ""
    check_type: str = ""  # one of NUMERICAL_OPS; unknown -> NOT_EVALUATED
    description: str = ""
    metric_direction: Annotated[Literal["higher", "lower", "na"],
                                BeforeValidator(_lenient_direction)] = "na"
    values: dict[str, float] = Field(default_factory=dict)
    expected_value: Annotated[Optional[float], BeforeValidator(_lenient_float)] = None
    expected_label: Optional[str] = None
    expected_order: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)
    op_detail: str = ""
    abs_tol: float = 1e-9
    rel_tol: float = 1e-6
    source_snippet: str = ""
    evidence_refs: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)


class AuditPlan(BaseModel):
    scope_notes: str = ""
    numerical_checks: list[NumericalCheckProposal] = Field(default_factory=list)
    methodology_focus: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)
    cross_paper_targets: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)
    needs_external: bool = False
    external_rationale: str = ""
    suggested_queries: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)
    limitations: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)


class MethodologyFinding(BaseModel):
    severity: Literal["OBSERVED_PROBLEM", "POTENTIAL_CONCERN", "MISSING_INFORMATION"] = "POTENTIAL_CONCERN"
    category: str = ""
    finding: str = ""
    evidence: str = ""
    evidence_refs: Annotated[list[str], BeforeValidator(_lenient_str_list)] = Field(
        default_factory=list)


class MethodologyAssessment(BaseModel):
    findings: list[MethodologyFinding] = Field(default_factory=list)


class CrossPaperFinding(BaseModel):
    comparability: Literal[
        "DIRECTLY_COMPARABLE", "PARTIALLY_COMPARABLE",
        "NOT_DIRECTLY_COMPARABLE", "UNCERTAIN",
    ] = "UNCERTAIN"
    finding_ref: str = ""
    paper_id: str = ""
    relationship: str = ""
    detail: str = ""
    evidence: str = ""


class CrossPaperAssessment(BaseModel):
    comparisons: list[CrossPaperFinding] = Field(default_factory=list)


class AuditRevision(BaseModel):
    corrected_methodology: list[MethodologyFinding] = Field(default_factory=list)
    corrected_cross_paper: list[CrossPaperFinding] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Audit artifact layout
# ---------------------------------------------------------------------------


def audits_dir(project_dir: Path) -> Path:
    return agent_dir(project_dir) / "audits"


def audit_thread_dir(project_dir: Path, thread_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)
    return audits_dir(project_dir) / safe


def new_audit_thread_id(project_id: str, paper_id: str, claim_id: str) -> str:
    import uuid as _uuid

    return f"{project_id}--audit--{paper_id}--{claim_id}--{_uuid.uuid4().hex[:8]}"


def openalex_cache_dir(project_dir: Path) -> Path:
    d = agent_dir(project_dir) / "cache" / "openalex"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Spark Open prompts
# ---------------------------------------------------------------------------


CLASSIFY_SYSTEM_PROMPT = """You are the ResearchOS claim classifier. Given ONE scientific claim \
plus a paper brief, label it with one or more types from EXACTLY this set: \
numerical, mathematical, empirical_benchmark, methodological, causal, \
novelty_sota, physical_experimental, computational_efficiency, qualitative. \
A claim may belong to multiple categories. Return ONE JSON object: \
{types, rationale}. JSON only, no commentary."""

PLAN_SYSTEM_PROMPT = """You are the ResearchOS audit planner. Given a classified claim and its \
grounded evidence bundle, design a claim-specific audit. Return ONE JSON \
object: {scope_notes, numerical_checks, methodology_focus, \
cross_paper_targets, needs_external, external_rationale, suggested_queries \
(max 2), limitations}.

Category guidance (apply the claim's types):
- numerical: propose checks recomputing arithmetic, differences, ratios, \
units, rounding. Every check needs source_snippet (exact table/cell \
context), named values, expected value, tolerances, evidence refs.
- mathematical: focus methodology on definitions, assumptions, equation \
consistency, whether conclusions follow; propose equation-substitution \
checks only with concrete numbers from the evidence.
- empirical_benchmark: propose best_method/ranking/percentage_improvement \
checks from reported tables; focus methodology on baselines, metrics, \
dataset comparability, caveats. State metric direction (higher/lower).
- methodological: focus on controls, protocol completeness, evaluation \
design, confounders.
- causal: demand controls/confounders/uncertainty; never accept benchmark \
improvement alone as causal proof.
- novelty_sota: scope to task+dataset+metric+PUBLICATION DATE. Never treat \
historical SOTA as present SOTA. Compare only within the audited corpus \
unless external search is enabled and justified.
- physical_experimental: audit protocol completeness and internal \
consistency only; never claim undocumented execution was verified.
- computational_efficiency: propose complexity/evidence checks; a CPU \
benchmark is a SEPARATE optional action, not part of this audit.
- qualitative: focus on definitions, scope, and supporting observations.

Schema rules (violations fail validation):
- check_type is EXACTLY one of: percentage_improvement, error_reduction,
  best_method, ranking, aggregate, ratio, difference, unit_check.
- metric_direction is EXACTLY one of: "higher", "lower", "na".
- values maps short names to NUMBERS only, never prose.
- expected_value is a NUMBER or null (percentages as plain numbers,
  30.0 means 30%); use expected_label for best_method and
  expected_order (string list) for ranking.
- limitations is a JSON ARRAY of strings, never one long string.
- op inputs: percentage_improvement {baseline, proposed};
  error_reduction {before, after}; best_method/ranking values map plus
  expected label/order; aggregate values plus op_detail mean|sum;
  ratio/difference values {a, b}; unit_check expected_label + op_detail.

needs_external=true ONLY when project evidence cannot settle a specific \
question AND targeted literature could; give at most 2 focused queries. \
scope_notes must state what this audit does NOT cover. \
JSON only, no commentary."""

METHODOLOGY_SYSTEM_PROMPT = """You are the ResearchOS methodology auditor. Assess assumptions, \
controls, datasets, train/test separation, evaluation protocol, metric \
comparability, baseline fairness, confounders, statistical uncertainty, \
reproducibility information, theory-vs-measured gaps, and author-stated \
limitations for the claim, using ONLY the evidence bundle. Return ONE JSON \
object: {findings: [{severity, category, finding, evidence, \
evidence_refs}]}. Severity is OBSERVED_PROBLEM (paper explicitly \
establishes it), POTENTIAL_CONCERN (suggested limitation), or \
MISSING_INFORMATION (details absent). Never present potential concerns as \
proven errors. JSON only, no commentary."""

CROSS_PAPER_SYSTEM_PROMPT = """You are the ResearchOS cross-paper auditor. Compare the selected \
claim against the given project findings from OTHER papers. Return ONE JSON \
object: {comparisons: [{comparability, finding_ref, paper_id, \
relationship, detail, evidence}]}. comparability is DIRECTLY_COMPARABLE, \
PARTIALLY_COMPARABLE, NOT_DIRECTLY_COMPARABLE, or UNCERTAIN. Different \
datasets are not automatic contradictions; similar numbers are not automatic \
confirmation; different metrics/assumptions block direct comparison. Use \
ONLY the provided finding/paper IDs. Never manufacture relationships. \
JSON only, no commentary."""

AUDIT_CRITIC_SYSTEM_PROMPT = """You are the ResearchOS audit critic. Skeptically review a completed \
claim audit: are numerical checks correctly computed and grounded? Are \
methodology severities justified (observed vs potential vs missing)? Are \
cross-paper comparability judgments defensible? Is external evidence \
overclaimed (abstracts are not full text)? Is the scope honest? Return ONE \
JSON object: verdict (SUPPORTED (audit stands), REVISION_REQUIRED (fixable \
from available evidence), or AMBIGUOUS_REQUIRES_HUMAN (genuine judgment \
call)), summary, checks (a JSON array of OBJECTS, each exactly \
{name, passed, note} with passed a JSON boolean - never bare strings), \
concerns (array of {claim_ref, question, detail, severity} with severity \
blocking/major/minor), required_fixes (only for REVISION_REQUIRED: concrete \
corrections achievable from the available evidence), and human_question \
(only for AMBIGUOUS_REQUIRES_HUMAN: the exact question needing researcher \
judgment, both positions, response options). Be fair: minor nits are \
SUPPORTED. JSON only, no commentary."""

AUDIT_REVISE_SYSTEM_PROMPT = """You are the ResearchOS audit reviser. Given the audit sections, the \
critic's required fixes, and the evidence bundle, return ONE JSON object: \
{corrected_methodology, corrected_cross_paper, notes} with FULL corrected \
finding lists. Address every required fix using ONLY available evidence. \
Keep unchallenged findings. Never invent evidence. JSON only, no commentary."""


def build_classify_text(claim: str, evidence: str, paper_brief: str) -> str:
    return (
        f"Claim under audit: {claim}\nClaim evidence: {evidence}\n\n"
        f"PAPER BRIEF:\n{paper_brief}\n\nReturn the classification."
    )


def build_plan_text(types: list[str], claim: str, bundle_json: str) -> str:
    return (
        f"Claim types: {types}\nClaim: {claim}\n\n"
        f"EVIDENCE BUNDLE (JSON):\n{bundle_json}\n\nReturn the audit plan."
    )


def build_methodology_text(claim: str, focus: list[str], bundle_json: str) -> str:
    return (
        f"Claim: {claim}\nMethodology focus: {focus}\n\n"
        f"EVIDENCE BUNDLE (JSON):\n{bundle_json}\n\nReturn the assessment."
    )


def build_cross_paper_text(claim: str, targets: list[str], findings_json: str) -> str:
    return (
        f"Claim under audit: {claim}\nComparison targets: {targets}\n\n"
        f"PROJECT FINDINGS (other papers, JSON):\n{findings_json}\n\n"
        "Return the comparison."
    )


def build_critic_text(audit_json: str) -> str:
    return f"COMPLETED AUDIT (JSON):\n{audit_json}\n\nReturn your verdict."


def build_revision_text(audit_json: str, critic_json: str, bundle_json: str) -> str:
    return (
        f"AUDIT SECTIONS (JSON):\n{audit_json}\n\n"
        f"CRITIC ASSESSMENT (JSON):\n{critic_json}\n\n"
        f"EVIDENCE BUNDLE (JSON):\n{bundle_json}\n\nReturn the revision."
    )


# ---------------------------------------------------------------------------
# Deterministic evidence collection
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def extract_pdf_pages(pdf_path: Path, pages: list[int]) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract raw page text with PyMuPDF. Gaps are reported, never filled."""
    import fitz  # PyMuPDF (already a project dependency)

    out: list[dict[str, Any]] = []
    gaps: list[str] = []
    if not pdf_path.is_file():
        return out, [f"PDF not available locally: {pdf_path}"]
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:  # noqa: BLE001 - reported as an evidence gap
        return out, [f"PDF unreadable: {e}"]
    try:
        for p in sorted(set(pages))[:MAX_EVIDENCE_PAGES]:
            if p < 1 or p > doc.page_count:
                gaps.append(f"page {p} out of range (1..{doc.page_count})")
                continue
            try:
                text = doc[p - 1].get_text("text") or ""
            except Exception as e:  # noqa: BLE001
                gaps.append(f"page {p} extraction failed: {e}")
                continue
            text, cut = _truncate(text.strip(), MAX_PAGE_CHARS)
            out.append({"page": p, "chars": len(text), "truncated": cut, "text": text})
    finally:
        doc.close()
    return out, gaps


def collect_evidence_bundle(
    analysis: AnalysisResult,
    claim_id: str,
    pdf_path: Path | None,
    project_findings_brief: str,
) -> dict[str, Any]:
    """Assemble the grounded evidence bundle for one claim.

    Never audits from the short summary alone: merges the claim object,
    referenced objects, and raw PDF page excerpts. Reports gaps instead
    of inventing missing content.
    """
    claim = next((o for o in analysis.objects if o.id == claim_id), None)
    if claim is None:
        raise ValueError(f"unknown claim '{claim_id}'")
    bundle: dict[str, Any] = {
        "claim": {
            "id": claim.id,
            "title": claim.title,
            "statement": f"{claim.what_it_is} {claim.claim_supported}".strip(),
            "evidence": claim.evidence,
            "limitations": claim.limitations,
            "page": claim.page,
        },
        "objects": [],
        "page_texts": [],
        "gaps": [],
        "project_findings_brief": project_findings_brief[:MAX_FINDINGS_BRIEF_CHARS],
    }
    by_id = {o.id: o for o in analysis.objects}
    for ref in [claim_id, *(claim.related_objects or [])]:
        o = by_id.get(ref)
        if o is None:
            bundle["gaps"].append(f"referenced object '{ref}' not in analysis")
            continue
        text = (f"{o.title}. {o.what_it_is} {o.claim_supported} "
                f"Evidence: {o.evidence} Limitations: {o.limitations}")
        text, cut = _truncate(text.strip(), MAX_OBJECT_CHARS)
        bundle["objects"].append({
            "id": o.id, "type": o.type, "page": o.page, "label": o.label,
            "truncated": cut, "text": text,
        })
    pages = [claim.page or 0] + [by_id[r].page or 0 for r in (claim.related_objects or [])
                                 if r in by_id]
    pages = [p for p in pages if p and p > 0]
    if pdf_path is None:
        bundle["gaps"].append("paper PDF path unknown; no page excerpts")
    elif pages:
        texts, gaps = extract_pdf_pages(pdf_path, pages)
        bundle["page_texts"] = texts
        bundle["gaps"].extend(gaps)
    else:
        bundle["gaps"].append("no page numbers on claim/objects; no page excerpts")
    bundle["gaps"].append(
        "section-level retrieval (methods/limitations/conclusions) unavailable; "
        "audit uses object evidence plus page excerpts only")
    return bundle


# ---------------------------------------------------------------------------
# Deterministic numerical-check executors
# ---------------------------------------------------------------------------


_NUMBER_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _normalize_number_token(token: str) -> str:
    token = token.strip()
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    if token.startswith("-0") and len(token) > 2 and token[1] == "0":
        token = "-" + token[2:]
    token = token.lstrip("0") or "0"
    if token.startswith("."):
        token = "0" + token
    if token.startswith("-."):
        token = "-0." + token[2:]
    return token


def _snippet_numbers(snippet: str) -> set[str]:
    return {_normalize_number_token(t) for t in _NUMBER_TOKEN.findall(snippet or "")}


def trace_values(values: dict[str, float], snippet: str) -> list[str]:
    """Every cited value must appear in the cited table/cell context.

    Returns the names of values NOT traceable (empty = all grounded).
    """
    tokens = _snippet_numbers(snippet)
    missing = []
    for name, v in values.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            missing.append(f"{name} (non-finite)")
            continue
        candidates = {_normalize_number_token(repr(float(v)))}
        if float(v).is_integer():
            candidates.add(_normalize_number_token(str(int(v))))
        if not (candidates & tokens):
            missing.append(name)
    return missing


def _rounding_half_unit(expected: float) -> float:
    """Half a unit in the last printed decimal place of the expected value.

    The planner quotes expected values at paper precision (e.g. 7.89 for an
    exact 7.894737); a recomputation that rounds to the quoted literal is
    consistent with the paper, not a discrepancy.
    """
    rep = repr(float(expected))
    if "e" in rep or "E" in rep or "inf" in rep or "nan" in rep:
        return 0.0
    if "." not in rep:
        return 0.5
    return 0.5 * (10.0 ** -len(rep.split(".")[1]))


def _within(expected: float, recomputed: float, abs_tol: float, rel_tol: float) -> bool:
    eff_abs = max(abs_tol, _rounding_half_unit(expected))
    return abs(recomputed - expected) <= eff_abs + rel_tol * abs(expected) + 1e-12


def execute_numerical_check(check: dict[str, Any]) -> dict[str, Any]:
    """Run one proposed check in Python. Returns the full check record."""
    base = {
        "check_id": check.get("check_id", ""),
        "check_type": check.get("check_type", ""),
        "description": check.get("description", ""),
        "status": "NOT_EVALUATED",
        "evidence_refs": check.get("evidence_refs", []),
        "actual_computation_or_reasoning": "",
        "limitations": "",
    }
    op = check.get("check_type", "")
    values = check.get("values", {}) or {}
    snippet = check.get("source_snippet", "") or ""
    if op not in NUMERICAL_OPS:
        base["actual_computation_or_reasoning"] = f"unknown operation '{op}'"
        base["limitations"] = "unsupported check type; not evaluated"
        return base
    missing = trace_values(values, snippet)
    if missing:
        base["actual_computation_or_reasoning"] = (
            f"cited value(s) not found in source context: {missing}")
        base["limitations"] = "values untraceable to cited table/cell context"
        return base
    direction = check.get("metric_direction", "na")
    abs_tol = float(check.get("abs_tol", 1e-9) or 0.0)
    rel_tol = float(check.get("rel_tol", 1e-6) or 0.0)
    try:
        return _EXECUTORS[op](base, check, values, direction, abs_tol, rel_tol)
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as e:
        base["actual_computation_or_reasoning"] = f"uncomputable inputs: {e}"
        base["limitations"] = "check proposal missing required fields"
        return base


def _pass_record(base: dict, operation: str, expected: Any, recomputed: Any,
                 ok: bool, detail: str) -> dict[str, Any]:
    base["status"] = "PASSED" if ok else "DISCREPANCY"
    base["actual_computation_or_reasoning"] = (
        f"{operation}: expected={expected} recomputed={recomputed}. {detail}")
    return base


def _need(value: Any, what: str) -> Any:
    if value is None or value == "" or value == []:
        raise ValueError(f"missing {what}")
    return value


def _op_percentage_improvement(base, check, values, direction, abs_tol, rel_tol):
    baseline = _need(values.get("baseline"), "values.baseline")
    proposed = _need(values.get("proposed"), "values.proposed")
    expected = _need(check.get("expected_value"), "expected_value")
    if direction == "lower":
        recomputed = (baseline - proposed) / baseline * 100.0
    else:
        recomputed = (proposed - baseline) / baseline * 100.0
    return _pass_record(base, f"percentage_improvement({direction}-is-better)",
                        expected, round(recomputed, 6),
                        _within(expected, recomputed, abs_tol, rel_tol),
                        f"baseline={baseline} proposed={proposed}")


def _op_error_reduction(base, check, values, direction, abs_tol, rel_tol):
    before = _need(values.get("before"), "values.before")
    after = _need(values.get("after"), "values.after")
    expected = _need(check.get("expected_value"), "expected_value (relative %)")
    absolute = before - after
    recomputed = absolute / before * 100.0
    return _pass_record(base, "error_reduction",
                        expected, round(recomputed, 6),
                        _within(expected, recomputed, abs_tol, rel_tol),
                        f"absolute={absolute}")


def _op_best_method(base, check, values, direction, abs_tol, rel_tol):
    if not values:
        raise ValueError("empty values")
    expected = _need(check.get("expected_label"), "expected_label")
    pick = min(values, key=values.get) if direction == "lower" else max(values, key=values.get)
    return _pass_record(base, f"best_method({direction}-is-better)",
                        expected, pick, pick == expected, f"candidates={values}")


def _op_ranking(base, check, values, direction, abs_tol, rel_tol):
    if not values:
        raise ValueError("empty values")
    expected = _need(check.get("expected_order"), "expected_order")
    reverse = direction != "lower"
    recomputed = sorted(values, key=values.get, reverse=reverse)  # type: ignore[arg-type]
    return _pass_record(base, f"ranking({direction}-is-better)",
                        expected, recomputed, recomputed == expected, f"values={values}")


def _op_aggregate(base, check, values, direction, abs_tol, rel_tol):
    if not values:
        raise ValueError("empty values")
    expected = _need(check.get("expected_value"), "expected_value")
    kind = (check.get("op_detail") or "mean").lower()
    if kind not in ("mean", "sum"):
        raise ValueError("op_detail must be mean/sum")
    vals = list(values.values())
    recomputed = sum(vals) / len(vals) if kind == "mean" else sum(vals)
    return _pass_record(base, f"aggregate({kind})",
                        expected, round(recomputed, 6),
                        _within(expected, recomputed, abs_tol, rel_tol), f"values={values}")


def _op_ratio(base, check, values, direction, abs_tol, rel_tol):
    a = _need(values.get("a"), "values.a")
    b = _need(values.get("b"), "values.b")
    expected = _need(check.get("expected_value"), "expected_value")
    recomputed = a / b
    return _pass_record(base, "ratio(a/b)",
                        expected, round(recomputed, 6),
                        _within(expected, recomputed, abs_tol, rel_tol), "")


def _op_difference(base, check, values, direction, abs_tol, rel_tol):
    a = _need(values.get("a"), "values.a")
    b = _need(values.get("b"), "values.b")
    expected = _need(check.get("expected_value"), "expected_value")
    recomputed = a - b
    return _pass_record(base, "difference(a-b)",
                        expected, round(recomputed, 6),
                        _within(expected, recomputed, abs_tol, rel_tol), "")


def _op_unit_check(base, check, values, direction, abs_tol, rel_tol):
    expected = _need(check.get("expected_label"), "expected_label (unit)")
    actual = _need(check.get("op_detail"), "op_detail (unit)")
    ok = expected.strip().lower() == actual.strip().lower()
    if expected.lower() not in (check.get("source_snippet", "") or "").lower():
        base["status"] = "NOT_EVALUATED"
        base["actual_computation_or_reasoning"] = "unit not found in source context"
        base["limitations"] = "unit untraceable"
        return base
    return _pass_record(base, "unit_check", expected, actual, ok, "")


_EXECUTORS = {
    "percentage_improvement": _op_percentage_improvement,
    "error_reduction": _op_error_reduction,
    "best_method": _op_best_method,
    "ranking": _op_ranking,
    "aggregate": _op_aggregate,
    "ratio": _op_ratio,
    "difference": _op_difference,
    "unit_check": _op_unit_check,
}


def compute_overall_assessment(
    checks: list[dict[str, Any]],
    methodology: list[dict[str, Any]],
    cross_paper: list[dict[str, Any]],
    gaps: list[str],
) -> tuple[str, str]:
    """Deterministic overall verdict from section outcomes.

    - discrepancies identified: any DISCREPANCY check or OBSERVED_PROBLEM
    - substantiated within audited evidence: >=1 PASSED, nothing negative
    - partially substantiated: >=1 PASSED or direct corroboration + caveats
    - insufficient evidence: nothing was evaluable (gaps dominate)
    - inconclusive on audited evidence: otherwise
    """
    statuses = [c.get("status", "") for c in checks]
    sevs = [m.get("severity", "") for m in methodology]
    comps = [c.get("comparability", "") for c in cross_paper]
    n_eval = sum(1 for s in statuses if s in ("PASSED", "DISCREPANCY"))
    if any(s == "DISCREPANCY" for s in statuses) or "OBSERVED_PROBLEM" in sevs:
        return ("discrepancies identified",
                "at least one recomputation disagrees or an inconsistency is established")
    if n_eval == 0 and not methodology and not cross_paper:
        return ("insufficient evidence",
                "no checks were evaluable and no findings were produced; "
                f"evidence gaps: {len(gaps)}")
    n_pass = sum(1 for s in statuses if s == "PASSED")
    caveats = (
        any(s in ("POTENTIAL_CONCERN", "INCONCLUSIVE", "NOT_EVALUATED") for s in statuses)
        or "POTENTIAL_CONCERN" in sevs
        or "MISSING_INFORMATION" in sevs
    )
    direct = "DIRECTLY_COMPARABLE" in comps
    if n_pass > 0 and not caveats and not sevs:
        return ("substantiated within audited evidence",
                f"{n_pass} check(s) passed with no negative findings")
    if n_pass > 0 or direct:
        return ("partially substantiated",
                "positive evidence exists alongside caveats or unevaluated checks")
    return ("inconclusive on audited evidence",
            "evidence neither substantiates nor contradicts the claim")


# ---------------------------------------------------------------------------
# Runtime context + node helpers
# ---------------------------------------------------------------------------


@dataclass
class AuditContext:
    project_dir: Path
    analysis: AnalysisResult
    paper: PaperEntry
    claim_id: str
    pdf_path: Optional[Path]
    spark: Any  # SparkOpenClient, or a test double with complete_json()
    openalex: Any  # OpenAlexClient, or FakeOpenAlex with search()
    external_enabled: bool = False


def _actx(config: RunnableConfig) -> AuditContext:
    return config["configurable"]["ctx"]  # type: ignore[index]


def _afail(project_dir: Path, thread_id: str, error: str) -> dict[str, Any]:
    record_transition(project_dir, thread_id, "failed", error[:300])
    try:
        record = load_run_record(project_dir, thread_id)
        record.status = "failed"
        record.final_status = "failed"
        record.error = error
        save_run_record(project_dir, record)
    except FileNotFoundError:
        pass
    return {"status": "failed", "run_error": error}


def _arecord_spark(
    project_dir: Path, thread_id: str, state: AuditState, tel: TelemetryRecord
) -> dict[str, Any]:
    record_telemetry(project_dir, thread_id, tel)
    return {"telemetry": [*state.get("telemetry", []), tel.model_dump()]}


def _aspark_call(
    ctx: AuditContext, state: AuditState, messages: list[dict[str, Any]],
    schema: Any, node: str,
) -> tuple[Any, dict[str, Any], Optional[str]]:
    try:
        parsed, tel = ctx.spark.complete_json(
            messages, schema, node=node, thread_id=state["thread_id"])
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return None, {}, str(e)
    except Exception as e:  # noqa: BLE001 - fail closed
        return None, {}, f"{node} failed: {e}"
    update = _arecord_spark(ctx.project_dir, state["thread_id"], state, tel)
    return parsed, update, None


def _project_findings_brief(project: Any, exclude_paper: str) -> str:
    lines = []
    for f in project.findings:
        if f.paper_id == exclude_paper:
            continue
        lines.append(f"- [{f.id}] ({f.paper_id}): {f.statement} [evidence: {f.evidence}]")
    return "\n".join(lines) if lines else "(no findings from other papers yet)"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def a_load_claim(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    from project import load_project, summarize_analysis

    ctx = _actx(config)
    project = load_project(ctx.project_dir)
    target = next((o for o in ctx.analysis.objects
                   if o.id == ctx.claim_id and o.type == "claim"), None)
    if target is None:
        return _afail(ctx.project_dir, state["thread_id"],
                      f"claim '{ctx.claim_id}' not found (or not a claim) in this paper's analysis")
    refs = [ctx.claim_id, *(target.related_objects or [])]
    statement = f"{target.title}: {target.what_it_is} {target.claim_supported}".strip()
    record_transition(ctx.project_dir, state["thread_id"], "load_claim_and_project", ctx.claim_id)
    return {
        "research_question": project.research_question,
        "claim_statement": statement,
        "claim_evidence": target.evidence or "",
        "object_refs": refs,
        "paper_brief": summarize_analysis(ctx.analysis),
        "external_enabled": ctx.external_enabled,
        "status": "running",
    }


def a_classify(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    messages = [
        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
        {"role": "user", "content": build_classify_text(
            state["claim_statement"], state["claim_evidence"], state["paper_brief"])},
    ]
    result, update, error = _aspark_call(ctx, state, messages, ClaimClassification,
                                         "classify_claim")
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert result is not None
    record_transition(ctx.project_dir, state["thread_id"], "classify_claim",
                      ",".join(result.types))
    return {**update, "claim_types": list(result.types),
            "classification_rationale": result.rationale}


def a_collect_evidence(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    from project import load_project

    ctx = _actx(config)
    project = load_project(ctx.project_dir)
    try:
        bundle = collect_evidence_bundle(
            ctx.analysis, ctx.claim_id, ctx.pdf_path,
            _project_findings_brief(project, state["paper_id"]),
        )
    except ValueError as e:
        return _afail(ctx.project_dir, state["thread_id"], str(e))
    record_transition(
        ctx.project_dir, state["thread_id"], "collect_internal_evidence",
        f"{len(bundle['objects'])} objects, {len(bundle['page_texts'])} pages, "
        f"{len(bundle['gaps'])} gaps")
    return {"evidence_bundle": bundle}


def a_plan_checks(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": build_plan_text(
            state.get("claim_types", []), state["claim_statement"],
            json.dumps(state.get("evidence_bundle", {})))},
    ]
    plan, update, error = _aspark_call(ctx, state, messages, AuditPlan, "plan_audit_checks")
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert plan is not None
    if len(plan.suggested_queries) > MAX_QUERIES:
        plan.suggested_queries = plan.suggested_queries[:MAX_QUERIES]
    record_transition(
        ctx.project_dir, state["thread_id"], "plan_audit_checks",
        f"{len(plan.numerical_checks)} numerical checks, "
        f"external={'yes' if plan.needs_external else 'no'}")
    return {**update, "audit_plan": plan.model_dump()}


def a_execute_checks(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    plan = state.get("audit_plan") or {}
    proposed = plan.get("numerical_checks", [])
    results = []
    for i, c in enumerate(proposed):
        check = dict(c)
        # The planner often leaves check_id empty; assign stable ids so
        # records (and UI keys) stay unique and traceable.
        if not check.get("check_id"):
            check["check_id"] = f"check-{i + 1}"
        results.append(execute_numerical_check(check))
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    record_transition(ctx.project_dir, state["thread_id"], "execute_numerical_checks",
                      f"{len(results)} checks: {counts or 'none proposed'}")
    return {"numerical_results": results}


def a_methodology(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    plan = state.get("audit_plan") or {}
    messages = [
        {"role": "system", "content": METHODOLOGY_SYSTEM_PROMPT},
        {"role": "user", "content": build_methodology_text(
            state["claim_statement"], plan.get("methodology_focus", []),
            json.dumps(state.get("evidence_bundle", {})))},
    ]
    result, update, error = _aspark_call(ctx, state, messages, MethodologyAssessment,
                                         "methodology_assessment")
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert result is not None
    findings = [f.model_dump() for f in result.findings]
    record_transition(ctx.project_dir, state["thread_id"], "methodology_assessment",
                      f"{len(findings)} findings")
    return {**update, "methodology_findings": findings}


def _validate_cross_paper(state: AuditState, ctx: AuditContext,
                          comparisons: list[dict[str, Any]]) -> list[str]:
    from project import load_project

    project = load_project(ctx.project_dir)
    finding_ids = {f.id for f in project.findings if f.paper_id != state["paper_id"]}
    paper_ids = {p.paper_id for p in project.papers}
    errors = []
    for i, c in enumerate(comparisons):
        if c.get("finding_ref") not in finding_ids:
            errors.append(f"comparison[{i}]: unknown finding '{c.get('finding_ref')}'")
        if c.get("paper_id") not in paper_ids:
            errors.append(f"comparison[{i}]: unknown paper '{c.get('paper_id')}'")
    return errors


def a_compare_project(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    bundle = state.get("evidence_bundle", {})
    plan = state.get("audit_plan") or {}
    messages = [
        {"role": "system", "content": CROSS_PAPER_SYSTEM_PROMPT},
        {"role": "user", "content": build_cross_paper_text(
            state["claim_statement"], plan.get("cross_paper_targets", []),
            bundle.get("project_findings_brief", ""))},
    ]
    result, update, error = _aspark_call(ctx, state, messages, CrossPaperAssessment,
                                         "compare_project_evidence")
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert result is not None
    comparisons = [c.model_dump() for c in result.comparisons]
    errors = _validate_cross_paper(state, ctx, comparisons)
    if errors:
        # Fabricated refs invalidate the section; record and continue with
        # the valid subset rather than laundering invented relationships.
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.validation_failures.extend(errors)
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        from project import load_project

        project = load_project(ctx.project_dir)
        finding_ids = {f.id for f in project.findings if f.paper_id != state["paper_id"]}
        paper_ids = {p.paper_id for p in project.papers}
        comparisons = [c for c in comparisons
                       if c.get("finding_ref") in finding_ids and c.get("paper_id") in paper_ids]
        record_transition(ctx.project_dir, state["thread_id"], "compare_project_evidence",
                          f"{len(errors)} invalid refs dropped")
    else:
        record_transition(ctx.project_dir, state["thread_id"], "compare_project_evidence",
                          f"{len(comparisons)} comparisons")
    return {**update, "cross_paper": comparisons}


def a_literature_search(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    """Bounded OpenAlex retrieval. Fail-soft: errors become notes, and the
    audit continues on internal evidence. Cached on disk so resume/retry
    never repeats completed searches."""
    ctx = _actx(config)
    plan = state.get("audit_plan") or {}
    queries = [q for q in plan.get("suggested_queries", []) if q][:MAX_QUERIES]
    collected: list[dict[str, Any]] = []
    notes: list[str] = []
    for q in queries:
        if len(collected) >= MAX_RECORDS:
            notes.append("record budget exhausted; stopped")
            break
        try:
            records = ctx.openalex.search(q, per_page=min(5, MAX_RECORDS - len(collected)))
        except OpenAlexError as e:
            notes.append(f"query failed gracefully: {q[:80]} ({e})")
            continue
        except Exception as e:  # noqa: BLE001 - any search failure is fail-soft
            notes.append(f"query failed gracefully: {q[:80]} ({type(e).__name__})")
            continue
        for r in records:
            collected.append(r.to_dict() if hasattr(r, "to_dict") else dict(r))
            if len(collected) >= MAX_RECORDS:
                break
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        for q in queries:
            record.decisions.append(f"openalex query: {q[:160]}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "bounded_literature_search",
                      f"{len(queries)} queries, {len(collected)} records")
    return {"external_queries": queries, "external_sources": collected,
            "external_notes": notes}


def a_assess_sources(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    """Deterministic source triage (no extra model call): keep at most 3
    records with abstracts, always labeled ABSTRACT-LEVEL EVIDENCE."""
    ctx = _actx(config)
    candidates = state.get("external_sources", [])
    with_abstracts = [c for c in candidates if (c.get("abstract") or "").strip()]
    with_abstracts.sort(key=lambda c: (-int(c.get("cited_by") or 0), str(c.get("year") or "")))
    selected = []
    for c in with_abstracts[:MAX_SELECTED]:
        selected.append({
            **c,
            "evidence_level": "ABSTRACT_ONLY",
            "evidence_label": "ABSTRACT-LEVEL EVIDENCE",
        })
    notes = list(state.get("external_notes", []))
    if len(candidates) > len(selected):
        notes.append(f"{len(candidates) - len(selected)} candidate(s) set aside by budget")
    if candidates and not selected:
        notes.append("no candidates had usable abstracts")
    record_transition(ctx.project_dir, state["thread_id"], "assess_sources",
                      f"{len(selected)} selected (abstract-level)")
    return {"external_sources": selected, "external_notes": notes}


def _audit_snapshot(state: AuditState) -> dict[str, Any]:
    return {
        "claim": state.get("claim_statement", ""),
        "claim_types": state.get("claim_types", []),
        "numerical_results": state.get("numerical_results", []),
        "methodology_findings": state.get("methodology_findings", []),
        "cross_paper": state.get("cross_paper", []),
        "external_sources": [
            {"title": s.get("title", ""), "doi": s.get("doi", ""),
             "year": s.get("year"), "evidence_label": s.get("evidence_label", "")}
            for s in state.get("external_sources", [])
        ],
        "evidence_gaps": (state.get("evidence_bundle", {}) or {}).get("gaps", []),
    }


def a_critic(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    node = "scientific_critic" if state.get("revision_count", 0) == 0 else "scientific_critic_rereview"
    messages = [
        {"role": "system", "content": AUDIT_CRITIC_SYSTEM_PROMPT},
        {"role": "user", "content": build_critic_text(json.dumps(_audit_snapshot(state)))},
    ]
    verdict, update, error = _aspark_call(ctx, state, messages, CriticVerdict, node)
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert verdict is not None
    record_transition(ctx.project_dir, state["thread_id"], node, f"verdict={verdict.verdict}")
    return {**update, "critic_assessment": verdict.model_dump()}


def a_decision_gate(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    verdict = (state.get("critic_assessment") or {}).get("verdict", "")
    if verdict == "SUPPORTED":
        decision = "finalize"
    elif verdict == "REVISION_REQUIRED" and state.get("revision_count", 0) < MAX_AUDIT_REVISIONS:
        decision = "revise"
    else:
        decision = "human_review"
    record_transition(ctx.project_dir, state["thread_id"], "audit_decision_gate",
                      f"verdict={verdict} -> {decision}")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.decisions.append(f"audit decision: {verdict} -> {decision}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    return {"decision": decision}


def a_revise(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    messages = [
        {"role": "system", "content": AUDIT_REVISE_SYSTEM_PROMPT},
        {"role": "user", "content": build_revision_text(
            json.dumps(_audit_snapshot(state)),
            json.dumps(state.get("critic_assessment") or {}),
            json.dumps(state.get("evidence_bundle", {})))},
    ]
    result, update, error = _aspark_call(ctx, state, messages, AuditRevision,
                                         "bounded_revision")
    if error:
        return _afail(ctx.project_dir, state["thread_id"], error)
    assert result is not None
    methodology = [f.model_dump() for f in result.corrected_methodology]
    cross_paper = [c.model_dump() for c in result.corrected_cross_paper]
    errors = _validate_cross_paper(state, ctx, cross_paper)
    if errors:
        from project import load_project

        project = load_project(ctx.project_dir)
        finding_ids = {f.id for f in project.findings if f.paper_id != state["paper_id"]}
        paper_ids = {p.paper_id for p in project.papers}
        cross_paper = [c for c in cross_paper
                       if c.get("finding_ref") in finding_ids and c.get("paper_id") in paper_ids]
    record_transition(ctx.project_dir, state["thread_id"], "bounded_revision",
                      result.notes[:150])
    return {**update, "methodology_findings": methodology, "cross_paper": cross_paper,
            "revision_count": state.get("revision_count", 0) + 1}


def build_audit_review_request(state: AuditState) -> HumanReviewRequest:
    critic = state.get("critic_assessment") or {}
    draft = critic.get("human_question") or {}
    checks = state.get("numerical_results", [])
    summary = "; ".join(f"{c.get('check_id')}:{c.get('status')}" for c in checks) or "no checks"
    return HumanReviewRequest(
        kind="audit",
        question=draft.get("question") or (
            "The audit critic flagged consequential ambiguity. How should it be resolved?"),
        why_stopped=draft.get("critic_concern") or critic.get("summary", "") or
        "Critic verdict requires researcher judgment.",
        investigator_position=draft.get("investigator_position") or
        f"Audit position: checks [{summary}]",
        critic_concern=draft.get("critic_concern") or critic.get("summary", ""),
        evidence_a=(state.get("claim_statement", "") + "\nTypes: " +
                    ",".join(state.get("claim_types", [])))[:2000],
        evidence_b=json.dumps(_audit_snapshot(state))[:2000],
        finding_refs=[],
        object_refs=state.get("object_refs", []),
        options=draft.get("options") or ["Accept audit as-is", "Discard audit"],
        allow_free_text=True,
    )


def a_prepare_review(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    request = build_audit_review_request(state)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.human_review_request = request.model_dump()
        record.decisions.append(f"human review requested (audit): {request.question[:200]}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "human_review_requested", "audit")
    return {"needs_human_review": True,
            "human_review_request": request.model_dump(),
            "status": "awaiting_human"}


def a_human_review(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    request = state.get("human_review_request") or {}
    resume_value = interrupt(request)
    response = HumanReviewResponse.model_validate(resume_value)
    if not response.decided_at:
        response.decided_at = utc_now_iso()
    ctx = _actx(config)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.human_review_response = response.model_dump()
        record.decisions.append(f"human decision: {response.choice[:200]}")
        record.status = "running"
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "human_review_resumed",
                      response.choice[:200])
    return {"human_review_response": response.model_dump(), "status": "running"}


def a_apply_decision(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    response = state.get("human_review_response") or {}
    choice = str(response.get("choice", ""))
    lowered = choice.lower()
    if "discard" in lowered or "abort" in lowered:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.status = "discarded"
            record.final_status = "discarded"
            record.decisions.append(f"researcher discarded the audit: {choice[:200]}")
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "apply_audit_decision",
                          "discarded by researcher")
        return {"status": "discarded", "decision": "abort"}
    record_transition(ctx.project_dir, state["thread_id"], "apply_audit_decision",
                      f"researcher accepted: {choice[:200]}")
    return {"decision": "finalize"}


def a_finalize(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    record_transition(ctx.project_dir, state["thread_id"], "finalize", "audit accepted")
    return {"decision": "report"}


def a_evidence_report(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _actx(config)
    bundle = state.get("evidence_bundle", {}) or {}
    checks = state.get("numerical_results", [])
    methodology = state.get("methodology_findings", [])
    cross_paper = state.get("cross_paper", [])
    gaps = bundle.get("gaps", [])
    overall, rationale = compute_overall_assessment(checks, methodology, cross_paper, gaps)
    unresolved = [
        *(m["finding"][:300] for m in methodology
          if m.get("severity") in ("POTENTIAL_CONCERN", "MISSING_INFORMATION")),
        *((state.get("critic_assessment") or {}).get("required_fixes", []) or []),
    ][:12]
    plan = state.get("audit_plan") or {}
    report = {
        "audit_id": "",
        "thread_id": state["thread_id"],
        "project_id": state["project_id"],
        "paper_id": state["paper_id"],
        "claim_id": state["claim_id"],
        "claim_text": state.get("claim_statement", ""),
        "claim_type": state.get("claim_types", []),
        "audit_scope": plan.get("scope_notes", ""),
        "checks": checks,
        "methodology_findings": methodology,
        "cross_paper_findings": cross_paper,
        "external_sources": state.get("external_sources", []),
        "external_queries": state.get("external_queries", []),
        "external_notes": state.get("external_notes", []),
        "unresolved_questions": unresolved,
        "overall_assessment": overall,
        "assessment_rationale": rationale,
        "limitations": plan.get("limitations", []) + gaps,
        "provenance": {
            "claim_ref": f"{state['paper_id']}:{state['claim_id']}",
            "object_refs": state.get("object_refs", []),
            "evidence_pages": [p.get("page") for p in bundle.get("page_texts", [])],
            "evidence_objects": [o.get("id") for o in bundle.get("objects", [])],
            "external_enabled": state.get("external_enabled", False),
            "revisions": state.get("revision_count", 0),
            "human_reviewed": state.get("human_review_response") is not None,
        },
        "model_telemetry": state.get("telemetry", []),
        "created_at": utc_now_iso(),
    }
    tdir = audit_thread_dir(ctx.project_dir, state["thread_id"])
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (tdir / "evidence_bundle.json").write_text(json.dumps(bundle, indent=2)[:200000],
                                               encoding="utf-8")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.decisions.append(f"audit report: overall={overall}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "evidence_report",
                      f"overall={overall}")
    return {"audit_report": report, "overall_assessment": overall,
            "unresolved_questions": unresolved}


def _next_audit_id(taken: set[str]) -> str:
    i = 1
    while f"a{i}" in taken:
        i += 1
    taken.add(f"a{i}")
    return f"a{i}"


def a_attach(state: AuditState, config: RunnableConfig) -> dict[str, Any]:
    from project import ScientificAudit

    ctx = _actx(config)
    report = state.get("audit_report")
    if report is None:
        return _afail(ctx.project_dir, state["thread_id"], "no audit report to attach")
    from project import load_project

    project = load_project(ctx.project_dir)
    if any(a.thread_id == state["thread_id"] for a in project.audits):
        record_transition(ctx.project_dir, state["thread_id"], "attach_to_project",
                          "already attached; skipped duplicate")
    else:
        taken = {a.audit_id for a in project.audits}
        report["audit_id"] = _next_audit_id(taken)
        # M7: retain the originating question version for provenance.
        report.setdefault("question_version", project.question_version or 1)
        project.audits.append(ScientificAudit.model_validate(report))
        save_project(ctx.project_dir, project)
        tdir = audit_thread_dir(ctx.project_dir, state["thread_id"])
        (tdir / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        record_transition(ctx.project_dir, state["thread_id"], "attach_to_project",
                          f"attached {report['audit_id']} overall={report['overall_assessment']}")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.status = "completed"
        record.final_status = "completed"
        record.decisions.append(f"audit complete: {report.get('overall_assessment')}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    return {"status": "completed"}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _pass_or_end(next_node: str):
    def _route(state: AuditState) -> str:
        return END if state.get("status") == "failed" else next_node

    return _route


def _route_after_gate(state: AuditState) -> str:
    decision = state.get("decision", "")
    if decision == "finalize":
        return "finalize"
    if decision == "revise":
        return "bounded_revision"
    return "prepare_review"


def _route_after_external_gate(state: AuditState) -> str:
    plan = state.get("audit_plan") or {}
    if state.get("external_enabled") and plan.get("needs_external"):
        return "bounded_literature_search"
    return "scientific_critic"


def _route_after_apply(state: AuditState) -> str:
    return END if state.get("decision") == "abort" else "finalize"


def build_audit_graph(checkpointer: SqliteSaver):
    graph = StateGraph(AuditState)
    graph.add_node("load_claim_and_project", a_load_claim)
    graph.add_node("classify_claim", a_classify)
    graph.add_node("collect_internal_evidence", a_collect_evidence)
    graph.add_node("plan_audit_checks", a_plan_checks)
    graph.add_node("execute_numerical_checks", a_execute_checks)
    graph.add_node("methodology_assessment", a_methodology)
    graph.add_node("compare_project_evidence", a_compare_project)
    graph.add_node("bounded_literature_search", a_literature_search)
    graph.add_node("assess_sources", a_assess_sources)
    graph.add_node("scientific_critic", a_critic)
    graph.add_node("audit_decision_gate", a_decision_gate)
    graph.add_node("bounded_revision", a_revise)
    graph.add_node("prepare_review", a_prepare_review)
    graph.add_node("human_review", a_human_review)
    graph.add_node("apply_audit_decision", a_apply_decision)
    graph.add_node("finalize", a_finalize)
    graph.add_node("evidence_report", a_evidence_report)
    graph.add_node("attach_to_project", a_attach)

    graph.add_edge(START, "load_claim_and_project")
    graph.add_conditional_edges("load_claim_and_project", _pass_or_end("classify_claim"),
                               {"classify_claim": "classify_claim", END: END})
    graph.add_conditional_edges("classify_claim", _pass_or_end("collect_internal_evidence"),
                               {"collect_internal_evidence": "collect_internal_evidence", END: END})
    graph.add_conditional_edges("collect_internal_evidence", _pass_or_end("plan_audit_checks"),
                               {"plan_audit_checks": "plan_audit_checks", END: END})
    graph.add_conditional_edges("plan_audit_checks", _pass_or_end("execute_numerical_checks"),
                               {"execute_numerical_checks": "execute_numerical_checks", END: END})
    graph.add_conditional_edges("execute_numerical_checks",
                               _pass_or_end("methodology_assessment"),
                               {"methodology_assessment": "methodology_assessment", END: END})
    graph.add_conditional_edges("methodology_assessment",
                               _pass_or_end("compare_project_evidence"),
                               {"compare_project_evidence": "compare_project_evidence", END: END})
    # a_compare_project never fails the run (invalid refs are dropped);
    # the external gate is a deterministic conditional edge.
    graph.add_conditional_edges("compare_project_evidence", _route_after_external_gate, {
        "bounded_literature_search": "bounded_literature_search",
        "scientific_critic": "scientific_critic",
    })
    graph.add_edge("bounded_literature_search", "assess_sources")
    graph.add_edge("assess_sources", "scientific_critic")
    graph.add_edge("scientific_critic", "audit_decision_gate")
    graph.add_conditional_edges("audit_decision_gate", _route_after_gate, {
        "finalize": "finalize",
        "bounded_revision": "bounded_revision",
        "prepare_review": "prepare_review",
    })
    graph.add_conditional_edges("bounded_revision", _pass_or_end("scientific_critic"),
                               {"scientific_critic": "scientific_critic", END: END})
    graph.add_edge("prepare_review", "human_review")
    graph.add_edge("human_review", "apply_audit_decision")
    graph.add_conditional_edges("apply_audit_decision", _route_after_apply,
                               {"finalize": "finalize", END: END})
    graph.add_edge("finalize", "evidence_report")
    graph.add_edge("evidence_report", "attach_to_project")
    graph.add_edge("attach_to_project", END)
    return graph.compile(checkpointer=checkpointer)
