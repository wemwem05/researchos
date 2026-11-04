#!/usr/bin/env python3
"""ResearchOS Milestone 8: autonomous literature discovery graph.

Given a project's research question (plus optional focus), this LangGraph
workflow plans a bounded OpenAlex search strategy, retrieves and deduplicates
candidates, evaluates scientific relevance with Spark Open, reflects on
coverage (at most one additional focused round), submits recommendations to a
scientific critic, and persists ranked, explainable recommendations.

Topology:

  load_context -> plan_search (Spark) -> retrieve_candidates (OpenAlex)
  -> normalize_and_deduplicate (Python) -> shortlist (Python)
  -> evaluate_relevance (Spark) -> reflect (Spark)
  -> [optional_refined_search: retrieve -> dedupe -> shortlist -> evaluate]
  -> discovery_critic (Spark) -> discovery_decision_gate
  -> finalize | bounded_revision (max 1, Spark) -> discovery_critic
  -> prepare_recommendations (Python) -> persist_recommendations -> END

Researcher approval happens AFTER the graph completes (plain API on the
persisted run), so this graph has no interrupt and no resume path. Reuses
the M5/M6/M7 infrastructure: same SQLite checkpointer file, RunRecord
(kind="discover"), telemetry log, atomic project saves, and the M7 job
dispatcher for durability.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agent_graph import (
    RunRecord,
    agent_dir,
    load_run_record,
    record_telemetry,
    record_transition,
    save_run_record,
)
from discovery_models import (
    DiscoveryBudget,
    DiscoveryCandidate,
    DiscoveryFailure,
    DiscoveryQueryRecord,
    DiscoveryRun,
    candidate_from_record,
    clamp_novelty_wording,
    dedupe_records,
)
from openalex import OpenAlexError
from project import load_project, save_project
from spark_open import SparkOpenError, utc_now_iso

SCHEMA_VERSION = "m8-1"

# One bounded discovery revision, then accept-with-limitations (mirrors M5).
MAX_DISCOVERY_REVISIONS = 1

# Prompt/context budgets (keeps checkpoints small and prompts focused).
MAX_CONTEXT_CHARS = 6000
MAX_ABSTRACT_CHARS_EVAL = 1500
MAX_ALREADY_IN_PROJECT_SHOWN = 5

STRATEGIES = ("A", "B", "C", "D", "E", "F")


# ---------------------------------------------------------------------------
# Spark Open schemas
# ---------------------------------------------------------------------------


class PlannedQuery(BaseModel):
    query: str = Field(min_length=1)
    strategy: str = ""
    rationale: str = ""


class PlannedExpansion(BaseModel):
    work_openalex_id: str = Field(min_length=1)
    direction: Literal["references", "citations"] = "references"
    rationale: str = ""


class SearchPlan(BaseModel):
    strategy_notes: str = ""
    queries: list[PlannedQuery] = Field(default_factory=list)


class CandidateEval(BaseModel):
    candidate_id: str = Field(min_length=1)
    verdict: Literal["highly_relevant", "relevant", "tangential", "off_topic"] = (
        "tangential"
    )
    why_relevant: str = ""
    may_contribute: str = ""
    may_overlap_with: str = ""
    still_unknown: str = ""
    why_read_next: str = ""
    novelty_overlap: bool = False
    novelty_note: str = ""


class EvaluationResult(BaseModel):
    evaluations: list[CandidateEval] = Field(default_factory=list)


class ReflectionResult(BaseModel):
    decision: Literal["stop", "continue"] = "stop"
    explanation: str = ""
    round2_queries: list[PlannedQuery] = Field(default_factory=list)
    expansions: list[PlannedExpansion] = Field(default_factory=list)
    coverage_notes: str = ""


class CriticConcern(BaseModel):
    candidate_id: str = ""
    issue: str = ""
    severity: Literal["blocking", "major", "minor"] = "minor"


class DiscoveryCriticVerdict(BaseModel):
    verdict: Literal["SUPPORTED", "NEEDS_REVISION"] = "SUPPORTED"
    summary: str = ""
    concerns: list[CriticConcern] = Field(default_factory=list)
    drop_candidate_ids: list[str] = Field(default_factory=list)
    required_fixes: list[str] = Field(default_factory=list)


class DiscoveryRevision(BaseModel):
    corrected_evaluations: list[CandidateEval] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class DiscoveryState(TypedDict):
    project_id: str
    thread_id: str
    focus: str
    question: str
    question_version: int
    start_question_version: int
    context_text: str
    budget: dict[str, Any]
    round: int
    strategy_notes: str
    counts: dict[str, int]
    duplicates_log: list[dict[str, Any]]
    planned: list[dict[str, Any]]
    planned_expansions: list[dict[str, Any]]
    queries_done: list[dict[str, Any]]
    retrieved: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    shortlist: list[str]
    evaluated: dict[str, dict[str, Any]]
    reflection: dict[str, Any]
    critic: dict[str, Any]
    revision_used: bool
    recommendations: list[str]
    status: str
    error: str
    limitations: list[str]
    usage: dict[str, int]


@dataclass
class DiscoveryContext:
    project_dir: Path
    spark: Any
    openalex: Any
    focus: str = ""
    budget: DiscoveryBudget = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.budget is None:
            self.budget = DiscoveryBudget()


def _dctx(config: RunnableConfig) -> DiscoveryContext:
    ctx = config["configurable"]["ctx"]
    assert isinstance(ctx, DiscoveryContext)
    return ctx


def initial_discovery_state(
    project_id: str, thread_id: str, focus: str = "",
    budget: Optional[DiscoveryBudget] = None,
) -> DiscoveryState:
    return {
        "project_id": project_id,
        "thread_id": thread_id,
        "focus": focus or "",
        "question": "",
        "question_version": 1,
        "start_question_version": 1,
        "context_text": "",
        "budget": (budget or DiscoveryBudget()).model_dump(),
        "round": 1,
        "strategy_notes": "",
        "counts": {},
        "duplicates_log": [],
        "planned": [],
        "planned_expansions": [],
        "queries_done": [],
        "retrieved": [],
        "candidates": [],
        "shortlist": [],
        "evaluated": {},
        "reflection": {},
        "critic": {},
        "revision_used": False,
        "recommendations": [],
        "status": "running",
        "error": "",
        "limitations": [],
        "usage": {"spark_calls": 0, "provider_calls": 0, "records": 0,
                  "expansions": 0, "deep_inspections": 0, "tokens_total": 0},
    }


def new_discovery_thread_id(project_id: str) -> str:
    import uuid as _uuid

    return f"{project_id}--discover--{_uuid.uuid4().hex[:8]}"


def discovery_dir(project_dir: Path) -> Path:
    d = agent_dir(project_dir) / "discovery"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Spark Open prompts
# ---------------------------------------------------------------------------


PLAN_SYSTEM_PROMPT = """You are the ResearchOS literature scout. Given a research question, an optional focus, and a compact summary of a research project (papers already analyzed, findings, open questions, novelty risks, prior discovery decisions), design a bounded OpenAlex search strategy.

