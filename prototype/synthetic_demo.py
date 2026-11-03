#!/usr/bin/env python3
"""SYNTHETIC DEMO ONLY — not scientific evidence.

Builds an explicitly fake interrupt/resume scenario for the Milestone 5
human-review pathway: two imaginary papers (SYN-A, SYN-B) with an apparent
contradiction on different dataset splits. Nothing here comes from a real
paper, and this fixture is NEVER merged into a real research project.

The canned client replaces Muse Spark Open (no API calls, no cost). The
resulting run record is staged for the Project view with synthetic=true and
a [SYNTHETIC DEMO] banner in every user-visible string.

Usage:
    python prototype/synthetic_demo.py --project-dir prototype/projects/synthetic-hitl-demo
    python prototype/agent.py stage-web --project-dir prototype/projects/synthetic-hitl-demo --web-dir web
    # then resume it live:
    python prototype/agent.py review --project-dir prototype/projects/synthetic-hitl-demo \
        --thread-id <id> --choice "Not comparable — drop the connection"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.sqlite import SqliteSaver

import agent_graph as ag
import project as pr
import spark_open
from agent_graph import (
    CriticVerdict,
    HumanQuestionDraft,
    InvestigatorProposal,
    RunContext,
    RunRecord,
)
from analyze_paper import AnalysisResult
from spark_open import TelemetryRecord

BANNER = "[SYNTHETIC DEMO — not scientific evidence]"


def synthetic_analysis() -> AnalysisResult:
    def obj(oid, otype, label):
        return {
            "id": oid, "type": otype, "page": 1, "label": label, "title": f"{BANNER} {label}",
            "what_it_is": "synthetic", "why_it_is_here": "synthetic",
            "claim_supported": "synthetic", "limitations": "synthetic",
            "paper_importance": 0.5, "paper_importance_reason": "synthetic",
            "research_relevance": "synthetic", "research_relevance_score": 0.9,
            "research_relevance_reason": "synthetic", "related_objects": [],
            "confidence": 0.9, "evidence": "synthetic",
        }

    return AnalysisResult.model_validate({
        "paper_title": f"{BANNER} imaginary paper SYN-B",
        "research_question": "synthetic question",
        "objects": [
            obj("syn-eq-1", "equation", "Equation 1"),
            obj("syn-fig-1", "figure", "Figure 1"),
            obj("syn-tab-1", "table", "Table 1"),
            obj("syn-claim-1", "claim", "Claim 1"),
        ],
    })


class CannedSyntheticClient:
    """Deterministic stand-in for Spark Open. Makes zero API calls."""

    def complete_json(self, messages, schema, *, node, thread_id):
        if node == "investigator":
            payload = InvestigatorProposal.model_validate({
                "findings": [{
                    "statement": f"{BANNER} SYN-B reports 92% accuracy with cost Q.",
                    "evidence": "Table 1, p.1 (synthetic)",
                    "page": 1, "object_ids": ["syn-tab-1"], "confidence": 0.9,
                }],
                "connections": [{
                    "from_ref": "new[0]", "to_ref": "f1", "type": "contradicts",
                    "reason": f"{BANNER} SYN-B reports 92% where SYN-A reported 81%.",
                    "confidence": 0.8,
                }],
                "contribution": {
                    "what_it_adds": f"{BANNER} an apparent contradiction.",
                    "stance_toward_prior": "Challenges f1 on the surface.",
                    "overlap_with_direction": "Synthetic.",
                    "new_open_questions": [],
                },
                "open_questions": [],
                "novelty_risks": [],
                "methodological_differences": [f"{BANNER} different synthetic splits."],
                "what_changed": f"{BANNER} nothing; this is a fixture.",
            })
        elif node in ("critic", "critic_after_revision"):
            payload = CriticVerdict.model_validate({
                "verdict": "AMBIGUOUS_REQUIRES_HUMAN",
                "summary": f"{BANNER} the papers use different synthetic splits.",
                "checks": [{"name": "splits_comparable", "passed": False,
                            "note": "SYN-A split X vs SYN-B split Y (both imaginary)."}],
                "concerns": [{"claim_ref": "connection new[0]", "question": "Comparable?",
                              "detail": "Different splits.", "severity": "blocking"}],
                "required_fixes": [],
                "human_question": HumanQuestionDraft(
                    question=f"{BANNER} Paper SYN-A reports 81% while paper SYN-B "
                    "reports 92%. However, the papers use different dataset splits. "
                    "Should these findings be treated as directly comparable?",
                    investigator_position="Comparable: both report accuracy.",
                    critic_concern="Different splits may make comparison indefensible.",
                    affected_connection_indexes=[0],
                    options=["Comparable — keep the connection",
                             "Not comparable — drop the connection",
                             "Keep unresolved"],
                ).model_dump(),
            })
        else:
            raise AssertionError(f"{BANNER} unexpected node '{node}'")
        tel = TelemetryRecord(
            timestamp="2026-11-03T00:00:00+00:00", thread_id=thread_id, node=node,
            latency_ms=0, success=True, provider="synthetic-demo",
            model="synthetic-demo-canned-responses",
        )
        return payload, tel


def build_synthetic_project(project_dir: Path) -> None:
    proj = pr.Project(research_question=f"{BANNER} synthetic question?")
    proj.papers.append(pr.PaperEntry(
        paper_id="syn-a", title=f"{BANNER} imaginary paper SYN-A",
        filename="synthetic.pdf", added_order=1, n_objects=4, n_localized=0))
    proj.findings.append(pr.Finding(
        id="f1", paper_id="syn-a",
        statement=f"{BANNER} SYN-A reports 81% accuracy with cost P.",
        evidence="Table 1, p.1 (synthetic)", page=1, object_ids=[], confidence=0.9))
    proj.contributions.append(pr.PaperContribution(
        paper_id="syn-a", what_it_adds="synthetic baseline",
        stance_toward_prior="first synthetic paper",
        overlap_with_direction="synthetic"))
    pr.save_project(project_dir, proj)


def main() -> int:
    p = argparse.ArgumentParser(description="Build the synthetic HITL demo fixture.")
    p.add_argument("--project-dir", required=True)
    p.add_argument("--thread-id", default="synthetic-hitl-demo--awaiting")
    args = p.parse_args()

    project_dir = Path(args.project_dir)
    build_synthetic_project(project_dir)
    analysis = synthetic_analysis()
    paper = pr.PaperEntry(paper_id="syn-b",
                          title=f"{BANNER} imaginary paper SYN-B",
                          filename="synthetic.pdf", added_order=2,
                          n_objects=4, n_localized=0)
    thread_id = args.thread_id
    record = RunRecord(thread_id=thread_id, project_id=project_dir.resolve().name,
                       paper_id="syn-b", research_question=f"{BANNER} synthetic question?",
                       status="running", created_at=spark_open.utc_now_iso(),
                       synthetic=True, paper=paper.model_dump(), analysis_path="synthetic")
    ag.save_run_record(project_dir, record)
    # Persist the synthetic analysis beside the run for resume support.
    (ag.agent_dir(project_dir) / "synthetic_analysis.json").write_text(
        analysis.model_dump_json(indent=2), encoding="utf-8")
    record.analysis_path = str((ag.agent_dir(project_dir) / "synthetic_analysis.json")
                               .resolve().relative_to(Path(__file__).resolve().parent.parent))
    ag.save_run_record(project_dir, record)

    ctx = RunContext(project_dir=project_dir, analysis=analysis, paper=paper,
                     spark=CannedSyntheticClient(), synthetic=True)
    config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
    ag.agent_dir(project_dir).mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(ag.checkpoints_path(project_dir))) as cp:
        graph = ag.build_graph(cp)
        graph.invoke(ag.initial_state(project_dir.resolve().name, thread_id, "syn-b"), config)
    record = ag.load_run_record(project_dir, thread_id)
    print(f"[{record.status}] {thread_id} (SYNTHETIC DEMO)", file=sys.stderr)
    return 0 if record.status == "awaiting_human" else 1


if __name__ == "__main__":
    raise SystemExit(main())
