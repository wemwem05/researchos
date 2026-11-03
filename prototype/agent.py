#!/usr/bin/env python3
"""ResearchOS Milestone 5 CLI: run and resume agentic investigations.

Usage:
    python prototype/agent.py investigate \
        --project-dir prototype/projects/ot-correspondence \
        --paper-id eot-maps --pdf samples/x.pdf \
        --analysis prototype/output.json --localized prototype/localized_output.json
    python prototype/agent.py status --project-dir <dir> [--thread-id <id>]
    python prototype/agent.py review --project-dir <dir> --thread-id <id> \
        --choice "Not comparable — drop the connection" --free-text "..."
    python prototype/agent.py stage-web --project-dir <dir> --web-dir web
    python prototype/agent.py serve --project-dir <dir> --port 8765
    python prototype/agent.py verify

Auth for Spark Open: BASETEN_API_KEY (the PDF pipeline's MODEL_API_KEY is
untouched). Resume (review) needs no API key: everything after the human
interrupt is deterministic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.sqlite import SqliteSaver

from agent_graph import (
    RunContext,
    RunRecord,
    agent_dir,
    build_graph,
    checkpoints_path,
    initial_state,
    list_run_records,
    load_run_record,
    new_thread_id,
    read_telemetry,
    resume_command,
    save_run_record,
    telemetry_summary,
)
from analyze_paper import AnalysisResult
from project import PaperEntry, load_project, project_path
from spark_open import SparkOpenClient, SparkOpenError, utc_now_iso, verify_connectivity


class NoSparkCalls:
    """Resume-path guard: nothing after the human interrupt may call a model."""

    def complete_json(self, *args, **kwargs):  # noqa: ANN002,ANN003
        raise AssertionError("no model calls allowed after human review")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _rel(p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(_repo_root()))
    except ValueError:
        return p


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else _repo_root() / path


def _paper_entry(args: argparse.Namespace, analysis: AnalysisResult, order: int) -> PaperEntry:
    n_localized = sum(
        1 for o in json.loads(Path(args.localized).read_text())["localizations"] if o["bbox"]
    )
    return PaperEntry(
        paper_id=args.paper_id,
        title=analysis.paper_title,
        filename=Path(args.pdf).name,
        added_order=order,
        n_objects=len(analysis.objects),
        n_localized=n_localized,
        pdf_path=_rel(args.pdf),
        localized_path=_rel(args.localized),
    )


def _checkpointer(project_dir: Path) -> SqliteSaver:
    agent_dir(project_dir).mkdir(parents=True, exist_ok=True)
    return SqliteSaver.from_conn_string(str(checkpoints_path(project_dir)))


def cmd_verify(_args: argparse.Namespace) -> int:
    try:
        tel = verify_connectivity()
    except SparkOpenError as e:
        print(f"error: Baseten connectivity probe failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(tel.model_dump(), indent=2))
    print(
        f"[ok] {tel.provider} {tel.model} reachable in {tel.latency_ms}ms",
        file=sys.stderr,
    )
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    if not project_path(project_dir).is_file():
        print(f"error: no project in {project_dir} (run project.py init first)", file=sys.stderr)
        return 2
    for label, path in (("PDF", args.pdf), ("analysis", args.analysis), ("localized", args.localized)):
        if not Path(path).is_file():
            print(f"error: {label} not found: {path}", file=sys.stderr)
            return 2
    try:
        analysis = AnalysisResult.model_validate_json(Path(args.analysis).read_text())
    except Exception as e:
        print(f"error: invalid analysis file: {e}", file=sys.stderr)
        return 2
    project = load_project(project_dir)
    if any(p.paper_id == args.paper_id for p in project.papers):
        print(f"error: paper '{args.paper_id}' is already in the project", file=sys.stderr)
        return 2
    try:
        spark = SparkOpenClient()
    except SparkOpenError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    paper = _paper_entry(args, analysis, len(project.papers) + 1)
    project_id = project_dir.resolve().name
    thread_id = args.thread_id or new_thread_id(project_id, args.paper_id)
    record = RunRecord(
        thread_id=thread_id,
        project_id=project_id,
        paper_id=args.paper_id,
        research_question=project.research_question,
        status="running",
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
        paper=paper.model_dump(),
        analysis_path=_rel(args.analysis),
    )
    save_run_record(project_dir, record)
    ctx = RunContext(project_dir=project_dir, analysis=analysis, paper=paper, spark=spark)
    config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
    with _checkpointer(project_dir) as checkpointer:
        graph = build_graph(checkpointer)
        graph.invoke(initial_state(project_id, thread_id, args.paper_id), config)
    record = load_run_record(project_dir, thread_id)
    if record.status == "awaiting_human":
        req = record.human_review_request or {}
        print(f"[human review] thread {thread_id} is waiting.", file=sys.stderr)
        print(f"  question: {req.get('question', '')[:300]}", file=sys.stderr)
        print(f"  options: {req.get('options', [])}", file=sys.stderr)
        print(f"  resume: agent.py review --project-dir {args.project_dir} "
              f"--thread-id {thread_id} --choice \"...\"", file=sys.stderr)
        return 0
    if record.status == "completed":
        print(f"[done] thread {thread_id} completed.", file=sys.stderr)
        return 0
    print(f"[failed] thread {thread_id}: {record.error}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    if args.thread_id:
        try:
            record = load_run_record(project_dir, args.thread_id)
        except FileNotFoundError:
            print(f"error: unknown thread {args.thread_id}", file=sys.stderr)
            return 2
        print(record.model_dump_json(indent=2))
        return 0
    records = list_run_records(project_dir)
    if not records:
        print("(no agent runs yet)")
        return 0
    for r in records:
        steps = " -> ".join(t.node for t in r.transitions[-6:])
        print(f"{r.thread_id}  paper={r.paper_id}  status={r.status}  updated={r.updated_at}")
        print(f"    {steps}")
    summary = telemetry_summary(project_dir)
    print(f"telemetry: {summary['calls']} calls, avg "
          f"{summary['avg_latency_ms']}ms ({summary['provider']} {summary['model']})")
    return 0


def resume_investigation(project_dir: Path, thread_id: str, choice: str, free_text: str = "") -> RunRecord:
    """Resume an awaiting_human run with the researcher's decision."""
    record = load_run_record(project_dir, thread_id)
    if record.status != "awaiting_human":
        raise ValueError(f"thread {thread_id} is '{record.status}', not awaiting_human")
    analysis = AnalysisResult.model_validate_json(_resolve(record.analysis_path).read_text())
    paper = PaperEntry.model_validate(record.paper)
    ctx = RunContext(
        project_dir=project_dir, analysis=analysis, paper=paper,
        spark=NoSparkCalls(), synthetic=record.synthetic,
    )
    config = {"configurable": {"thread_id": thread_id, "ctx": ctx}}
    with _checkpointer(project_dir) as checkpointer:
        graph = build_graph(checkpointer)
        graph.invoke(resume_command(choice, free_text), config)
    return load_run_record(project_dir, thread_id)


