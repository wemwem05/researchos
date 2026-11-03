#!/usr/bin/env python3
"""ResearchOS Milestone 6: Autonomous Computational Verification subgraph.

A researcher-selected scientific claim flows through a bounded LangGraph
workflow that plans an experiment (Spark Open), generates code (Spark Open),
executes it in an isolated Modal Sandbox (never on the host), validates the
output deterministically, repairs failures (Spark Open, max 3 attempts), and
attaches a grounded evidence report to the Research Project.

Topology:

  select_claim -> classify_testability -> experiment_planner -> validate_plan
    -> generate_code -> code_preflight -> modal_execute -> validate_result
    -> interpret_result | diagnose_and_repair (<=3) -> evidence_report
    -> attach_to_project -> END

Human review (durable interrupt/resume, same pattern as Milestone 5)
triggers for: over-budget plans, unsafe code, or unavailable data.
Nothing runs automatically on paper upload; the researcher starts
verification explicitly per claim.

Scientific honesty rules enforced here (not just prompted):
- successful execution alone can never yield "supported"
- "supported" requires a valid execution AND every evaluated criterion met
- criteria failures clamp to inconclusive / not_supported deterministically
- the report separates observed measurements from model interpretation
- there is no universal "paper reproduced" status
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from agent_graph import (
    HumanReviewRequest,
    HumanReviewResponse,
    RunRecord,
    agent_dir,
    load_run_record,
    record_telemetry,
    record_transition,
    save_run_record,
)
from analyze_paper import AnalysisResult, extract_json
from modal_exec import ModalError, ModalRun
from project import PaperEntry, VerificationEvidence, load_project, save_project
from spark_open import SparkOpenError, TelemetryRecord, utc_now_iso

SCHEMA_VERSION = "m6-1"

# Bounded automatic repair of code/execution failures.
MAX_REPAIRS = 3

# Bounded plan repair (mirrors the M5 validation-repair pattern).
MAX_PLAN_REPAIRS = 1

# Code generation/repair budget. Spark Open is reasoning-heavy and a full
# experiment file is long; smaller budgets truncate the JSON mid-file.
CODE_TOKENS = 32000

# Deterministic budget caps. Plans exceeding these escalate to the researcher,
# who may approve a capped version or abort. Single constants block.
MAX_INPUT_N = 8192
MAX_N_SIZES = 6
MAX_REPS = 10
MAX_PROJECTIONS = 2000
MAX_TOTAL_CELLS = 200  # len(sizes) * len(algorithms) * reps

# Code contract: exact CSV schema the generated experiment must write.
REQUIRED_CSV_COLUMNS = ("n", "algorithm", "rep", "runtime_s", "mem_bytes")
RESULT_JSON_PREFIX = "RESULT_JSON:"

# Import allowlist for generated code (defense-in-depth; the real boundary
# is sandbox isolation — this code never executes on the host).
ALLOWED_IMPORTS = frozenset({
    "numpy", "matplotlib",
    "math", "statistics", "json", "csv", "time", "random", "tracemalloc",
    "platform", "sys", "os", "pathlib", "io", "dataclasses", "typing",
    "collections", "itertools", "functools", "hashlib",
})
BANNED_PATTERNS = (
    "socket", "urllib", "requests", "httpx", "subprocess", "shutil",
    "os.system", "os.popen", "eval(", "exec(", "__import__", "open(",
    "pip install", "pip_install", "MODAL_", "BASETEN_", "MODEL_API_KEY",
    "TOKEN", "SECRET", "API_KEY",
)

VerificationStatus = Literal[
    "supported", "not_supported", "inconclusive", "execution_failed", "not_testable",
]

STATUS_LABELS: dict[str, str] = {
    "supported": "supported in tested conditions",
    "not_supported": "not supported in tested conditions",
    "inconclusive": "inconclusive",
    "execution_failed": "execution failed",
    "not_testable": "not computationally testable",
}


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class VerificationState(TypedDict):
    project_id: str
    thread_id: str
    research_question: str
    paper_id: str
    claim_id: str
    claim_statement: str
    claim_evidence: str
    object_refs: list[str]
    paper_brief: str
    testability: Optional[dict[str, Any]]
    plan: Optional[dict[str, Any]]
    plan_valid: bool
    plan_errors: list[str]
    plan_repairs: int
    data_limitation: str
    code: str
    code_sha256: str
    preflight_errors: list[str]
    attempt_index: int
    modal_meta: Optional[dict[str, Any]]
    exec_valid: bool
    exec_errors: list[str]
    sanity_failed: list[str]
    repair_count: int
    repair_history: list[dict[str, Any]]
    result_summary: Optional[dict[str, Any]]
    criteria: list[dict[str, Any]]
    interpretation: Optional[dict[str, Any]]
    evidence: Optional[dict[str, Any]]
    escalation_kind: str
    needs_human_review: bool
    human_review_request: Optional[dict[str, Any]]
    human_review_response: Optional[dict[str, Any]]
    status: str
    decision: str
    telemetry: list[dict[str, Any]]
    run_error: Optional[str]


def initial_verify_state(project_id: str, thread_id: str, paper_id: str, claim_id: str) -> VerificationState:
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
        "testability": None,
        "plan": None,
        "plan_valid": False,
        "plan_errors": [],
        "plan_repairs": 0,
        "data_limitation": "",
        "code": "",
        "code_sha256": "",
        "preflight_errors": [],
        "attempt_index": 0,
        "modal_meta": None,
        "exec_valid": False,
        "exec_errors": [],
        "sanity_failed": [],
        "repair_count": 0,
        "repair_history": [],
        "result_summary": None,
        "criteria": [],
        "interpretation": None,
        "evidence": None,
        "escalation_kind": "",
        "needs_human_review": False,
        "human_review_request": None,
        "human_review_response": None,
        "status": "running",
        "decision": "",
        "telemetry": [],
        "run_error": None,
    }


# ---------------------------------------------------------------------------
# Structured model I/O
# ---------------------------------------------------------------------------


class TestabilityVerdict(BaseModel):
    testable: bool
    rationale: str = ""
    suggested_benchmark: str = ""
    needs_unavailable_data: bool = False
    unavailable_detail: str = ""


class ExperimentParams(BaseModel):
    sizes: list[int] = Field(default_factory=list)
    dim: int = 3
    n_projections: int = 100
    reps: int = 5
    seeds: list[int] = Field(default_factory=list)
    warmup_reps: int = 1
    algorithms: list[str] = Field(default_factory=lambda: ["sliced_wasserstein", "sinkhorn"])
    sinkhorn_reg: float = 0.1
    sinkhorn_max_iter: int = 500
    sinkhorn_tol: float = 1e-6


class SuccessCriterion(BaseModel):
    name: str
    kind: Literal["runtime_comparison", "sanity", "scaling", "custom"] = "custom"
    rule: str = ""
    metric: str = "runtime_s"
    algorithm_a: str = ""
    algorithm_b: str = ""
    at_n: int = 0
    op: Literal["lt", "gt"] = "lt"
    stat: Literal["median", "mean", "min"] = "median"


class ExperimentBudget(BaseModel):
    max_input_n: int = 0
    est_seconds: int = 0
    note: str = ""


class ExperimentPlan(BaseModel):
    claim: str = ""
    paper_refs: list[str] = Field(default_factory=list)
    object_refs: list[str] = Field(default_factory=list)
    hypothesis: str = ""
    independent_vars: list[str] = Field(default_factory=list)
    dependent_vars: list[str] = Field(default_factory=list)
    controlled_vars: list[str] = Field(default_factory=list)
    algorithms: list[str] = Field(default_factory=list)
    params: ExperimentParams = Field(default_factory=ExperimentParams)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    budget: ExperimentBudget = Field(default_factory=ExperimentBudget)
    limitations: list[str] = Field(default_factory=list)
    scope_notes: str = ""


class GeneratedCode(BaseModel):
    code: str = ""
    notes: str = ""
    expected_artifacts: list[str] = Field(default_factory=lambda: ["results.csv", "scaling.png"])


class RepairDiagnosis(BaseModel):
    diagnosis: str = ""
    fix_strategy: str = ""
    code: str = ""
    notes: str = ""


class Interpretation(BaseModel):
    status: VerificationStatus = "inconclusive"
    observed: list[str] = Field(default_factory=list)
    supported_conclusions: list[str] = Field(default_factory=list)
    partial: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scaling_comment: str = ""


# ---------------------------------------------------------------------------
# Verification artifact layout (outside ephemeral sandbox state)
# ---------------------------------------------------------------------------


def verifications_dir(project_dir: Path) -> Path:
    return agent_dir(project_dir) / "verifications"


def verify_thread_dir(project_dir: Path, thread_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)
    return verifications_dir(project_dir) / safe


def attempt_dir(project_dir: Path, thread_id: str, attempt: int) -> Path:
    d = verify_thread_dir(project_dir, thread_id) / f"attempt-{attempt}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_verify_thread_id(project_id: str, paper_id: str, claim_id: str) -> str:
    import uuid as _uuid

    return f"{project_id}--verify--{paper_id}--{claim_id}--{_uuid.uuid4().hex[:8]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Spark Open prompts (consequential reasoning; gates stay deterministic)
# ---------------------------------------------------------------------------


CLASSIFY_SYSTEM_PROMPT = """You are the ResearchOS testability classifier. Given ONE scientific \
claim plus a paper brief, decide whether the claim's computational aspect can \
be tested by a small CPU experiment on synthetic data in under ten minutes. \
Benchmark-style efficiency/motivation claims usually can; claims requiring \
trained models, real datasets you cannot download, or human judgment cannot. \
If the claim needs unavailable real data but a related computational aspect \
(e.g. an efficiency motivation) could be benchmarked on synthetic data, set \
needs_unavailable_data=true and describe that proxy in suggested_benchmark; \
the researcher will decide whether the proxy is worthwhile. Return ONE JSON \
object: testable (bool), rationale, suggested_benchmark, \
needs_unavailable_data (bool), unavailable_detail. JSON only, no commentary."""