Search approaches (pick only those that fit; 1-3 queries total):
A. Direct topic search — papers directly answering the research question.
B. Method-focused search — papers applying the relevant mathematical or algorithmic method.
C. Related work exploration — NOT in round 1 (no retrieved works exist yet); citation expansion is planned only in round 2.
D. Alternative approaches — competing methods for the same problem.
E. Novelty-overlap investigation — papers that may already cover a proposed direction.
F. Recent developments — newer work updating the state of knowledge.

Rules: prefer specific multi-term queries over generic ones (a bare single-concept query such as "optimal transport" alone is forbidden — always add problem or method context terms); every query must name at least 2 distinctive content terms; never repeat a prior failed query listed in the context; note which project gap each query targets.

Exact JSON shape — use these key names verbatim:
{"strategy_notes": str, "queries": [{"query": str, "strategy": "A"|"B"|"C"|"D"|"E"|"F", "rationale": str}]}
Return JSON only, no commentary."""

EVAL_SYSTEM_PROMPT = """You are the ResearchOS relevance judge. Given a research question, project context, and up to 8 candidate papers (title, authors, year, venue, abstract, evidence level), assess each candidate's scientific relevance to the project.

Verdict rubric: highly_relevant — directly addresses the question or a listed open question or method; relevant — useful background, method, or comparison; tangential — weak connection; off_topic — unrelated despite keyword overlap.

For each candidate write: why_relevant, may_contribute (to this project), may_overlap_with (existing papers, methods, or directions), still_unknown (what the abstract does NOT tell us), why_read_next. Set novelty_overlap=true only when the abstract concretely suggests prior work on a proposed direction — never from shared terminology alone.

Honesty rules: ground every statement in the provided metadata or abstract; never claim full-text knowledge; never invent results, equations, or benchmarks; a title alone never justifies more than "tangential"; never state global novelty conclusions — coverage is limited to the retrieved literature.

Exact JSON shape — use these key names verbatim:
{"evaluations": [{"candidate_id": str, "verdict": "highly_relevant"|"relevant"|"tangential"|"off_topic", "why_relevant": str, "may_contribute": str, "may_overlap_with": str, "still_unknown": str, "why_read_next": str, "novelty_overlap": bool, "novelty_note": str}]}
Return JSON only, no commentary."""

REFLECT_SYSTEM_PROMPT = """You are the ResearchOS search strategist. Given the research question, the round-1 queries and what they retrieved, and the relevance verdicts so far, decide whether ONE more bounded search round is worthwhile.

Stop when: coverage of the question is adequate, the verdicts are consistently off-topic (more of the same queries will not help), or no concrete gap remains. Continue ONLY when a specific unexplored angle exists (an untested competing method, an uncovered sub-question, a suspicious novelty overlap worth one check, a promising citation trail).

A continued round may add up to 3 NEW queries (unlike round-1 queries — no near-duplicates) and up to 2 citation expansions, each naming a retrieved OpenAlex work id and a direction (references = works it cites, citations = works citing it) with a rationale. Expansion ids MUST be the OpenAlex ids shown next to each verdict below (e.g. W123); candidate labels like dc1 are NOT valid expansion ids and will be dropped.

Exact JSON shape — use these key names verbatim:
{"decision": "stop"|"continue", "explanation": str, "round2_queries": [{"query": str, "strategy": "A"|"B"|"C"|"D"|"E"|"F", "rationale": str}], "expansions": [{"work_openalex_id": str, "direction": "references"|"citations", "rationale": str}], "coverage_notes": str}
Return JSON only, no commentary."""

CRITIC_SYSTEM_PROMPT = """You are the ResearchOS recommendation critic. Given the research question, project context, and evaluated candidates WITH their source abstracts, validate each recommendation.

Check: every claim in the evaluation is grounded in the shown abstract or metadata (flag ungrounded specifics); no unsupported assumptions about full text; no exaggerated novelty (it is forbidden to claim nobody studied something — coverage is limited); no title-only overinterpretation; metadata (year, venue, authors) used consistently; each recommendation compares against the project's existing papers; all candidate ids come from the provided list (no fabricated citations).

Verdict SUPPORTED when the set is sound (minor notes allowed). Verdict NEEDS_REVISION only for fixable problems: list concerns with candidate ids and severity, drop_candidate_ids for unsalvageable entries, and required_fixes the revision must apply.

Exact JSON shape — use these key names verbatim:
{"verdict": "SUPPORTED"|"NEEDS_REVISION", "summary": str, "concerns": [{"candidate_id": str, "issue": str, "severity": "blocking"|"major"|"minor"}], "drop_candidate_ids": [str], "required_fixes": [str]}
Return JSON only, no commentary."""

REVISE_SYSTEM_PROMPT = """You are the ResearchOS recommendation reviser. Given the critic's concerns and required fixes, rewrite ONLY the flagged candidate evaluations. Keep verdicts and reasons grounded in the shown abstracts; drop nothing yourself (dropping is handled separately); preserve candidate ids exactly.

Exact JSON shape — use these key names verbatim:
{"corrected_evaluations": [{"candidate_id": str, "verdict": "highly_relevant"|"relevant"|"tangential"|"off_topic", "why_relevant": str, "may_contribute": str, "may_overlap_with": str, "still_unknown": str, "why_read_next": str, "novelty_overlap": bool, "novelty_note": str}], "notes": str}
Return JSON only, no commentary."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_spark(project_dir: Path, thread_id: str, tel: Any) -> None:
    record_telemetry(project_dir, thread_id, tel)
    try:
        record = load_run_record(project_dir, thread_id)
        record.telemetry.append(tel.model_dump() if hasattr(tel, "model_dump") else tel)
        record.updated_at = utc_now_iso()
        save_run_record(project_dir, record)
    except FileNotFoundError:
        pass


def _dspark_call(
    ctx: DiscoveryContext, state: DiscoveryState, messages: list[dict[str, Any]],
    schema: Any, node: str,
) -> tuple[Any, Optional[str]]:
    """One Spark Open call with telemetry. Returns (parsed, error)."""
    usage = dict(state.get("usage") or {})
    budget = state.get("budget") or {}
    if usage.get("spark_calls", 0) >= int(budget.get("max_spark_calls", 8)):
        return None, f"spark call budget exhausted at node {node}"
    try:
        parsed, tel = ctx.spark.complete_json(
            messages, schema, node=node, thread_id=state["thread_id"])
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - fail closed
        return None, f"{node} failed: {e}"
    _record_spark(ctx.project_dir, state["thread_id"], tel)
    usage["spark_calls"] = usage.get("spark_calls", 0) + 1
    try:
        usage["tokens_total"] = usage.get("tokens_total", 0) + int(
            getattr(tel, "total_tokens", 0) or 0)
    except (TypeError, ValueError):
        pass
    state["usage"] = usage
    return parsed, None


