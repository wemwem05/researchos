#!/usr/bin/env python3
"""ResearchOS Milestone 5: minimal stdlib API server for the agent graph.

The Next.js frontend is static; this server enables LIVE human-review resume
from the Project view. When it is not running, the UI falls back to the
staged fixtures (agent.py stage-web). No new dependencies: stdlib only.

Endpoints (JSON, CORS open for localhost demo use):
  GET  /api/agent/runs
  GET  /api/agent/runs/<thread_id>
  POST /api/agent/runs/<thread_id>/review   {"choice": str, "free_text": str}
  POST /api/agent/investigate               {"paper_id","pdf","analysis","localized"}
  GET  /api/agent/telemetry                 {"summary": ..., "recent": [...]}
  GET  /api/agent/verifications             (Milestone 6: attached evidence)
  GET  /api/agent/verifications/<thread>/files/<name>  (allowlisted artifacts)
  POST /api/agent/verifications/start       {"paper_id","claim_id"} (validated refs only)
  GET  /api/agent/audits                    (Milestone 6.1: attached audits)
  POST /api/agent/audits/start              {"paper_id","claim_id","external?"} (refs only)

The verifications/start endpoint accepts only validated project/claim
references — never arbitrary code. Bound to localhost for the hackathon demo.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJECT_DIR: Path


def _send(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchOSAgent/5.0"

    def log_message(self, *args):  # noqa: ANN002 - stdlib signature
        sys.stderr.write("[agent-server] %s\n" % args[1])

    def do_OPTIONS(self):  # noqa: N802 - stdlib name
        _send(self, 204, {})

    def do_GET(self):  # noqa: N802 - stdlib name
        from agent_graph import list_run_records, load_run_record, read_telemetry, telemetry_summary

        try:
            path = urlparse(self.path).path
            if path == "/api/agent/runs":
                runs = [
                    {
                        "thread_id": r.thread_id,
                        "kind": r.kind,
                        "paper_id": r.paper_id,
                        "claim_id": r.claim_id,
                        "status": r.status,
                        "updated_at": r.updated_at,
                        "synthetic": r.synthetic,
                    }
                    for r in list_run_records(PROJECT_DIR)
                ]
                _send(self, 200, {"runs": runs})
            elif path == "/api/agent/verifications":
                from project import load_project

                try:
                    project = load_project(PROJECT_DIR)
                except FileNotFoundError:
                    _send(self, 404, {"error": "no project"})
                    return
                _send(self, 200, {"verifications": [v.model_dump() for v in project.verifications]})
            elif path == "/api/agent/audits":
                from project import load_project

                try:
                    project = load_project(PROJECT_DIR)
                except FileNotFoundError:
                    _send(self, 404, {"error": "no project"})
                    return
                _send(self, 200, {"audits": [a.model_dump() for a in project.audits]})
            elif path.startswith("/api/agent/verifications/"):
                # Live artifact download: /api/agent/verifications/<thread>/files/<name>
                # with a strict filename allowlist (never arbitrary paths).
                rest = path[len("/api/agent/verifications/"):]
                parts = rest.split("/")
                if len(parts) == 3 and parts[1] == "files":
                    self._serve_verify_file(parts[0], parts[2])
                else:
                    _send(self, 404, {"error": "not found"})
                return
            elif path.startswith("/api/agent/runs/"):
                thread_id = path[len("/api/agent/runs/"):]
                try:
                    record = load_run_record(PROJECT_DIR, thread_id)
                except FileNotFoundError:
                    _send(self, 404, {"error": f"unknown thread {thread_id}"})
                    return
                _send(self, 200, record.model_dump())
            elif path == "/api/agent/telemetry":
                rows = read_telemetry(PROJECT_DIR)
                _send(self, 200, {"summary": telemetry_summary(PROJECT_DIR),
                                  "recent": rows[-20:]})
            else:
                _send(self, 404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - server must stay up; report JSON error
            traceback.print_exc()
            _send(self, 500, {"error": f"{type(e).__name__}: {e}"})

    def _serve_verify_file(self, thread_id: str, name: str) -> None:
        from verify_graph import verify_thread_dir

        ALLOWED = {
            "experiment.py": "text/plain; charset=utf-8",
            "results.csv": "text/csv; charset=utf-8",
            "scaling.png": "image/png",
            "stdout.txt": "text/plain; charset=utf-8",
            "stderr.txt": "text/plain; charset=utf-8",
            "evidence.json": "application/json",
        }
        if name not in ALLOWED:
            _send(self, 404, {"error": "not found"})
            return
        tdir = verify_thread_dir(PROJECT_DIR, thread_id)
        if name == "evidence.json":
            target = tdir / name
        else:
            # Artifacts live in per-attempt dirs; serve from the latest one.
            attempts = sorted(tdir.glob("attempt-*")) if tdir.is_dir() else []
            target = attempts[-1] / name if attempts else tdir / name
        if not target.is_file():
            _send(self, 404, {"error": "artifact not available yet"})
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ALLOWED[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - stdlib name
        from agent import resume_audit, resume_investigation, resume_verification
        from agent_graph import load_run_record as _load_record

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _send(self, 400, {"error": "invalid JSON body"})
                return
            path = urlparse(self.path).path
            if path.startswith("/api/agent/runs/") and path.endswith("/review"):
                thread_id = path[len("/api/agent/runs/"):-len("/review")]
                choice = payload.get("choice", "")
                if not isinstance(choice, str) or not choice.strip():
                    _send(self, 400, {"error": "choice is required"})
                    return
                try:
                    kind = _load_record(PROJECT_DIR, thread_id).kind or "investigate"
                except FileNotFoundError:
                    _send(self, 404, {"error": f"unknown thread {thread_id}"})
                    return
                try:
                    if kind == "verify":
                        resume = resume_verification
                    elif kind == "audit":
                        resume = resume_audit
                    else:
                        resume = resume_investigation
                    record = resume(
                        PROJECT_DIR, thread_id, choice,
                        str(payload.get("free_text", "") or ""),
                    )
                except FileNotFoundError:
                    _send(self, 404, {"error": f"unknown thread {thread_id}"})
                    return
                except ValueError as e:
                    _send(self, 409, {"error": str(e)})
                    return
                _send(self, 200, record.model_dump())
            elif path == "/api/agent/investigate":
                for key in ("paper_id", "pdf", "analysis", "localized"):
                    if not payload.get(key):
                        _send(self, 400, {"error": f"{key} is required"})
                        return
                thread = threading.Thread(
                    target=_run_investigation, args=(dict(payload),), daemon=True
                )
                thread.start()
                _send(self, 202, {"status": "started"})
            elif path == "/api/agent/verifications/start":
                from agent import validate_claim_ref
                from verify_graph import new_verify_thread_id

                paper_id = payload.get("paper_id", "")
                claim_id = payload.get("claim_id", "")
                if not isinstance(paper_id, str) or not isinstance(claim_id, str):
                    _send(self, 400, {"error": "paper_id and claim_id must be strings"})
                    return
                try:
                    validate_claim_ref(PROJECT_DIR, paper_id, claim_id)
                except ValueError as e:
                    _send(self, 400, {"error": str(e)})
                    return
                thread_id = new_verify_thread_id(
                    PROJECT_DIR.resolve().name, paper_id, claim_id)
                thread = threading.Thread(
                    target=_run_verification,
                    args=(paper_id, claim_id, thread_id), daemon=True,
                )
                thread.start()
                _send(self, 202, {"status": "started", "thread_id": thread_id})
            elif path == "/api/agent/audits/start":
                from agent import validate_claim_ref
                from audit_graph import new_audit_thread_id

                paper_id = payload.get("paper_id", "")
                claim_id = payload.get("claim_id", "")
                external = payload.get("external", False)
                if not isinstance(paper_id, str) or not isinstance(claim_id, str):
                    _send(self, 400, {"error": "paper_id and claim_id must be strings"})
                    return
                if not isinstance(external, bool):
                    _send(self, 400, {"error": "external must be a boolean"})
                    return
                try:
                    validate_claim_ref(PROJECT_DIR, paper_id, claim_id)
                except ValueError as e:
                    _send(self, 400, {"error": str(e)})
                    return
                thread_id = new_audit_thread_id(
                    PROJECT_DIR.resolve().name, paper_id, claim_id)
                thread = threading.Thread(
                    target=_run_audit,
                    args=(paper_id, claim_id, external, thread_id), daemon=True,
                )
                thread.start()
                _send(self, 202, {"status": "started", "thread_id": thread_id})
            else:
                _send(self, 404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - server must stay up
            traceback.print_exc()
            _send(self, 500, {"error": f"{type(e).__name__}: {e}"})


def _run_investigation(payload: dict) -> None:
    """Background investigate (each thread opens its own SQLite connection)."""
    import argparse as _ap

    from agent import cmd_investigate

    args = _ap.Namespace(
        project_dir=str(PROJECT_DIR),
        paper_id=payload["paper_id"],
        pdf=payload["pdf"],
        analysis=payload["analysis"],
        localized=payload["localized"],
        thread_id=None,
    )
    try:
        cmd_investigate(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def _run_verification(paper_id: str, claim_id: str, thread_id: str) -> None:
    """Background verify-claim (own SQLite connection; validated refs only)."""
    import argparse as _ap

    from agent import cmd_verify_claim

    args = _ap.Namespace(
        project_dir=str(PROJECT_DIR),
        paper_id=paper_id,
        claim_id=claim_id,
        thread_id=thread_id,
    )
    try:
        cmd_verify_claim(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def _run_audit(paper_id: str, claim_id: str, external: bool, thread_id: str) -> None:
    """Background audit-claim (own SQLite connection; validated refs only)."""
    import argparse as _ap

    from agent import cmd_audit_claim

    args = _ap.Namespace(
        project_dir=str(PROJECT_DIR),
        paper_id=paper_id,
        claim_id=claim_id,
        thread_id=thread_id,
        external=external,
    )
    try:
        cmd_audit_claim(args)
    except Exception:  # noqa: BLE001 - background job; traceback is the record
        traceback.print_exc()


def serve(project_dir: Path, port: int) -> None:
    global PROJECT_DIR
    PROJECT_DIR = project_dir
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[agent-server] serving {project_dir} on http://127.0.0.1:{port}",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--project-dir", required=True)
    p.add_argument("--port", default="8765")
    ns = p.parse_args()
    serve(Path(ns.project_dir), int(ns.port))