PLANNER_SYSTEM_PROMPT = """You are the ResearchOS experiment planner. Design a small, rigorous, \
CPU-only benchmark that tests the stated claim's computational aspect on \
synthetic data. Return ONE JSON object with: claim, paper_refs, object_refs, \
hypothesis, independent_vars, dependent_vars, controlled_vars, algorithms, \
params {sizes, dim, n_projections, reps, seeds, warmup_reps, algorithms, \
sinkhorn_reg, sinkhorn_max_iter, sinkhorn_tol}, success_criteria [{name, \
kind, rule, metric, algorithm_a, algorithm_b, at_n, op, stat}], budget \
{max_input_n, est_seconds, note}, limitations, scope_notes.

Rigor rules (mandatory):
- fixed random seeds; explicit sizes, dim, projection counts; documented \
Sinkhorn regularization, max iterations, and stopping tolerance; multiple \
repetitions plus warm-up runs; runtime DISTRIBUTIONS (median/IQR over reps), \
never a single timing as proof.
- success_criteria must include at least one runtime_comparison (median \
runtime_s at a size both algorithms run) and one sanity criterion.
- cost awareness: estimate total work BEFORE choosing sizes. Quadratic \
baselines (Sinkhorn: O(n^2) memory, O(n^2 x iters) time) need a modest max_n \
(<= 2048 suggested); near-linear sliced methods may go larger. The whole \
experiment including warm-ups must finish comfortably inside a 10-minute \
sandbox run; when in doubt choose fewer and smaller.
- State hardware/runtime provenance expectations and numerical sanity checks.
- Compare observed scaling descriptively; NEVER claim a proven asymptotic \
complexity from a few sizes.
- Sliced Wasserstein and entropy-regularized OT are DIFFERENT objectives: \
measuring one's runtime says nothing about the other's accuracy. Say what \
this experiment does NOT test in scope_notes and limitations.
JSON only, no commentary."""

CODEGEN_SYSTEM_PROMPT = """You are the ResearchOS experiment coder. Write ONE self-contained Python \
file implementing the given experiment plan. Return ONE JSON object: {code, \
notes, expected_artifacts}.

Hard contract (validation rejects violations):
- imports ONLY from: numpy, matplotlib, math, statistics, json, csv, time, \
random, tracemalloc, platform, sys, os, pathlib, io, dataclasses, typing, \
collections, itertools, functools, hashlib. No other imports, no pip, no \
network, no subprocess, no file reads outside /work, no secrets.
- fixed seeds from params; warm-up reps BEFORE timed reps; time each \
(algorithm, n, rep) with time.perf_counter; tracemalloc peak per measurement \
(labeled mem_bytes).
- implement Sinkhorn and sliced Wasserstein with NUMPY ONLY: do NOT import \
scipy, ot, sklearn, pandas, or anything outside the allowlist.
- NEVER call open(): write /work/results.csv with numpy.savetxt (EXACT \
columns n,algorithm,rep,runtime_s,mem_bytes via fmt='%s' on a string array, \
with header=... and comments='') and /work/scaling.png with \
matplotlib.pyplot.savefig. Algorithms labeled EXACTLY as in params.algorithms.
- scaling.png: log-log runtime-vs-n plot with median + IQR bands \
per algorithm, titled with seeds/dim/projections.
- print EXACTLY one line starting with "RESULT_JSON:" followed by JSON: \
{sanity: {check_name: bool}, config_echo: {sizes, dim, n_projections, reps, \
seeds, sinkhorn_reg, sinkhorn_max_iter, sinkhorn_tol}, notes}.
- sanity checks must include: nonnegative distances, self-distance ~ 0, \
symmetry spot-check, Sinkhorn convergence flags and marginal sums.
- deterministic: same seeds => same CSV (no wall-clock or process-dependent \
values in outputs).
- keep the file focused (~250-400 lines, terse comments): the response has a \
finite token budget and truncation fails validation.
JSON only, no commentary."""

DIAGNOSE_SYSTEM_PROMPT = """You are the ResearchOS failure diagnostician. Given the experiment plan, \
the failing code, and the failure evidence (preflight errors OR sandbox \
stdout/stderr/exit/missing artifacts), return ONE JSON object: {diagnosis, \
fix_strategy, code, notes} with the FULL corrected file. Obey the same hard \
contract as the original coder (imports, CSV schema, RESULT_JSON line, \
artifacts, determinism). Fix only what failed; keep the science identical. \
Reminder: numpy-only algorithms (no scipy/ot/sklearn/pandas), never call \
open() (numpy.savetxt + pyplot.savefig). Keep the file focused (~250-400 \
lines): truncation fails validation. \
Never invent measurements. JSON only, no commentary."""

INTERPRET_SYSTEM_PROMPT = """You are the ResearchOS scientific interpreter. Given the plan, the \
deterministic criteria evaluation, and the measured summary statistics, \
return ONE JSON object: {status, observed, supported_conclusions, partial, \
out_of_scope, limitations, scaling_comment}. status is one of: supported, \
not_supported, inconclusive, execution_failed, not_testable.

Honesty rules (mandatory, and deterministically re-checked):
- observed[]: ONLY directly measured numbers (cite medians/IQRs/ratios).
- supported requires EVERY evaluated criterion met; if any criterion is \
unmet or unevaluated, choose not_supported or inconclusive.
- If the evidence cannot decide the hypothesis, report inconclusive even \
though execution passed.
- partial[]: what is only partly tested; out_of_scope[]: what this \
experiment says nothing about (e.g. accuracy, real data, full paper \
reproduction).
- Describe observed scaling without claiming proven asymptotics. State that \
sliced Wasserstein and entropic OT are different objectives where relevant.
JSON only, no commentary."""


def build_classify_text(claim: str, evidence: str, paper_brief: str) -> str:
    return (
        f"Claim under test: {claim}\n"
        f"Claim evidence: {evidence}\n\n"
        f"PAPER BRIEF:\n{paper_brief}\n\n"
        "Return the testability verdict."
    )