def _dfail(project_dir: Path, thread_id: str, error: str,
           partial: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fail closed: failed run record (+ partial persisted run when given)."""
    try:
        record = load_run_record(project_dir, thread_id)
        record.status = "failed"
        record.error = error
        record.updated_at = utc_now_iso()
        save_run_record(project_dir, record)
    except FileNotFoundError:
        pass
    try:
        record_transition(project_dir, thread_id, "failed", error[:200])
    except FileNotFoundError:
        pass
    out: dict[str, Any] = {"status": "failed", "error": error}
    if partial:
        out.update(partial)
    return out


def _pass_or_end(next_node: str):
    def route(state: DiscoveryState) -> str:
        if state.get("status") == "failed":
            return END
        return next_node

    route.__name__ = f"pass_or_end_{next_node}"
    return route


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def build_discovery_context(project: Any, focus: str) -> str:
    """Compact bounded context: question, papers, findings, gaps, history."""
    lines = [
        f"Research question (v{project.question_version or 1}): "
        f"{project.research_question}",
    ]
    if (focus or "").strip():
        lines.append(f"Discovery focus: {focus.strip()[:500]}")
    if project.papers:
        lines.append(f"Papers already analyzed ({len(project.papers)}):")
        contribs = {c.paper_id: c for c in project.contributions}
        for paper in project.papers[:12]:
            adds = ""
            if paper.paper_id in contribs:
                adds = f" — {_truncate(contribs[paper.paper_id].what_it_adds, 160)}"
            lines.append(f"- {paper.title[:160]}{adds}")
    else:
        lines.append("Papers already analyzed: none (greenfield project).")
    if project.findings:
        lines.append(f"Key findings ({len(project.findings)}):")
        for finding in project.findings[:10]:
            lines.append(
                f"- [{finding.id}] {_truncate(finding.statement, 220)} "
                f"({finding.evidence[:80]})")
    if project.open_questions:
        lines.append("Open questions:")
        for question in project.open_questions[:8]:
            lines.append(f"- [{question.id}] {_truncate(question.text, 200)}")
    if project.novelty_risks:
        lines.append("Potential novelty risks / proposed directions:")
        for risk in project.novelty_risks[:5]:
            lines.append(
                f"- [{risk.id}] {_truncate(risk.direction, 200)} "
                f"Note: {_truncate(risk.note, 200)}")
    prior: list[str] = []
    for run in getattr(project, "discoveries", []) or []:
        for cand in run.candidates or []:
            if cand.status in ("dismissed", "imported", "approved"):
                prior.append(f"{cand.status}: {cand.title[:120]}")
    if prior:
        lines.append("Prior discovery decisions (do not re-recommend dismissed):")
        for entry in prior[:20]:
            lines.append(f"- {entry}")
    text = "\n".join(lines)
    return _truncate(text, MAX_CONTEXT_CHARS)


def project_dedup_items(project: Any, project_dir: Path) -> list[dict[str, Any]]:
    """Project papers as dedup items, enriched with discovered identifiers
    recorded at import time (pipeline.json provenance)."""
    items = []
    for paper in project.papers:
        item: dict[str, Any] = {
            "paper_id": paper.paper_id,
            "title": paper.title,
            "authors": [],
            "publication_year": None,
        }
        pipe = project_dir / "papers" / paper.paper_id / "pipeline.json"
        try:
            if pipe.is_file():
                prov = json.loads(pipe.read_text()).get("discovered_from") or {}
                for key in ("doi", "openalex_id", "arxiv_id"):
                    if prov.get(key):
                        item[key] = prov[key]
        except (OSError, ValueError):
            pass
        items.append(item)
    return items


def prior_dedup_items(project: Any) -> list[tuple[dict[str, Any], str]]:
    """(candidate dict, run label) for every prior discovery candidate."""
    out = []
    for run in getattr(project, "discoveries", []) or []:
        for cand in run.candidates or []:
            out.append(({
                "title": cand.title,
                "authors": cand.authors,
                "publication_year": cand.publication_year,
                "doi": cand.doi,
                "openalex_id": cand.openalex_id,
                "arxiv_id": cand.arxiv_id,
            }, f"run {run.run_id} ({cand.status})"))
    return out


# ---------------------------------------------------------------------------
# Nodes: load, plan, retrieve
# ---------------------------------------------------------------------------


def d_load_context(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    try:
        project = load_project(ctx.project_dir)
    except Exception as e:  # noqa: BLE001 - fail closed with the reason
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"cannot load project: {e}")
    qv = project.question_version or 1
    context = build_discovery_context(project, state.get("focus", ""))
    record_transition(ctx.project_dir, state["thread_id"], "load_context",
                      f"qv{qv}, {len(project.papers)} papers, "
                      f"{len(project.findings)} findings")
    return {
        "question": project.research_question,
        "question_version": qv,
        "start_question_version": qv,
        "context_text": context,
    }


def _validate_plan_queries(queries: list[dict[str, Any]], budget: dict[str, Any],
                           seen: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Deterministic plan hygiene: caps, non-empty, strategy codes, dedup."""
    notes: list[str] = []
    clean: list[dict[str, Any]] = []
    cap = int(budget.get("max_queries_per_round", 3))
    for raw in queries or []:
        if not isinstance(raw, dict):
            continue
        query = str(raw.get("query") or "").strip()
        if not query or len(query) < 8:
            continue
        if len(query.split()) < 2:
            notes.append(f"dropped single-term query {query!r}")
            continue
        if query.lower() in seen:
            notes.append(f"dropped duplicate query {query!r}")
            continue
        strategy = str(raw.get("strategy") or "?").strip().upper()
        if strategy not in STRATEGIES:
            strategy = "?"
        clean.append({"query": query, "strategy": strategy,
                      "rationale": str(raw.get("rationale") or "")[:400]})
        seen.add(query.lower())
        if len(clean) >= cap:
            break
    return clean, notes


def d_plan_search(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    budget = state.get("budget") or {}
    messages = [
        {"role": "system", "content": PLAN_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Research context:\n{state.get('context_text', '')}\n\n"
            f"Budget: up to {budget.get('max_queries_per_round', 3)} round-1 "
            f"queries. Plan the round-1 search now.")},
    ]
    parsed, error = _dspark_call(ctx, state, messages, SearchPlan, "plan_search")
    if error or parsed is None:
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"search planning failed: {error}")
    planned, notes = _validate_plan_queries(
        [q.model_dump() for q in parsed.queries], budget, set())
    if not planned:
        return _dfail(ctx.project_dir, state["thread_id"],
                      "planner produced no usable queries")
    record_transition(ctx.project_dir, state["thread_id"], "plan_search",
                      f"{len(planned)} queries. {parsed.strategy_notes[:150]}")
    return {"planned": planned, "strategy_notes": parsed.strategy_notes[:2000],
            "limitations": [*state.get("limitations", []), *notes],
            "usage": dict(state.get("usage") or {})}