def cmd_review(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    try:
        record = resume_investigation(project_dir, args.thread_id, args.choice, args.free_text or "")
    except FileNotFoundError:
        print(f"error: unknown thread {args.thread_id}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"[{record.status}] thread {args.thread_id}", file=sys.stderr)
    if record.status == "failed":
        print(f"  error: {record.error}", file=sys.stderr)
        return 1
    return 0


def stage_agent_web(project_dir: Path, web_dir: Path) -> None:
    out = web_dir / "public" / "data" / "agent"
    runs_out = out / "runs"
    runs_out.mkdir(parents=True, exist_ok=True)
    records = list_run_records(project_dir)
    for r in records:
        (runs_out / f"{r.thread_id}.json").write_text(
            r.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    latest = records[-1] if records else None
    (out / "latest.json").write_text(
        (latest.model_dump_json(indent=2) if latest else "null") + "\n", encoding="utf-8"
    )
    (out / "telemetry.json").write_text(
        json.dumps(read_telemetry(project_dir), indent=2) + "\n", encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(telemetry_summary(project_dir), indent=2) + "\n", encoding="utf-8"
    )
    print(f"[done] staged {len(records)} agent runs + telemetry into {out}", file=sys.stderr)


def cmd_stage_web(args: argparse.Namespace) -> int:
    try:
        stage_agent_web(Path(args.project_dir), Path(args.web_dir))
    except Exception as e:
        print(f"error: stage-web failed: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from agent_server import serve

    serve(Path(args.project_dir), int(args.port))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ResearchOS Milestone 5: agent graph CLI.")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("investigate", help="Run an agentic add-paper investigation.")
    pi.add_argument("--project-dir", required=True)
    pi.add_argument("--paper-id", required=True)
    pi.add_argument("--pdf", required=True)
    pi.add_argument("--analysis", required=True)
    pi.add_argument("--localized", required=True)
    pi.add_argument("--thread-id", default=None)

    ps = sub.add_parser("status", help="Show agent runs.")
    ps.add_argument("--project-dir", required=True)
    ps.add_argument("--thread-id", default=None)

    pr = sub.add_parser("review", help="Answer a pending human review (resumes the run).")
    pr.add_argument("--project-dir", required=True)
    pr.add_argument("--thread-id", required=True)
    pr.add_argument("--choice", required=True)
    pr.add_argument("--free-text", default="")

    pw = sub.add_parser("stage-web", help="Stage agent runs + telemetry into web/public.")
    pw.add_argument("--project-dir", required=True)
    pw.add_argument("--web-dir", default="web")

    pv = sub.add_parser("verify", help="Probe Baseten connectivity with one real call.")

    pserve = sub.add_parser("serve", help="Serve the agent API for live HITL resume.")
    pserve.add_argument("--project-dir", required=True)
    pserve.add_argument("--port", default="8765")
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    if args.command == "investigate":
        return cmd_investigate(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "review":
        return cmd_review(args)
    if args.command == "stage-web":
        return cmd_stage_web(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "serve":
        return cmd_serve(args)
    raise AssertionError(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