def build_planner_text(
    claim: str, evidence: str, object_refs: list[str], paper_brief: str, data_limitation: str
) -> str:
    limit = f"\nResearcher note (record as a limitation): {data_limitation}\n" if data_limitation else ""
    return (
        f"Claim under test: {claim}\n"
        f"Claim evidence: {evidence}\n"
        f"Grounded object refs: {object_refs}\n"
        f"{limit}\n"
        f"PAPER BRIEF:\n{paper_brief}\n\n"
        "Return the experiment plan."
    )


def build_codegen_text(plan_json: str) -> str:
    return f"EXPERIMENT PLAN (JSON):\n{plan_json}\n\nReturn the experiment file."


def build_diagnose_text(plan_json: str, code: str, failure: str) -> str:
    return (
        f"EXPERIMENT PLAN (JSON):\n{plan_json}\n\n"
        f"FAILING CODE:\n{code}\n\n"
        f"FAILURE EVIDENCE:\n{failure}\n\n"
        "Return the diagnosis and corrected file."
    )


def build_interpret_text(plan_json: str, criteria_json: str, summary_json: str) -> str:
    return (
        f"EXPERIMENT PLAN (JSON):\n{plan_json}\n\n"
        f"DETERMINISTIC CRITERIA EVALUATION (JSON):\n{criteria_json}\n\n"
        f"MEASURED SUMMARY STATISTICS (JSON):\n{summary_json}\n\n"
        "Return the interpretation."
    )


# ---------------------------------------------------------------------------
# Deterministic validators
# ---------------------------------------------------------------------------


def validate_plan(plan: Any) -> tuple[list[str], bool]:
    """Returns (errors, over_budget). Errors block; over_budget escalates."""
    errors: list[str] = []
    if isinstance(plan, BaseModel):
        plan = plan.model_dump()
    if not isinstance(plan, dict):
        return ["malformed plan: not a JSON object"], False
    params = plan.get("params", {})
    if not isinstance(params, dict):
        return ["malformed plan: params not an object"], False
    sizes = params.get("sizes", [])
    if not isinstance(sizes, list) or not sizes or any(not isinstance(n, int) or n <= 0 for n in sizes):
        errors.append("params.sizes must be a non-empty list of positive ints")
        sizes = []
    for key in ("dim", "n_projections", "reps"):
        v = params.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            errors.append(f"params.{key} must be a positive int")
    seeds = params.get("seeds", [])
    if not isinstance(seeds, list) or not seeds or any(not isinstance(s, int) for s in seeds):
        errors.append("params.seeds must be a non-empty list of ints (fixed seeds required)")
    algos = params.get("algorithms", [])
    if not isinstance(algos, list) or len(algos) < 2 or any(not isinstance(a, str) for a in algos):
        errors.append("params.algorithms must list >=2 algorithm labels")
    for key in ("sinkhorn_reg", "sinkhorn_max_iter", "sinkhorn_tol"):
        if params.get(key) is None:
            errors.append(f"params.{key} must be documented (no silent defaults)")
    criteria = plan.get("success_criteria", [])
    if not isinstance(criteria, list) or not criteria:
        errors.append("success_criteria must be a non-empty list")
        criteria = []
    kinds = set()
    for i, c in enumerate(criteria):
        if not isinstance(c, dict) or not c.get("name") or not c.get("rule"):
            errors.append(f"success_criteria[{i}]: needs name + rule")
            continue
        kinds.add(c.get("kind", "custom"))
        if c.get("kind") == "runtime_comparison":
            if c.get("algorithm_a") not in algos or c.get("algorithm_b") not in algos:
                errors.append(f"success_criteria[{i}]: algorithms must match params.algorithms")
            if c.get("at_n") not in sizes:
                errors.append(f"success_criteria[{i}]: at_n must be one of params.sizes")
            if c.get("op") not in ("lt", "gt") or c.get("stat") not in ("median", "mean", "min"):
                errors.append(f"success_criteria[{i}]: op must be lt/gt, stat median/mean/min")
    if "runtime_comparison" not in kinds:
        errors.append("success_criteria must include a runtime_comparison")
    if "sanity" not in kinds:
        errors.append("success_criteria must include a sanity criterion")
    if errors:
        return errors, False
    over = (
        max(sizes) > MAX_INPUT_N
        or len(sizes) > MAX_N_SIZES
        or params["reps"] > MAX_REPS
        or params["n_projections"] > MAX_PROJECTIONS
        or len(sizes) * len(algos) * params["reps"] > MAX_TOTAL_CELLS
    )
    return [], over


def clamp_plan_to_budget(plan: dict[str, Any]) -> dict[str, Any]:
    """Deterministic budget clamp applied only after researcher approval."""
    import copy

    capped = copy.deepcopy(plan)
    p = capped["params"]
    p["sizes"] = sorted(p["sizes"])[:MAX_N_SIZES]
    p["sizes"] = [min(n, MAX_INPUT_N) for n in p["sizes"]]
    p["reps"] = min(p["reps"], MAX_REPS)
    p["n_projections"] = min(p["n_projections"], MAX_PROJECTIONS)
    # Drop runtime_comparison criteria whose at_n no longer exists.
    kept = [c for c in capped["success_criteria"]
            if c.get("kind") != "runtime_comparison" or c.get("at_n") in p["sizes"]]
    if not any(c.get("kind") == "runtime_comparison" for c in kept):
        a, b = p["algorithms"][0], p["algorithms"][1]
        kept.append({
            "name": f"{a} faster than {b} at n={max(p['sizes'])} (median)",
            "kind": "runtime_comparison", "rule": "capped-budget default comparison",
            "metric": "runtime_s", "algorithm_a": a, "algorithm_b": b,
            "at_n": max(p["sizes"]), "op": "lt", "stat": "median",
        })
    capped["success_criteria"] = kept
    capped.setdefault("limitations", []).append(
        "Budget capped after researcher approval "
        f"(sizes={p['sizes']}, reps={p['reps']}, projections={p['n_projections']})."
    )
    return capped