def _record_from_openalex(rec: Any, query: str, run_round: int) -> dict[str, Any]:
    d = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
    return {
        "title": d.get("title", ""),
        "authors": d.get("authors", []) or [],
        "publication_year": d.get("year"),
        "publication_date": d.get("publication_date", ""),
        "venue": d.get("venue", ""),
        "work_type": d.get("work_type", ""),
        "doi": d.get("doi", ""),
        "openalex_id": d.get("openalex_id", ""),
        "arxiv_id": d.get("arxiv_id", ""),
        "semantic_scholar_id": "",
        "abstract": d.get("abstract", ""),
        "landing_page_url": d.get("landing_page") or d.get("url", ""),
        "open_access_url": d.get("pdf_url", "") if d.get("is_oa") else "",
        "pdf_url": d.get("pdf_url", ""),
        "license": d.get("license", ""),
        "citation_count": d.get("cited_by"),
        "source_provider": "openalex",
        "retrieval_query": query,
        "retrieval_round": run_round,
    }


def d_retrieve(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    budget = state.get("budget") or {}
    run_round = state.get("round", 1)
    max_records = int(budget.get("max_records", 50))
    retrieved = list(state.get("retrieved") or [])
    queries_done = list(state.get("queries_done") or [])
    limitations = list(state.get("limitations") or [])
    usage = dict(state.get("usage") or {})
    planned = list(state.get("planned") or [])
    errors: list[str] = []

    provider_calls_before = getattr(ctx.openalex, "api_calls", None)

    def remaining() -> int:
        return max_records - len(retrieved)

    for item in planned:
        if remaining() <= 0:
            limitations.append("record budget exhausted; query skipped")
            break
        query = item.get("query", "")
        per_page = min(10, remaining())
        try:
            records = ctx.openalex.search(query, per_page=per_page, max_per_page=25)
        except Exception as e:  # noqa: BLE001 - per-query fail-soft
            errors.append(f"{query[:60]}: {e}")
            queries_done.append({"query": query, "round": run_round,
                                 "strategy": item.get("strategy", ""),
                                 "rationale": item.get("rationale", ""),
                                 "provider": "openalex", "n_records": 0,
                                 "error": str(e)[:200]})
            continue
        for rec in records:
            retrieved.append(_record_from_openalex(rec, query, run_round))
        queries_done.append({"query": query, "round": run_round,
                             "strategy": item.get("strategy", ""),
                             "rationale": item.get("rationale", ""),
                             "provider": "openalex", "n_records": len(records),
                             "error": ""})

    # Citation expansion (round 2+, agent-planned with concrete work ids).
    expansions = list(state.get("planned_expansions") or [])
    max_exp = int(budget.get("max_expansions", 2))
    max_deep = int(budget.get("max_deep_inspections", 3))
    for exp in expansions:
        if usage.get("expansions", 0) >= max_exp:
            limitations.append("expansion budget exhausted")
            break
        if usage.get("deep_inspections", 0) >= max_deep:
            limitations.append("deep-inspection budget exhausted")
            break
        if remaining() <= 0:
            limitations.append("record budget exhausted; expansion skipped")
            break
        work_id = str(exp.get("work_openalex_id") or "")
        direction = exp.get("direction", "references")
        label = f"{direction} of {work_id}"
        try:
            if direction == "citations":
                records = ctx.openalex.search_filtered(
                    {"cites": work_id}, per_page=min(10, remaining()))
            else:
                ids = ctx.openalex.referenced_ids(work_id, limit=25)
                usage["deep_inspections"] = usage.get("deep_inspections", 0) + 1
                records = ctx.openalex.works_batch(ids[: min(10, remaining())])
                usage["deep_inspections"] = usage.get("deep_inspections", 0) + 1
            usage["expansions"] = usage.get("expansions", 0) + 1
            for rec in records:
                retrieved.append(_record_from_openalex(rec, label, run_round))
            queries_done.append({"query": label, "round": run_round,
                                 "strategy": "C",
                                 "rationale": str(exp.get("rationale", ""))[:400],
                                 "provider": "openalex",
                                 "n_records": len(records), "error": ""})
        except Exception as e:  # noqa: BLE001 - per-expansion fail-soft
            errors.append(f"{label}: {e}")
            queries_done.append({"query": label, "round": run_round,
                                 "strategy": "C", "rationale": "",
                                 "provider": "openalex", "n_records": 0,
                                 "error": str(e)[:200]})

    if provider_calls_before is not None:
        made = getattr(ctx.openalex, "api_calls", provider_calls_before)
        usage["provider_calls"] = usage.get("provider_calls", 0) + max(
            0, made - provider_calls_before)
    usage["records"] = len(retrieved)
    state["usage"] = usage

    if not retrieved:
        detail = "; ".join(errors[:3]) or "no records returned"
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"retrieval returned nothing: {detail}")
    record_transition(ctx.project_dir, state["thread_id"], "retrieve_candidates",
                      f"round {run_round}: {len(retrieved)} records, "
                      f"{len(errors)} failed calls")
    return {"retrieved": retrieved, "queries_done": queries_done,
            "limitations": limitations, "planned": [], "planned_expansions": [],
            "usage": usage}


# ---------------------------------------------------------------------------
# Nodes: dedupe, shortlist, evaluate, reflect
# ---------------------------------------------------------------------------


def d_dedupe(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    try:
        project = load_project(ctx.project_dir)
    except Exception as e:  # noqa: BLE001 - fail closed with the reason
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"cannot load project: {e}")
    run_round = state.get("round", 1)
    # Only newly retrieved records (round 2 merges into existing candidates).
    fresh = [r for r in (state.get("retrieved") or [])
             if r.get("retrieval_round", 1) == run_round]
    existing = [c for c in (state.get("candidates") or [])]
    prior = prior_dedup_items(project)
    # This-run index must also cover round-1 candidates on round 2.
    seed_records = list(existing)
    unique, duplicates = dedupe_records(
        fresh,
        project_dedup_items(project, ctx.project_dir),
        prior + [(c, "this run") for c in seed_records],
    )
    now = utc_now_iso()
    qv = state.get("question_version", 1)
    # Deterministic numbering: continue past the highest dc number in use.
    taken = set()
    for c in existing:
        cid = str(c.get("candidate_id", ""))
        if cid.startswith("dc") and cid[2:].isdigit():
            taken.add(int(cid[2:]))
    counter = max(taken) + 1 if taken else 1
    new_candidates = []
    for record in unique:
        cid = f"dc{counter}"
        counter += 1
        try:
            cand = candidate_from_record(record, cid, qv, now)
        except Exception as e:  # noqa: BLE001 - invalid record skipped, logged
            duplicates.append({**record, "duplicate_of": "invalid",
                               "duplicate_reason": str(e)[:200]})
            continue
        new_candidates.append(cand.model_dump(mode="json"))
    # Already-in-project matches surface (capped) for the UI filter; they are
    # never shortlisted or recommended.
    shown = 0
    for dup in duplicates:
        if not dup.get("already_in_project"):
            continue
        if shown >= MAX_ALREADY_IN_PROJECT_SHOWN:
            break
        cid = f"dc{counter}"
        counter += 1
        try:
            cand = candidate_from_record(dup, cid, qv, now)
        except Exception:  # noqa: BLE001 - invalid record skipped
            continue
        dump = cand.model_dump(mode="json")
        dump["project_paper_id"] = str(dup.get("project_paper_id") or "")
        new_candidates.append(dump)
        shown += 1
    counts = dict(state.get("counts") or {})
    counts["retrieved"] = len(state.get("retrieved") or [])
    counts["duplicates"] = counts.get("duplicates", 0) + len(duplicates)
    record_transition(ctx.project_dir, state["thread_id"],
                      "normalize_and_deduplicate",
                      f"{len(unique)} unique, {len(duplicates)} duplicates")
    log = list(state.get("duplicates_log") or [])
    for d in duplicates:
        log.append({k: v for k, v in d.items() if k in (
            "title", "duplicate_of", "duplicate_reason", "retrieval_query")})
    return {"candidates": existing + new_candidates,
            "duplicates_log": log,
            "counts": counts}


def _round_robin(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Deterministic round-robin interleave across retrieval queries."""
    queues = {key: list(val) for key, val in sorted(groups.items())}
    out: list[dict[str, Any]] = []
    while any(queues.values()):
        for key in sorted(queues):
            if queues[key]:
                out.append(queues[key].pop(0))
    return out


def d_shortlist(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    budget = state.get("budget") or {}
    cap = int(budget.get("max_shortlist", 8))
    pool = [c for c in (state.get("candidates") or [])
            if str(c.get("title") or "").strip()
            and not c.get("already_in_project")]
    # Prefer abstract-bearing records; keep provider order otherwise.
    with_abs = [c for c in pool if str(c.get("abstract") or "").strip()]
    without = [c for c in pool if not str(c.get("abstract") or "").strip()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for cand in with_abs + without:
        groups.setdefault(str(cand.get("retrieval_query", "?")), []).append(cand)
    ordered = _round_robin(groups)[:cap]
    shortlist = [str(c["candidate_id"]) for c in ordered]
    counts = dict(state.get("counts") or {})
    counts["shortlisted"] = len(shortlist)
    if not shortlist:
        return _dfail(ctx.project_dir, state["thread_id"],
                      "no shortlistable candidates (all duplicates or untitled)")
    record_transition(ctx.project_dir, state["thread_id"], "shortlist",
                      f"{len(shortlist)} of {len(pool)}")
    return {"shortlist": shortlist, "counts": counts}


def _eval_prompt_block(cand: dict[str, Any]) -> str:
    authors = ", ".join(cand.get("authors") or [])[:200]
    return (
        f"[{cand.get('candidate_id')}] {cand.get('title', '')}\n"
        f"  {authors} — {cand.get('venue', '')} "
        f"({cand.get('publication_year', '?')}) [{cand.get('work_type', '')}]\n"
        f"  cited by {cand.get('citation_count', '?')} | "
        f"evidence: {cand.get('evidence_access_level', '')}\n"
        f"  abstract: {_truncate(str(cand.get('abstract') or '(no abstract)'), MAX_ABSTRACT_CHARS_EVAL)}"
    )


def _apply_evaluations(state: DiscoveryState,
                       evaluations: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate agent evaluations: known ids only, verdicts in enum, novelty
    wording clamped deterministically. Returns (evaluated-map, notes)."""
    known = {str(c.get("candidate_id")) for c in (state.get("candidates") or [])}
    evaluated = dict(state.get("evaluated") or {})
    notes: list[str] = []
    for raw in evaluations or []:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("candidate_id") or "")
        if cid not in known:
            notes.append(f"dropped evaluation for unknown id {cid!r}")
            continue
        verdict = raw.get("verdict", "tangential")
        if verdict not in ("highly_relevant", "relevant", "tangential", "off_topic"):
            verdict = "tangential"
        ev = {
            "candidate_id": cid,
            "verdict": verdict,
            "why_relevant": str(raw.get("why_relevant") or "")[:2000],
            "may_contribute": str(raw.get("may_contribute") or "")[:2000],
            "may_overlap_with": str(raw.get("may_overlap_with") or "")[:2000],
            "still_unknown": str(raw.get("still_unknown") or "")[:2000],
            "why_read_next": str(raw.get("why_read_next") or "")[:2000],
            "novelty_overlap": bool(raw.get("novelty_overlap", False)),
            "novelty_note": str(raw.get("novelty_note") or "")[:2000],
        }
        fixed, hit = clamp_novelty_wording(ev["novelty_note"])
        if hit:
            ev["novelty_note"] = fixed
            notes.append(f"clamped novelty wording for {cid}")
        evaluated[cid] = ev
    return evaluated, notes


def d_evaluate(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    by_id = {str(c.get("candidate_id")): c for c in (state.get("candidates") or [])}
    todo = [cid for cid in (state.get("shortlist") or [])
            if cid not in (state.get("evaluated") or {}) and cid in by_id]
    if not todo:
        return {}
    usage = dict(state.get("usage") or {})
    budget = state.get("budget") or {}
    if usage.get("spark_calls", 0) >= int(budget.get("max_spark_calls", 8)):
        note = "spark budget exhausted; unevaluated shortlist kept as tangential"
        evaluated = dict(state.get("evaluated") or {})
        for cid in todo:
            evaluated[cid] = {"candidate_id": cid, "verdict": "tangential",
                              "why_relevant": "", "may_contribute": "",
                              "may_overlap_with": "", "still_unknown": note,
                              "why_read_next": "", "novelty_overlap": False,
                              "novelty_note": ""}
        return {"evaluated": evaluated,
                "limitations": [*state.get("limitations", []), note]}
    blocks = "\n\n".join(_eval_prompt_block(by_id[cid]) for cid in todo)
    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Research question (v{state.get('question_version', 1)}): "
            f"{state.get('question', '')}\n"
            f"Project context:\n{state.get('context_text', '')}\n\n"
            f"Candidates to assess:\n{blocks}")},
    ]
    parsed, error = _dspark_call(ctx, state, messages, EvaluationResult,
                                 "evaluate_relevance")
    if error or parsed is None:
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"relevance evaluation failed: {error}")
    evaluated, notes = _apply_evaluations(
        state, [e.model_dump() for e in parsed.evaluations])
    record_transition(ctx.project_dir, state["thread_id"], "evaluate_relevance",
                      f"{len(todo)} assessed")
    return {"evaluated": evaluated,
            "limitations": [*state.get("limitations", []), *notes],
            "usage": dict(state.get("usage") or {})}