def preflight_code(code: Any) -> list[str]:
    """Static safety gates. AST allowlist is defense-in-depth; the sandbox
    is the real boundary. Returns error strings (empty = pass)."""
    if not isinstance(code, str) or not code.strip():
        return ["empty code"]
    errors: list[str] = []
    import ast as _ast

    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                if (a.name or "").split(".")[0] not in ALLOWED_IMPORTS:
                    errors.append(f"banned import: {a.name}")
        elif isinstance(node, _ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                errors.append(f"banned import: {node.module}")
    for pat in BANNED_PATTERNS:
        if pat in code:
            errors.append(f"banned pattern: {pat}")
    return errors


def extract_result_json(stdout: str) -> tuple[Optional[dict[str, Any]], str]:
    for line in (stdout or "").splitlines():
        if line.strip().startswith(RESULT_JSON_PREFIX):
            payload = line.strip()[len(RESULT_JSON_PREFIX):].strip()
            try:
                doc = json.loads(extract_json(payload))
            except Exception as e:  # noqa: BLE001 - reported as validation error
                return None, f"RESULT_JSON malformed: {e}"
            if not isinstance(doc, dict):
                return None, "RESULT_JSON must be an object"
            return doc, ""
    return None, f"stdout lacks a '{RESULT_JSON_PREFIX.strip()}' line"


def read_csv_rows(csv_bytes: bytes) -> tuple[list[dict[str, str]], str]:
    try:
        text = csv_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return [], f"results.csv not UTF-8: {e}"
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as e:  # noqa: BLE001
        return [], f"results.csv unparsable: {e}"
    return rows, ""


def _finite_positive(value: str) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def validate_result_contract(
    plan: dict[str, Any],
    returncode: int,
    timed_out: bool,
    stdout: str,
    csv_bytes: Optional[bytes],
    plot_bytes: Optional[bytes],
) -> tuple[list[str], list[str], Optional[dict[str, Any]], list[dict[str, str]]]:
    """Deterministic execution gates. Returns (errors, sanity_failed,
    result_json, csv_rows).

    Contract errors mean the execution is unusable (repair or
    execution_failed). Failed sanity flags are NOT contract errors: the
    measurements stand and route to interpretation (which can report at
    most inconclusive/not_supported), because a flag may record a genuine
    numerical finding rather than a code bug.
    """
    errors: list[str] = []
    sanity_failed: list[str] = []
    if timed_out:
        errors.append("execution timed out in the sandbox")
    if returncode != 0:
        errors.append(f"experiment exited with code {returncode}")
    result_json, err = extract_result_json(stdout)
    if err:
        errors.append(err)
        result_json = None
    rows: list[dict[str, str]] = []
    if csv_bytes is None:
        errors.append("missing artifact: results.csv")
    else:
        rows, err = read_csv_rows(csv_bytes)
        if err:
            errors.append(err)
            rows = []
    if plot_bytes is None or len(plot_bytes) == 0:
        errors.append("missing artifact: scaling.png (empty or absent)")
    if errors:
        return errors, sanity_failed, result_json, rows

    params = plan.get("params", {})
    sizes = params.get("sizes", [])
    algos = params.get("algorithms", [])
    reps = params.get("reps", 0)
    assert rows and rows[0] is not None
    header = set(rows[0].keys())
    missing_cols = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
    if missing_cols:
        errors.append(f"results.csv missing columns: {missing_cols}")
        return errors, result_json, rows
    expected = len(sizes) * len(algos) * reps
    if len(rows) != expected:
        errors.append(f"results.csv has {len(rows)} rows, expected {expected} "
                      f"({len(sizes)} sizes x {len(algos)} algorithms x {reps} reps)")
    seen_labels = sorted({r.get("algorithm", "") for r in rows})
    if seen_labels != sorted(algos):
        errors.append(f"results.csv labels {seen_labels} != plan {sorted(algos)}")
    try:
        seen_n = sorted({int(float(r["n"])) for r in rows})
    except (TypeError, ValueError):
        errors.append("results.csv column 'n' must be integral")
        seen_n = []
    if seen_n and seen_n != sorted(sizes):
        errors.append(f"results.csv sizes {seen_n} != plan {sorted(sizes)}")
    bad_runtime = [i for i, r in enumerate(rows) if not _finite_positive(r.get("runtime_s", ""))]
    if bad_runtime:
        errors.append(f"results.csv has {len(bad_runtime)} non-finite/non-positive runtimes")
    bad_mem = [i for i, r in enumerate(rows)
               if not _finite_positive(r.get("mem_bytes", "")) and r.get("mem_bytes") != "0"]
    if bad_mem:
        errors.append(f"results.csv has {len(bad_mem)} invalid mem_bytes values")
    sanity = (result_json or {}).get("sanity", {})
    if not isinstance(sanity, dict) or not sanity:
        errors.append("RESULT_JSON.sanity must be a non-empty object")
    else:
        sanity_failed = [k for k, v in sanity.items() if v is not True]
    return errors, sanity_failed, result_json, rows


def summarize_measurements(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Medians/IQRs/ratios computed deterministically from raw rows."""
    groups: dict[tuple[str, int], list[float]] = {}
    mem: dict[tuple[str, int], list[float]] = {}
    for r in rows:
        key = (r["algorithm"], int(float(r["n"])))
        groups.setdefault(key, []).append(float(r["runtime_s"]))
        mem.setdefault(key, []).append(float(r["mem_bytes"]))
    cells = {}
    for (algo, n), vals in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        qs = statistics.quantiles(sorted(vals), n=4) if len(vals) >= 2 else [vals[0]] * 3
        m = statistics.median(vals)
        cells[f"{algo}@n={n}"] = {
            "n": len(vals),
            "median_s": m,
            "iqr_s": [qs[0], qs[2]],
            "min_s": min(vals),
            "max_s": max(vals),
            "median_mem_bytes": statistics.median(mem[(algo, n)]),
        }
    ratios = {}
    by_algo: dict[str, dict[int, float]] = {}
    for (algo, n), vals in groups.items():
        by_algo.setdefault(algo, {})[n] = statistics.median(vals)
    for algo, series in by_algo.items():
        ns = sorted(series)
        for i in range(1, len(ns)):
            lo, hi = ns[i - 1], ns[i]
            if series[lo] > 0:
                ratios[f"{algo}: t(n={hi})/t(n={lo})"] = series[hi] / series[lo]
    return {"cells": cells, "median_ratios": ratios,
            "note": "descriptive ratios only; not a proof of asymptotic complexity"}


def evaluate_criteria(
    plan: dict[str, Any], rows: list[dict[str, str]], result_json: dict[str, Any]
) -> list[dict[str, Any]]:
    """Deterministic per-criterion verdicts. Kinds: runtime_comparison /
    sanity evaluate here; scaling is informational; custom is unevaluated."""
    out: list[dict[str, Any]] = []
    for c in plan.get("success_criteria", []):
        kind = c.get("kind", "custom")
        entry: dict[str, Any] = {"name": c.get("name", ""), "kind": kind,
                                 "rule": c.get("rule", ""), "evaluated": False,
                                 "met": False, "detail": ""}
        if kind == "runtime_comparison":
            vals: dict[str, list[float]] = {c["algorithm_a"]: [], c["algorithm_b"]: []}
            for r in rows:
                if int(float(r["n"])) == c["at_n"] and r["algorithm"] in vals:
                    vals[r["algorithm"]].append(float(r["runtime_s"]))
            if not vals[c["algorithm_a"]] or not vals[c["algorithm_b"]]:
                entry["detail"] = "missing measurements for comparison"
            else:
                stat = {"median": statistics.median, "mean": statistics.mean,
                        "min": min}[c.get("stat", "median")]
                va, vb = stat(vals[c["algorithm_a"]]), stat(vals[c["algorithm_b"]])
                met = (va < vb) if c.get("op", "lt") == "lt" else (va > vb)
                entry.update(evaluated=True, met=met,
                             detail=f"{c['stat']} {c['algorithm_a']}={va:.6g}s vs "
                                    f"{c['algorithm_b']}={vb:.6g}s at n={c['at_n']}")
        elif kind == "sanity":
            sanity = (result_json or {}).get("sanity", {})
            failed = [k for k, v in sanity.items() if v is not True]
            entry.update(evaluated=True, met=not failed,
                         detail="all sanity checks passed" if not failed
                         else f"failed: {failed}")
        elif kind == "scaling":
            entry.update(evaluated=True, met=True,
                         detail="scaling ratios recorded descriptively; see summary")
        else:
            entry["detail"] = "custom criterion: not machine-checkable"
        out.append(entry)
    return out


def clamp_status(proposed: str, exec_valid: bool, criteria: list[dict[str, Any]]) -> str:
    """Deterministic honesty clamp. Execution success alone can never yield
    'supported'; unmet/unevaluated criteria cap at inconclusive."""
    if not exec_valid:
        return "execution_failed"
    if proposed == "execution_failed":
        return "execution_failed"
    unevaluated = [c for c in criteria if not c.get("evaluated")]
    unmet = [c for c in criteria if c.get("evaluated") and not c.get("met")]
    if proposed == "supported":
        if unmet or unevaluated:
            return "inconclusive"
        return "supported"
    if proposed == "not_supported":
        return "not_supported"
    return "inconclusive" if proposed not in ("inconclusive", "not_testable") else proposed


# ---------------------------------------------------------------------------
# Runtime context + node helpers
# ---------------------------------------------------------------------------


@dataclass
class VerifyContext:
    project_dir: Path
    analysis: AnalysisResult
    paper: PaperEntry
    claim_id: str
    spark: Any  # SparkOpenClient, or a test double with complete_json()
    modal: Any  # ModalExec, or FakeModal with run_experiment()


def _vctx(config: RunnableConfig) -> VerifyContext:
    return config["configurable"]["ctx"]  # type: ignore[index]


def _vfail(project_dir: Path, thread_id: str, error: str) -> dict[str, Any]:
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


def _vrecord_spark(
    project_dir: Path, thread_id: str, state: VerificationState, tel: TelemetryRecord
) -> dict[str, Any]:
    record_telemetry(project_dir, thread_id, tel)
    return {"telemetry": [*state.get("telemetry", []), tel.model_dump()]}


def _spark_call(
    ctx: VerifyContext, state: VerificationState, messages: list[dict[str, Any]],
    schema: Any, node: str, max_tokens: Optional[int] = None,
) -> tuple[Any, dict[str, Any], Optional[str]]:
    """One Spark call. Returns (parsed_or_None, state_update, error_or_None)."""
    try:
        kwargs: dict[str, Any] = {}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        parsed, tel = ctx.spark.complete_json(
            messages, schema, node=node, thread_id=state["thread_id"], **kwargs)
    except SparkOpenError as e:
        if e.telemetry is not None:
            record_telemetry(ctx.project_dir, state["thread_id"], e.telemetry)
        return None, {}, str(e)
    except TypeError:
        # Test doubles with a minimal complete_json(messages, schema, *, node,
        # thread_id) signature: retry without the token override.
        try:
            parsed, tel = ctx.spark.complete_json(
                messages, schema, node=node, thread_id=state["thread_id"])
        except Exception as e2:  # noqa: BLE001 - fail closed like below
            return None, {}, f"{node} failed: {e2}"
        update = _vrecord_spark(ctx.project_dir, state["thread_id"], state, tel)
        return parsed, update, None
    except Exception as e:  # noqa: BLE001 - fail closed
        return None, {}, f"{node} failed: {e}"
    update = _vrecord_spark(ctx.project_dir, state["thread_id"], state, tel)
    return parsed, update, None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def v_select_claim(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    from project import summarize_analysis

    ctx = _vctx(config)
    project = load_project(ctx.project_dir)
    target = None
    for o in ctx.analysis.objects:
        if o.id == ctx.claim_id and o.type == "claim":
            target = o
            break
    if target is None:
        return _vfail(ctx.project_dir, state["thread_id"],
                      f"claim '{ctx.claim_id}' not found (or not a claim) in this paper's analysis")
    refs = [ctx.claim_id, *(target.related_objects or [])]
    statement = f"{target.title}: {target.what_it_is} {target.claim_supported}".strip()
    record_transition(ctx.project_dir, state["thread_id"], "select_claim", ctx.claim_id)
    return {
        "research_question": project.research_question,
        "claim_statement": statement,
        "claim_evidence": target.evidence or "",
        "object_refs": refs,
        "paper_brief": summarize_analysis(ctx.analysis),
        "status": "running",
    }


def v_classify(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    messages = [
        {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
        {"role": "user", "content": build_classify_text(
            state["claim_statement"], state["claim_evidence"], state["paper_brief"])},
    ]
    verdict, update, error = _spark_call(ctx, state, messages, TestabilityVerdict,
                                         "classify_testability")
    if error:
        return _vfail(ctx.project_dir, state["thread_id"], error)
    assert verdict is not None
    record_transition(ctx.project_dir, state["thread_id"], "classify_testability",
                      f"testable={verdict.testable} needs_data={verdict.needs_unavailable_data}")
    out: dict[str, Any] = {**update, "testability": verdict.model_dump()}
    if verdict.needs_unavailable_data:
        # Escalation takes precedence: the researcher decides whether a
        # synthetic proxy is worthwhile or the run should abort.
        out["decision"] = "escalate_data"
        out["escalation_kind"] = "unavailable_data"
    elif not verdict.testable:
        out["decision"] = "not_testable"
    else:
        out["decision"] = "plan"
    return out


PLAN_REPAIR_PROMPT = """You fix malformed ResearchOS experiment plans. The plan failed \
deterministic validation with the listed errors. Rewrite it as a fully valid \
plan with the same JSON shape, changing as little substance as possible. Keep \
all rigor rules (fixed seeds, reps + warm-up, runtime_comparison + sanity \
criteria, scope honesty). Return JSON only, no commentary."""


def v_plan(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": build_planner_text(
            state["claim_statement"], state["claim_evidence"], state["object_refs"],
            state["paper_brief"], state.get("data_limitation", ""))},
    ]
    plan, update, error = _spark_call(ctx, state, messages, ExperimentPlan,
                                      "experiment_planner")
    if error:
        return _vfail(ctx.project_dir, state["thread_id"], error)
    assert plan is not None
    record_transition(ctx.project_dir, state["thread_id"], "experiment_planner",
                      f"{len(plan.params.sizes)} sizes, {len(plan.success_criteria)} criteria")
    return {**update, "plan": plan.model_dump(), "plan_valid": False}


def v_validate_plan(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    plan = state.get("plan")
    errors, over = validate_plan(plan)
    update: dict[str, Any] = {}
    if errors and state.get("plan_repairs", 0) < MAX_PLAN_REPAIRS:
        messages = [
            {"role": "system", "content": PLAN_REPAIR_PROMPT},
            {"role": "user", "content": (
                f"PLAN (JSON):\n{json.dumps(plan)}\n\nVALIDATION ERRORS:\n"
                + "\n".join(f"- {e}" for e in errors) + "\n\nReturn the corrected plan.")},
        ]
        fixed, repair_update, error = _spark_call(ctx, state, messages, ExperimentPlan,
                                                  "plan_repair")
        update.update(repair_update)
        update["plan_repairs"] = state.get("plan_repairs", 0) + 1
        if error is None and fixed is not None:
            plan = fixed.model_dump()
            update["plan"] = plan
            errors, over = validate_plan(plan)
            record_transition(ctx.project_dir, state["thread_id"], "plan_repair",
                              "1 plan repair used" if not errors else "repair failed")
    if errors:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.validation_failures.extend(errors)
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "validate_plan_failed",
                          f"{len(errors)} errors")
        failed = _vfail(ctx.project_dir, state["thread_id"],
                        "experiment plan invalid: " + "; ".join(errors[:5]))
        return {**update, **failed}
    if over:
        record_transition(ctx.project_dir, state["thread_id"], "validate_plan",
                          "over budget -> escalate")
        return {**update, "plan_valid": True, "plan_errors": [],
                "decision": "escalate_budget", "escalation_kind": "budget"}
    record_transition(ctx.project_dir, state["thread_id"], "validate_plan", "plan valid")
    return {**update, "plan_valid": True, "plan_errors": [], "decision": "codegen"}


def v_codegen(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    messages = [
        {"role": "system", "content": CODEGEN_SYSTEM_PROMPT},
        {"role": "user", "content": build_codegen_text(json.dumps(state.get("plan")))},
    ]
    gen, update, error = _spark_call(ctx, state, messages, GeneratedCode,
                                     "generate_code", max_tokens=CODE_TOKENS)
    if error:
        return _vfail(ctx.project_dir, state["thread_id"], error)
    assert gen is not None
    record_transition(ctx.project_dir, state["thread_id"], "generate_code",
                      f"{len(gen.code)} chars, sha={sha256_text(gen.code)[:12]}")
    return {**update, "code": gen.code, "code_sha256": sha256_text(gen.code)}


def v_preflight(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    errors = preflight_code(state.get("code", ""))
    if not errors:
        record_transition(ctx.project_dir, state["thread_id"], "code_preflight", "pass")
        return {"preflight_errors": [], "decision": "execute"}
    record_transition(ctx.project_dir, state["thread_id"], "code_preflight_failed",
                      f"{len(errors)} errors")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.validation_failures.extend(f"preflight: {e}" for e in errors)
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    if all(e.startswith("syntax error") for e in errors):
        # Syntax slips need no human judgment: bounded auto-repair.
        return {"preflight_errors": errors, "decision": "diagnose"}
    return {"preflight_errors": errors, "decision": "escalate_unsafe",
            "escalation_kind": "unsafe"}


REMOTE_RESULTS_CSV = "/work/results.csv"
REMOTE_PLOT = "/work/scaling.png"


def _write_attempt_files(
    project_dir: Path, thread_id: str, attempt: int, code: str,
    run: Optional[ModalRun], infra_error: str = "",
) -> None:
    d = attempt_dir(project_dir, thread_id, attempt)
    (d / "experiment.py").write_text(code, encoding="utf-8")
    meta: dict[str, Any] = {"attempt": attempt, "code_sha256": sha256_text(code)}
    if run is None:
        meta["infra_error"] = infra_error
    else:
        (d / "stdout.txt").write_text(run.exec_result.stdout, encoding="utf-8")
        (d / "stderr.txt").write_text(run.exec_result.stderr, encoding="utf-8")
        (d / "env.json").write_text(json.dumps(run.env_probe, indent=2), encoding="utf-8")
        for remote, local in ((REMOTE_RESULTS_CSV, "results.csv"), (REMOTE_PLOT, "scaling.png")):
            if remote in run.artifacts:
                (d / local).write_bytes(run.artifacts[remote])
        meta.update({
            "sandbox_id": run.sandbox_id,
            "image": run.image_desc,
            "returncode": run.exec_result.returncode,
            "timed_out": run.exec_result.timed_out,
            "duration_s": round(run.exec_result.duration_s, 3),
            "missing_artifacts": run.missing_artifacts,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
        })
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def v_execute(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    attempt = state.get("attempt_index", 0)
    code = state.get("code", "")
    run: Optional[ModalRun] = None
    infra_error = ""
    # One automatic retry for sandbox-infrastructure failure only
    # (experiment failure is returned normally for diagnosis).
    for _ in range(2):
        try:
            run = ctx.modal.run_experiment(
                code, artifact_paths=[REMOTE_RESULTS_CSV, REMOTE_PLOT],
                thread_id=state["thread_id"],
            )
            infra_error = ""
            break
        except ModalError as e:
            infra_error = str(e)
        except Exception as e:  # noqa: BLE001 - test doubles raise plain errors
            infra_error = f"{type(e).__name__}: {e}"
    _write_attempt_files(ctx.project_dir, state["thread_id"], attempt, code, run, infra_error)
    if run is None:
        record_transition(ctx.project_dir, state["thread_id"], "modal_execute_failed",
                          infra_error[:200])
        return {
            "attempt_index": attempt + 1,
            "modal_meta": {"infra_error": infra_error, "attempt": attempt},
            "exec_valid": False,
            "exec_errors": [f"sandbox infrastructure failure: {infra_error}"],
            "decision": "evidence",
        }
    record_transition(ctx.project_dir, state["thread_id"], "modal_execute",
                      f"attempt {attempt}: rc={run.exec_result.returncode} "
                      f"timeout={run.exec_result.timed_out} {run.exec_result.duration_s:.1f}s")
    return {
        "attempt_index": attempt + 1,
        "modal_meta": {
            "sandbox_id": run.sandbox_id,
            "image": run.image_desc,
            "returncode": run.exec_result.returncode,
            "timed_out": run.exec_result.timed_out,
            "duration_s": round(run.exec_result.duration_s, 3),
            "missing_artifacts": run.missing_artifacts,
            "env_probe": run.env_probe,
            "attempt": attempt,
        },
        "decision": "validate",
    }


def _load_attempt(project_dir: Path, thread_id: str, attempt: int) -> tuple[str, str, Optional[bytes], Optional[bytes]]:
    d = verify_thread_dir(project_dir, thread_id) / f"attempt-{attempt}"
    stdout = (d / "stdout.txt").read_text(encoding="utf-8") if (d / "stdout.txt").is_file() else ""
    stderr = (d / "stderr.txt").read_text(encoding="utf-8") if (d / "stderr.txt").is_file() else ""
    csv_b = (d / "results.csv").read_bytes() if (d / "results.csv").is_file() else None
    png_b = (d / "scaling.png").read_bytes() if (d / "scaling.png").is_file() else None
    return stdout, stderr, csv_b, png_b


def v_validate_result(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    meta = state.get("modal_meta") or {}
    if meta.get("infra_error"):
        return {"exec_valid": False,
                "exec_errors": [f"sandbox infrastructure failure: {meta['infra_error']}"],
                "decision": "evidence"}
    attempt = meta.get("attempt", state.get("attempt_index", 1) - 1)
    stdout, stderr, csv_b, png_b = _load_attempt(ctx.project_dir, state["thread_id"], attempt)
    plan = state.get("plan") or {}
    errors, sanity_failed, result_json, rows = validate_result_contract(
        plan, meta.get("returncode", -1), meta.get("timed_out", False),
        stdout, csv_b, png_b,
    )
    if errors:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.validation_failures.extend(f"result: {e}" for e in errors)
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "validate_result_failed",
                          f"{len(errors)} errors")
        if state.get("repair_count", 0) < MAX_REPAIRS:
            return {"exec_valid": False, "exec_errors": errors, "decision": "diagnose",
                    "result_summary": None}
        record_transition(ctx.project_dir, state["thread_id"], "repair_budget_exhausted",
                          f"{MAX_REPAIRS} attempts used")
        return {"exec_valid": False, "exec_errors": errors, "decision": "evidence",
                "result_summary": None}
    assert result_json is not None
    summary = summarize_measurements(rows)
    criteria = evaluate_criteria(plan, rows, result_json)
    if sanity_failed:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.decisions.append(
                "sanity flags failed but contract holds; routing to interpretation "
                f"as findings (flags: {sanity_failed})")
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
    record_transition(ctx.project_dir, state["thread_id"], "validate_result",
                      f"valid: {len(rows)} rows, {len(criteria)} criteria evaluated"
                      + (f", sanity failed: {sanity_failed}" if sanity_failed else ""))
    return {"exec_valid": True, "exec_errors": [], "sanity_failed": sanity_failed,
            "decision": "interpret", "result_summary": summary, "criteria": criteria}


def v_diagnose(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    if state.get("repair_count", 0) >= MAX_REPAIRS:
        return {"decision": "evidence"}
    meta = state.get("modal_meta") or {}
    attempt = meta.get("attempt", 0)
    stdout, stderr, _, _ = _load_attempt(ctx.project_dir, state["thread_id"], attempt)
    failure_parts = [
        f"preflight errors: {state.get('preflight_errors', [])}",
        f"result errors: {state.get('exec_errors', [])}",
        f"returncode={meta.get('returncode')} timed_out={meta.get('timed_out')}",
        f"--- stdout (tail) ---\n{stdout[-3000:]}",
        f"--- stderr (tail) ---\n{stderr[-3000:]}",
    ]
    messages = [
        {"role": "system", "content": DIAGNOSE_SYSTEM_PROMPT},
        {"role": "user", "content": build_diagnose_text(
            json.dumps(state.get("plan")), state.get("code", ""), "\n".join(failure_parts))},
    ]
    fix, update, error = _spark_call(ctx, state, messages, RepairDiagnosis,
                                     "diagnose_and_repair", max_tokens=CODE_TOKENS)
    if error:
        return _vfail(ctx.project_dir, state["thread_id"], error)
    assert fix is not None
    record_transition(ctx.project_dir, state["thread_id"], "diagnose_and_repair",
                      f"repair {state.get('repair_count', 0) + 1}/{MAX_REPAIRS}: "
                      f"{fix.diagnosis[:150]}")
    history = [*state.get("repair_history", []), {
        "attempt": state.get("repair_count", 0) + 1,
        "diagnosis": fix.diagnosis,
        "fix_strategy": fix.fix_strategy,
        "code_sha256": sha256_text(fix.code),
    }]
    return {**update, "code": fix.code, "code_sha256": sha256_text(fix.code),
            "repair_count": state.get("repair_count", 0) + 1,
            "repair_history": history, "preflight_errors": [], "exec_errors": [],
            "exec_valid": False, "decision": "preflight"}


def v_interpret(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    messages = [
        {"role": "system", "content": INTERPRET_SYSTEM_PROMPT},
        {"role": "user", "content": build_interpret_text(
            json.dumps(state.get("plan")), json.dumps(state.get("criteria")),
            json.dumps(state.get("result_summary")))},
    ]
    interp, update, error = _spark_call(ctx, state, messages, Interpretation,
                                        "interpret_result")
    if error:
        return _vfail(ctx.project_dir, state["thread_id"], error)
    assert interp is not None
    record_transition(ctx.project_dir, state["thread_id"], "interpret_result",
                      f"proposed={interp.status}")
    return {**update, "interpretation": interp.model_dump()}


def _relposix(project_dir: Path, path: Path) -> str:
    return path.relative_to(project_dir).as_posix()


def v_evidence(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    tdir = verify_thread_dir(ctx.project_dir, state["thread_id"])
    tdir.mkdir(parents=True, exist_ok=True)
    testability = state.get("testability") or {}
    if state.get("decision") == "not_testable":
        # Direct route from classify only. Once the researcher approves
        # proceeding past a data escalation, the execution outcome decides.
        status: str = "not_testable"
        exec_valid = False
    else:
        proposed = (state.get("interpretation") or {}).get("status", "inconclusive")
        exec_valid = bool(state.get("exec_valid"))
        status = clamp_status(proposed, exec_valid, state.get("criteria", []))
    meta = state.get("modal_meta") or {}
    attempt = meta.get("attempt", 0)
    adir = tdir / f"attempt-{attempt}"
    stdout, stderr, _, _ = _load_attempt(ctx.project_dir, state["thread_id"], attempt) \
        if exec_valid or (adir / "stdout.txt").is_file() else ("", "", None, None)
    evidence = {
        "id": "",
        "thread_id": state["thread_id"],
        "paper_id": state["paper_id"],
        "claim_id": state["claim_id"],
        "claim_statement": state.get("claim_statement", ""),
        "object_refs": state.get("object_refs", []),
        "status": status,
        "status_label": STATUS_LABELS[status],
        "plan": state.get("plan") or {},
        "testability": testability,
        "code_sha256": state.get("code_sha256", ""),
        "code_path": _relposix(ctx.project_dir, adir / "experiment.py") if (adir / "experiment.py").is_file() else "",
        "params": (state.get("plan") or {}).get("params", {}),
        "environment": {
            "image": meta.get("image", ""),
            "runtime": (meta.get("env_probe") or {}).get("runtime", ""),
            "pip_freeze": (meta.get("env_probe") or {}).get("pip_freeze", ""),
        },
        "measurements_path": _relposix(ctx.project_dir, adir / "results.csv") if (adir / "results.csv").is_file() else "",
        "plot_path": _relposix(ctx.project_dir, adir / "scaling.png") if (adir / "scaling.png").is_file() else "",
        "stdout_path": _relposix(ctx.project_dir, adir / "stdout.txt") if (adir / "stdout.txt").is_file() else "",
        "stderr_path": _relposix(ctx.project_dir, adir / "stderr.txt") if (adir / "stderr.txt").is_file() else "",
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "criteria": state.get("criteria", []),
        "sanity_failed": state.get("sanity_failed", []),
        "result_summary": state.get("result_summary") or {},
        "observed": (state.get("interpretation") or {}).get("observed", []),
        "supported_conclusions": (state.get("interpretation") or {}).get("supported_conclusions", []),
        "partial": (state.get("interpretation") or {}).get("partial", []),
        "out_of_scope": (state.get("interpretation") or {}).get("out_of_scope", []),
        "limitations": (state.get("interpretation") or {}).get("limitations", [])
        + (state.get("plan") or {}).get("limitations", []),
        "scaling_comment": (state.get("interpretation") or {}).get("scaling_comment", ""),
        "repairs": state.get("repair_history", []),
        "attempts": state.get("attempt_index", 0),
        "modal": {
            "sandbox_id": meta.get("sandbox_id", ""),
            "returncode": meta.get("returncode"),
            "timed_out": meta.get("timed_out", False),
            "duration_s": meta.get("duration_s"),
            "infra_error": meta.get("infra_error", ""),
        },
        "telemetry": state.get("telemetry", []),
        "created_at": utc_now_iso(),
    }
    if state.get("data_limitation"):
        evidence["limitations"].append(
            "Researcher-approved data limitation: " + state["data_limitation"])
    (tdir / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.decisions.append(f"evidence_report: status={status} "
                                f"(proposed={(state.get('interpretation') or {}).get('status')})")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "evidence_report",
                      f"status={status}")
    return {"evidence": evidence}


def _next_verify_id(taken: set[str]) -> str:
    i = 1
    while f"v{i}" in taken:
        i += 1
    taken.add(f"v{i}")
    return f"v{i}"


def v_attach(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    from project import VerificationEvidence

    ctx = _vctx(config)
    evidence = state.get("evidence")
    if evidence is None:
        return _vfail(ctx.project_dir, state["thread_id"], "no evidence to attach")
    project = load_project(ctx.project_dir)
    if any(v.thread_id == state["thread_id"] for v in project.verifications):
        # Idempotent: checkpoint resume must not duplicate the attachment.
        record_transition(ctx.project_dir, state["thread_id"], "attach_to_project",
                          "already attached; skipped duplicate")
    else:
        taken = {v.id for v in project.verifications}
        evidence["id"] = _next_verify_id(taken)
        item = VerificationEvidence.model_validate(evidence)
        project.verifications.append(item)
        save_project(ctx.project_dir, project)  # atomic write (tmp + replace)
        tdir = verify_thread_dir(ctx.project_dir, state["thread_id"])
        (tdir / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        record_transition(ctx.project_dir, state["thread_id"], "attach_to_project",
                          f"attached {item.id} status={item.status}")
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.status = "completed"
        record.final_status = "completed"
        record.decisions.append(f"verification complete: {evidence.get('status')}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    return {"status": "completed"}


# ---------------------------------------------------------------------------
# Human review (durable interrupt/resume; checkpointed request first)
# ---------------------------------------------------------------------------


def build_verify_review_request(state: VerificationState) -> HumanReviewRequest:
    kind = state.get("escalation_kind", "")
    plan = state.get("plan") or {}
    params = plan.get("params", {})
    if kind == "budget":
        return HumanReviewRequest(
            kind="verification",
            question=("The planned experiment exceeds the CPU budget caps. Approve a "
                      "deterministically capped version, or abort?"),
            why_stopped=(f"Budget caps exceeded: sizes={params.get('sizes')}, "
                         f"reps={params.get('reps')}, projections={params.get('n_projections')}."),
            investigator_position=f"Run the capped plan: {json.dumps(params)[:500]}",
            critic_concern="Full plan may be too slow/costly for the sandbox limits.",
            evidence_a=json.dumps(params)[:1500],
            evidence_b=(f"Caps: n<={MAX_INPUT_N}, sizes<={MAX_N_SIZES}, reps<={MAX_REPS}, "
                        f"projections<={MAX_PROJECTIONS}, cells<={MAX_TOTAL_CELLS}."),
            finding_refs=[],
            object_refs=state.get("object_refs", []),
            options=["Approve capped experiment", "Abort verification"],
            allow_free_text=True,
        )
    if kind == "unsafe":
        return HumanReviewRequest(
            kind="verification",
            question="The generated code failed the static safety preflight. Abort, or auto-repair and retry (bounded)?",
            why_stopped="; ".join(state.get("preflight_errors", []))[:800],
            investigator_position="Attempt an automatic repair within the remaining budget.",
            critic_concern="Code contains banned imports or patterns; it has NOT been executed.",
            evidence_a="; ".join(state.get("preflight_errors", []))[:1500],
            evidence_b=f"Code sha256: {state.get('code_sha256', '')[:16]}… "
                       f"({len(state.get('code', ''))} chars, never executed)",
            finding_refs=[],
            object_refs=state.get("object_refs", []),
            options=["Abort verification", "Auto-repair and retry"],
            allow_free_text=True,
        )
    detail = (state.get("testability") or {}).get("unavailable_detail", "")
    return HumanReviewRequest(
        kind="verification",
        question=("This claim may need data that is unavailable in the sandbox. Abort, "
                  "or proceed with synthetic data and record the limitation?"),
        why_stopped=detail[:800] or "Classifier flagged unavailable data.",
        investigator_position="Proceed on synthetic data; record the limitation explicitly.",
        critic_concern="Results may not transfer to the paper's real setting.",
        evidence_a=state.get("claim_statement", "")[:1500],
        evidence_b=detail[:1500],
        finding_refs=[],
        object_refs=state.get("object_refs", []),
        options=["Abort verification", "Proceed anyway (record limitation)"],
        allow_free_text=True,
    )


def v_prepare_review(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    request = build_verify_review_request(state)
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.human_review_request = request.model_dump()
        record.decisions.append(
            f"human review requested (verification/{state.get('escalation_kind')}): "
            f"{request.question[:200]}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "human_review_requested",
                      state.get("escalation_kind", ""))
    return {"needs_human_review": True,
            "human_review_request": request.model_dump(),
            "status": "awaiting_human"}


def v_review(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    request = state.get("human_review_request") or {}
    resume_value = interrupt(request)
    response = HumanReviewResponse.model_validate(resume_value)
    if not response.decided_at:
        response.decided_at = utc_now_iso()
    ctx = _vctx(config)
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


def v_apply_decision(state: VerificationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _vctx(config)
    response = state.get("human_review_response") or {}
    choice = str(response.get("choice", ""))
    lowered = choice.lower()
    kind = state.get("escalation_kind", "")
    free_text = str(response.get("free_text", ""))
    if "abort" in lowered or "discard" in lowered:
        try:
            record = load_run_record(ctx.project_dir, state["thread_id"])
            record.status = "discarded"
            record.final_status = "discarded"
            record.decisions.append(f"researcher aborted verification: {choice[:200]}")
            save_run_record(ctx.project_dir, record)
        except FileNotFoundError:
            pass
        record_transition(ctx.project_dir, state["thread_id"], "apply_verify_decision",
                          "aborted by researcher")
        return {"status": "discarded", "decision": "abort"}
    if kind == "budget" and "approve" in lowered:
        capped = clamp_plan_to_budget(state.get("plan") or {})
        record_transition(ctx.project_dir, state["thread_id"], "apply_verify_decision",
                          "budget approved with deterministic caps")
        return {"plan": capped, "plan_valid": True, "decision": "codegen"}
    if kind == "unsafe" and ("repair" in lowered or "retry" in lowered):
        if state.get("repair_count", 0) >= MAX_REPAIRS:
            return {"decision": "evidence"}
        record_transition(ctx.project_dir, state["thread_id"], "apply_verify_decision",
                          "researcher approved auto-repair")
        return {"decision": "diagnose"}
    if kind == "unavailable_data" and "proceed" in lowered:
        note = (state.get("testability") or {}).get("unavailable_detail", "unavailable data")
        if free_text:
            note += f" Researcher note: {free_text[:300]}"
        record_transition(ctx.project_dir, state["thread_id"], "apply_verify_decision",
                          "proceeding with recorded limitation")
        return {"data_limitation": note, "decision": "plan"}
    # Fail-safe: unrecognized choices abort rather than continue blindly.
    try:
        record = load_run_record(ctx.project_dir, state["thread_id"])
        record.status = "discarded"
        record.final_status = "discarded"
        record.decisions.append(f"unrecognized choice treated as abort: {choice[:200]}")
        save_run_record(ctx.project_dir, record)
    except FileNotFoundError:
        pass
    record_transition(ctx.project_dir, state["thread_id"], "apply_verify_decision",
                      "unrecognized choice -> abort")
    return {"status": "discarded", "decision": "abort"}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def _route_after_select(state: VerificationState) -> str:
    return END if state.get("status") == "failed" else "classify_testability"


def _route_after_classify(state: VerificationState) -> str:
    if state.get("status") == "failed":
        return END
    decision = state.get("decision", "")
    if decision == "not_testable":
        return "evidence_report"
    if decision == "escalate_data":
        return "prepare_review"
    return "experiment_planner"


def _route_after_validate_plan(state: VerificationState) -> str:
    if state.get("status") == "failed":
        return END
    if state.get("decision") == "escalate_budget":
        return "prepare_review"
    return "generate_code"


def _route_after_preflight(state: VerificationState) -> str:
    decision = state.get("decision", "")
    if decision == "execute":
        return "modal_execute"
    if decision == "diagnose":
        return "diagnose_and_repair"
    return "prepare_review"


def _route_after_validate_result(state: VerificationState) -> str:
    decision = state.get("decision", "")
    if decision == "interpret":
        return "interpret_result"
    if decision == "diagnose":
        return "diagnose_and_repair"
    return "evidence_report"


def _route_after_apply(state: VerificationState) -> str:
    decision = state.get("decision", "")
    if decision == "codegen":
        return "generate_code"
    if decision == "diagnose":
        return "diagnose_and_repair"
    if decision == "plan":
        return "experiment_planner"
    return END


def _route_pass(next_node: str):
    """Unconditional edge that still respects a failed status."""

    def _route(state: VerificationState) -> str:
        return END if state.get("status") == "failed" else next_node

    return _route


def _route_after_diagnose(state: VerificationState) -> str:
    if state.get("status") == "failed":
        return END
    if state.get("decision") == "evidence":
        return "evidence_report"
    return "code_preflight"


def build_verify_graph(checkpointer: SqliteSaver):
    graph = StateGraph(VerificationState)
    graph.add_node("select_claim", v_select_claim)
    graph.add_node("classify_testability", v_classify)
    graph.add_node("experiment_planner", v_plan)
    graph.add_node("validate_plan", v_validate_plan)
    graph.add_node("generate_code", v_codegen)
    graph.add_node("code_preflight", v_preflight)
    graph.add_node("modal_execute", v_execute)
    graph.add_node("validate_result", v_validate_result)
    graph.add_node("diagnose_and_repair", v_diagnose)
    graph.add_node("interpret_result", v_interpret)
    graph.add_node("evidence_report", v_evidence)
    graph.add_node("attach_to_project", v_attach)
    graph.add_node("prepare_review", v_prepare_review)
    graph.add_node("human_review", v_review)
    graph.add_node("apply_verify_decision", v_apply_decision)

    graph.add_edge(START, "select_claim")
    graph.add_conditional_edges("select_claim", _route_after_select,
                               {"classify_testability": "classify_testability", END: END})
    graph.add_conditional_edges("classify_testability", _route_after_classify, {
        "experiment_planner": "experiment_planner",
        "evidence_report": "evidence_report",
        "prepare_review": "prepare_review",
    })
    graph.add_conditional_edges("experiment_planner", _route_pass("validate_plan"),
                               {"validate_plan": "validate_plan", END: END})
    graph.add_conditional_edges("validate_plan", _route_after_validate_plan, {
        "generate_code": "generate_code", "prepare_review": "prepare_review", END: END})
    graph.add_conditional_edges("generate_code", _route_pass("code_preflight"),
                               {"code_preflight": "code_preflight", END: END})
    graph.add_conditional_edges("code_preflight", _route_after_preflight, {
        "modal_execute": "modal_execute",
        "diagnose_and_repair": "diagnose_and_repair",
        "prepare_review": "prepare_review",
    })
    graph.add_edge("modal_execute", "validate_result")
    graph.add_conditional_edges("validate_result", _route_after_validate_result, {
        "interpret_result": "interpret_result",
        "diagnose_and_repair": "diagnose_and_repair",
        "evidence_report": "evidence_report",
    })
    graph.add_conditional_edges("diagnose_and_repair", _route_after_diagnose, {
        "code_preflight": "code_preflight",
        "evidence_report": "evidence_report",
        END: END,
    })
    graph.add_conditional_edges("interpret_result", _route_pass("evidence_report"),
                               {"evidence_report": "evidence_report", END: END})
    graph.add_edge("evidence_report", "attach_to_project")
    graph.add_edge("prepare_review", "human_review")
    graph.add_edge("human_review", "apply_verify_decision")
    graph.add_conditional_edges("apply_verify_decision", _route_after_apply, {
        "generate_code": "generate_code",
        "diagnose_and_repair": "diagnose_and_repair",
        "experiment_planner": "experiment_planner",
        END: END,
    })
    graph.add_edge("attach_to_project", END)
    return graph.compile(checkpointer=checkpointer)