def d_reflect(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    budget = state.get("budget") or {}
    if state.get("round", 1) >= int(budget.get("max_rounds", 2)):
        reflection = {"decision": "stop",
                      "explanation": "round budget reached",
                      "coverage_notes": ""}
        record_transition(ctx.project_dir, state["thread_id"], "reflect",
                          "stop (round budget)")
        return {"reflection": reflection}
    by_id = {str(c.get("candidate_id")): c for c in (state.get("candidates") or [])}
    evaluated = state.get("evaluated") or {}
    lines = []
    for cid in state.get("shortlist") or []:
        ev = evaluated.get(cid, {})
        cand = by_id.get(cid, {})
        title = str(cand.get("title", ""))[:100]
        bare_oa = str(cand.get("openalex_id", "")).rsplit("/", 1)[-1]
        lines.append(f"- [{cid}] {ev.get('verdict', '?')}: {title} "
                     f"(openalex: {bare_oa or 'none'})")
    queries = "; ".join(
        f"{q.get('query', '')[:80]} ({q.get('n_records', 0)})"
        for q in (state.get("queries_done") or []))
    messages = [
        {"role": "system", "content": REFLECT_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Research question: {state.get('question', '')}\n"
            f"Round-1 queries [{queries}]\n"
            f"Shortlist verdicts:\n" + "\n".join(lines) +
            f"\n\nRemaining budget: round 2 with up to "
            f"{budget.get('max_queries_per_round', 3)} new queries and up to "
            f"{budget.get('max_expansions', 2)} citation expansions. "
            f"Decide now.")},
    ]
    parsed, error = _dspark_call(ctx, state, messages, ReflectionResult,
                                 "reflect")
    if error or parsed is None:
        note = f"reflection failed ({error}); stopping after round 1"
        reflection = {"decision": "stop", "explanation": note,
                      "coverage_notes": ""}
        record_transition(ctx.project_dir, state["thread_id"], "reflect",
                          "stop (reflection failed)")
        return {"reflection": reflection,
                "limitations": [*state.get("limitations", []), note]}
    seen = {str(q.get("query", "")).lower()
            for q in (state.get("queries_done") or [])}
    clean_q, notes = _validate_plan_queries(
        [q.model_dump() for q in parsed.round2_queries], budget, seen)
    clean_e: list[dict[str, Any]] = []
    known_oa = {str(c.get("openalex_id", "")).rsplit("/", 1)[-1].upper()
                for c in (state.get("candidates") or [])}
    for raw in parsed.expansions or []:
        bare = raw.work_openalex_id.strip().rsplit("/", 1)[-1].upper()
        if not bare or bare not in known_oa:
            notes.append(f"dropped expansion for unknown work {bare!r}")
            continue
        if raw.direction not in ("references", "citations"):
            continue
        clean_e.append({"work_openalex_id": bare, "direction": raw.direction,
                        "rationale": raw.rationale[:400]})
        if len(clean_e) >= int(budget.get("max_expansions", 2)):
            break
    decision = parsed.decision if (clean_q or clean_e) else "stop"
    if parsed.decision == "continue" and decision == "stop":
        notes.append("round 2 had no usable queries/expansions; stopping")
    reflection = {"decision": decision,
                  "explanation": parsed.explanation[:2000],
                  "coverage_notes": parsed.coverage_notes[:2000]}
    record_transition(ctx.project_dir, state["thread_id"], "reflect",
                      f"{decision}: {parsed.explanation[:120]}")
    return {"reflection": reflection,
            "planned": clean_q if decision == "continue" else [],
            "planned_expansions": clean_e if decision == "continue" else [],
            "round": state.get("round", 1) + 1 if decision == "continue" else state.get("round", 1),
            "limitations": [*state.get("limitations", []), *notes],
            "usage": dict(state.get("usage") or {})}


def _route_after_reflect(state: DiscoveryState) -> str:
    if state.get("status") == "failed":
        return END
    if (state.get("reflection") or {}).get("decision") == "continue":
        return "retrieve_candidates"
    return "discovery_critic"


# ---------------------------------------------------------------------------
# Nodes: critic, revision, prepare, persist
# ---------------------------------------------------------------------------


def _critic_prompt_block(cand: dict[str, Any], evaluated: dict[str, Any]) -> str:
    ev = evaluated.get(str(cand.get("candidate_id")), {})
    return (
        f"{_eval_prompt_block(cand)}\n"
        f"  EVALUATION verdict={ev.get('verdict', '?')} "
        f"novelty_overlap={ev.get('novelty_overlap', False)}\n"
        f"  why_relevant: {_truncate(str(ev.get('why_relevant', '')), 600)}\n"
        f"  may_contribute: {_truncate(str(ev.get('may_contribute', '')), 600)}\n"
        f"  may_overlap_with: {_truncate(str(ev.get('may_overlap_with', '')), 600)}\n"
        f"  novelty_note: {_truncate(str(ev.get('novelty_note', '')), 600)}"
    )


def d_critic(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    by_id = {str(c.get("candidate_id")): c for c in (state.get("candidates") or [])}
    evaluated = state.get("evaluated") or {}
    todo = [cid for cid in (state.get("shortlist") or []) if cid in evaluated]
    if not todo:
        return _dfail(ctx.project_dir, state["thread_id"],
                      "nothing evaluated to critique")
    known_list = ", ".join(todo)
    blocks = "\n\n".join(_critic_prompt_block(by_id[cid], evaluated) for cid in todo)
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Research question: {state.get('question', '')}\n"
            f"Project context:\n{state.get('context_text', '')}\n\n"
            f"Known candidate ids (any other id is fabricated): "
            f"{known_list}\n\n"
            f"Evaluated candidates:\n{blocks}")},
    ]
    parsed, error = _dspark_call(ctx, state, messages, DiscoveryCriticVerdict,
                                 "discovery_critic")
    if error or parsed is None:
        return _dfail(ctx.project_dir, state["thread_id"],
                      f"recommendation critique failed: {error}")
    # Deterministic gates: known ids only, capped drops.
    known = set(todo)
    drops = [c for c in (parsed.drop_candidate_ids or []) if c in known]
    unknown_drops = [c for c in (parsed.drop_candidate_ids or []) if c not in known]
    concerns = []
    for raw in parsed.concerns or []:
        dump = raw.model_dump()
        if dump.get("candidate_id") and dump["candidate_id"] not in known:
            dump["candidate_id"] = ""
        concerns.append(dump)
    critic = {"verdict": parsed.verdict, "summary": parsed.summary[:2000],
              "concerns": concerns, "drop_candidate_ids": drops,
              "required_fixes": [str(f)[:500] for f in (parsed.required_fixes or [])]}
    notes = []
    if unknown_drops:
        notes.append(f"critic named unknown ids (ignored): {unknown_drops}")
    record_transition(ctx.project_dir, state["thread_id"], "discovery_critic",
                      f"{parsed.verdict}: {parsed.summary[:120]}")
    return {"critic": critic,
            "limitations": [*state.get("limitations", []), *notes],
            "usage": dict(state.get("usage") or {})}


def _route_after_gate(state: DiscoveryState) -> str:
    if state.get("status") == "failed":
        return END
    verdict = (state.get("critic") or {}).get("verdict", "SUPPORTED")
    if verdict == "NEEDS_REVISION" and not state.get("revision_used"):
        return "bounded_revision"
    return "prepare_recommendations"


def d_revise(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    critic = state.get("critic") or {}
    by_id = {str(c.get("candidate_id")): c for c in (state.get("candidates") or [])}
    evaluated = state.get("evaluated") or {}
    flagged = sorted({str(c.get("candidate_id")) for c in (critic.get("concerns") or [])
                      if str(c.get("candidate_id")) in evaluated})
    if not flagged:
        flagged = [cid for cid in (state.get("shortlist") or []) if cid in evaluated][:3]
    blocks = "\n\n".join(_critic_prompt_block(by_id[cid], evaluated) for cid in flagged)
    messages = [
        {"role": "system", "content": REVISE_SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Critic summary: {critic.get('summary', '')}\n"
            f"Required fixes:\n" +
            "\n".join(f"- {f}" for f in (critic.get("required_fixes") or [])) +
            f"\n\nFlagged evaluations:\n{blocks}")},
    ]
    parsed, error = _dspark_call(ctx, state, messages, DiscoveryRevision,
                                 "bounded_revision")
    if error or parsed is None:
        note = f"revision failed ({error}); keeping critic-flagged evaluations with limitations"
        return {"revision_used": True,
                "limitations": [*state.get("limitations", []), note]}
    evaluated_new, notes = _apply_evaluations(
        state, [e.model_dump() for e in parsed.corrected_evaluations])
    record_transition(ctx.project_dir, state["thread_id"], "bounded_revision",
                      parsed.notes[:150])
    return {"evaluated": evaluated_new, "revision_used": True,
            "limitations": [*state.get("limitations", []), *notes],
            "usage": dict(state.get("usage") or {})}


VERDICT_RANK = {"highly_relevant": 0, "relevant": 1, "tangential": 2,
                "off_topic": 3}


def d_prepare(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _dctx(config)
    budget = state.get("budget") or {}
    cap = int(budget.get("max_recommendations", 5))
    drops = set((state.get("critic") or {}).get("drop_candidate_ids") or [])
    evaluated = state.get("evaluated") or {}
    order = {cid: i for i, cid in enumerate(state.get("shortlist") or [])}
    ranked = sorted(
        (cid for cid in evaluated
         if cid not in drops and evaluated[cid].get("verdict") != "off_topic"),
        key=lambda cid: (VERDICT_RANK.get(evaluated[cid].get("verdict", "tangential"), 2),
                         order.get(cid, 999)),
    )[:cap]
    # Final novelty clamp on recommended notes (defense in depth).
    notes = []
    for cid in ranked:
        ev = evaluated[cid]
        fixed, hit = clamp_novelty_wording(ev.get("novelty_note", ""))
        if hit:
            ev["novelty_note"] = fixed
            notes.append(f"final novelty clamp for {cid}")
    counts = dict(state.get("counts") or {})
    counts["recommended"] = len(ranked)
    record_transition(ctx.project_dir, state["thread_id"],
                      "prepare_recommendations", f"{len(ranked)} recommended")
    return {"recommendations": ranked, "evaluated": evaluated,
            "counts": counts,
            "limitations": [*state.get("limitations", []), *notes]}


def d_persist(state: DiscoveryState, config: RunnableConfig) -> dict[str, Any]:
    import workspace

    ctx = _dctx(config)
    now = utc_now_iso()
    with workspace.project_lock(state["project_id"]):
        try:
            project = load_project(ctx.project_dir)
        except Exception as e:  # noqa: BLE001 - fail closed with the reason
            return _dfail(ctx.project_dir, state["thread_id"],
                          f"cannot load project: {e}")
        current_qv = project.question_version or 1
        stale = current_qv != state.get("start_question_version", current_qv)
        limitations = list(state.get("limitations") or [])
        if stale:
            limitations.append(
                f"question changed mid-search (v{state['start_question_version']} "
                f"-> v{current_qv}); results describe the older question")
        evaluated = state.get("evaluated") or {}
        recommended = set(state.get("recommendations") or [])
        # Persist the actionable set only (shortlisted + evaluated +
        # already-in-project surfaced matches): the full retrieval log
        # lives in agent/discovery/<run>.json so project.json stays small.
        keep = set(state.get("shortlist") or []) | set(evaluated)
        candidates = []
        for raw in (state.get("candidates") or []):
            if str(raw.get("candidate_id")) not in keep \
                    and not raw.get("already_in_project"):
                continue
            try:
                cand = DiscoveryCandidate.model_validate(raw)
            except Exception:  # noqa: BLE001 - invalid candidate skipped
                continue
            if cand.candidate_id in evaluated:
                try:
                    from discovery_models import CandidateEvaluation
                    cand.evaluation = CandidateEvaluation.model_validate(
                        evaluated[cand.candidate_id])
                except Exception:  # noqa: BLE001 - keep default evaluation
                    pass
            cand.recommended = cand.candidate_id in recommended
            candidates.append(cand)
        usage = dict(state.get("usage") or {})
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            tokens = sum(int(t.get("total_tokens") or 0)
                         for t in (record.telemetry or [])
                         if isinstance(t, dict))
            usage["tokens_total"] = tokens
        except (FileNotFoundError, TypeError, ValueError):
            pass
        run = DiscoveryRun(
            run_id=state["thread_id"],
            thread_id=state["thread_id"],
            project_id=state["project_id"],
            research_question=state.get("question", ""),
            research_question_version=state.get("question_version", 1),
            focus=state.get("focus", ""),
            status="completed",
            stale=stale,
            created_at=now,
            updated_at=now,
            budget=state.get("budget") or {},
            strategy_notes=(state.get("strategy_notes") or "")[:2000],
            queries=[DiscoveryQueryRecord.model_validate(q)
                     for q in (state.get("queries_done") or [])
                     if isinstance(q, dict)],
            counts=dict(state.get("counts") or {}),
            reflection_decision=(state.get("reflection") or {}).get("decision", ""),
            reflection_explanation=(state.get("reflection") or {}).get(
                "explanation", "")[:2000],
            critic_verdict=(state.get("critic") or {}).get("verdict", ""),
            critic_notes=(state.get("critic") or {}).get("summary", "")[:2000],
            revision_used=bool(state.get("revision_used", False)),
            limitations="; ".join(limitations)[:4000],
            candidates=candidates,
            usage={k: int(v) for k, v in usage.items()},
        )
        # Idempotent upsert by run id (safe retries overwrite, never duplicate).
        project.discoveries = [r for r in (project.discoveries or [])
                               if r.run_id != run.run_id] + [run]
        project.updated_at = now
        save_project(ctx.project_dir, project)
    # Full retrieval log for provenance (outside project.json to bound size).
    log_path = discovery_dir(ctx.project_dir) / f"{state['thread_id']}.json"
    try:
        slim = [{k: r.get(k) for k in (
            "title", "openalex_id", "doi", "arxiv_id", "retrieval_query",
            "retrieval_round")} for r in (state.get("retrieved") or [])]
        log_path.write_text(json.dumps({
            "run_id": state["thread_id"],
            "retrieved": slim,
            "duplicates_log": state.get("duplicates_log") or [],
            "queries_done": state.get("queries_done") or [],
            "usage": dict(state.get("usage") or {}),
            "limitations": list(state.get("limitations") or []),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.status = "completed"
        record.updated_at = now
        record.decisions.append(
            f"discovery completed: {len(run.candidates)} candidates, "
            f"{len(state.get('recommendations') or [])} recommended")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "persist_recommendations",
                      f"run {run.run_id} stored")
    return {"status": "completed"}


def build_discovery_graph(checkpointer: SqliteSaver):
    graph = StateGraph(DiscoveryState)
    graph.add_node("load_context", d_load_context)
    graph.add_node("plan_search", d_plan_search)
    graph.add_node("retrieve_candidates", d_retrieve)
    graph.add_node("normalize_and_deduplicate", d_dedupe)
    graph.add_node("shortlist", d_shortlist)
    graph.add_node("evaluate_relevance", d_evaluate)
    graph.add_node("reflect", d_reflect)
    graph.add_node("discovery_critic", d_critic)
    graph.add_node("discovery_decision_gate", lambda state: {})
    graph.add_node("bounded_revision", d_revise)
    graph.add_node("prepare_recommendations", d_prepare)
    graph.add_node("persist_recommendations", d_persist)

    graph.add_edge(START, "load_context")
    graph.add_conditional_edges("load_context", _pass_or_end("plan_search"),
                               {"plan_search": "plan_search", END: END})
    graph.add_conditional_edges("plan_search", _pass_or_end("retrieve_candidates"),
                               {"retrieve_candidates": "retrieve_candidates", END: END})
    graph.add_conditional_edges("retrieve_candidates",
                               _pass_or_end("normalize_and_deduplicate"),
                               {"normalize_and_deduplicate": "normalize_and_deduplicate",
                                END: END})
    graph.add_conditional_edges("normalize_and_deduplicate", _pass_or_end("shortlist"),
                               {"shortlist": "shortlist", END: END})
    graph.add_conditional_edges("shortlist", _pass_or_end("evaluate_relevance"),
                               {"evaluate_relevance": "evaluate_relevance", END: END})
    graph.add_conditional_edges("evaluate_relevance", _pass_or_end("reflect"),
                               {"reflect": "reflect", END: END})
    graph.add_conditional_edges("reflect", _route_after_reflect,
                               {"retrieve_candidates": "retrieve_candidates",
                                "discovery_critic": "discovery_critic", END: END})
    graph.add_conditional_edges("discovery_critic",
                               _pass_or_end("discovery_decision_gate"),
                               {"discovery_decision_gate": "discovery_decision_gate",
                                END: END})
    graph.add_conditional_edges("discovery_decision_gate", _route_after_gate, {
        "bounded_revision": "bounded_revision",
        "prepare_recommendations": "prepare_recommendations",
        END: END,
    })
    graph.add_conditional_edges("bounded_revision",
                               _pass_or_end("discovery_critic"),
                               {"discovery_critic": "discovery_critic", END: END})
    graph.add_edge("prepare_recommendations", "persist_recommendations")
    graph.add_edge("persist_recommendations", END)
    return graph.compile(checkpointer=checkpointer)


def run_discovery(
    project_dir: Path,
    thread_id: str,
    focus: str = "",
    budget: Optional[DiscoveryBudget] = None,
    spark: Any = None,
    openalex: Any = None,
) -> RunRecord:
    """Run the full discovery graph once. Creates the run record first
    (kind='discover'); no interrupt — approval happens after completion."""
    from spark_open import SparkOpenClient

    if spark is None:
        spark = SparkOpenClient()
    if openalex is None:
        from audit_graph import openalex_cache_dir
        from openalex import OpenAlexClient

        openalex = OpenAlexClient(openalex_cache_dir(project_dir))
    project = load_project(project_dir)
    project_id = project_dir.resolve().name
    now = utc_now_iso()
    record = RunRecord(
        thread_id=thread_id,
        project_id=project_id,
        kind="discover",
        paper_id="",
        claim_id="",
        research_question=project.research_question,
        status="running",
        created_at=now,
        updated_at=now,
    )
    record.decisions.append(
        f"discovery started (focus: {(focus or 'broad')[:150]})")
    save_run_record(project_dir, record)
    ctx = DiscoveryContext(project_dir=project_dir, spark=spark,
                           openalex=openalex, focus=focus or "",
                           budget=budget or DiscoveryBudget())
    config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
    agent_dir(project_dir).mkdir(parents=True, exist_ok=True)
    from agent_graph import checkpoints_path

    with SqliteSaver.from_conn_string(
            str(checkpoints_path(project_dir))) as checkpointer:
        graph = build_discovery_graph(checkpointer)
        graph.invoke(initial_discovery_state(project_id, thread_id, focus or "",
                                             ctx.budget), config)
    final = load_run_record(project_dir, thread_id)
    if final.status == "failed":
        raise DiscoveryFailure(final.error or "discovery run failed")
    return final


def update_candidate_status(
    project_dir: Path,
    run_id: str,
    candidate_id: str,
    status: str,
    reason: str = "",
    **fields: Any,
) -> DiscoveryCandidate:
    """Researcher decision / import lifecycle mutation (single-writer)."""
    import workspace

    allowed = {"pending", "approved", "dismissed", "importing", "imported",
               "import_failed", "full_text_unavailable"}
    if status not in allowed:
        raise ValueError(f"invalid candidate status {status!r}")
    with workspace.project_lock(project_dir.resolve().name):
        project = load_project(project_dir)
        run = next((r for r in (project.discoveries or []) if r.run_id == run_id),
                   None)
        if run is None:
            raise FileNotFoundError(f"unknown discovery run {run_id!r}")
        cand = next((c for c in run.candidates if c.candidate_id == candidate_id),
                    None)
        if cand is None:
            raise FileNotFoundError(f"unknown candidate {candidate_id!r}")
        if status != cand.status:
            if cand.status in ("importing", "imported") and status in (
                    "pending", "approved", "dismissed"):
                raise ValueError(f"candidate {candidate_id} is {cand.status}; "
                                 f"researcher decision locked")
            if cand.status == "imported":
                raise ValueError(f"candidate {candidate_id} is already "
                                 f"imported; terminal state")
        cand.status = status  # type: ignore[assignment]
        if reason:
            cand.status_reason = reason[:500]
        for key, value in fields.items():
            if hasattr(cand, key):
                setattr(cand, key, value)
        # Re-validate: setattr bypasses pydantic, and an invalid value here
        # would corrupt project.json for every future load. Failure raises
        # before anything is saved.
        idx = run.candidates.index(cand)
        run.candidates[idx] = DiscoveryCandidate.model_validate(
            cand.model_dump())
        run.updated_at = utc_now_iso()
        project.updated_at = run.updated_at
        save_project(project_dir, project)
        return run.candidates[idx]


