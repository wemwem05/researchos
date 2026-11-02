#!/usr/bin/env python3
"""ResearchOS analysis pipeline (Milestones 1 + 3.1).

Clean separation between document inventory, scientific explanation, and
research-question relevance:

  Stage 1 (inventory + explanation, question-independent): the PDF plus a
  deterministic label checklist (see inventory.py) goes to Muse, which must
  inventory EVERY labeled equation/figure/table, explain each, and rate its
  paper_importance. Claims stay selective (major claims only).

  Stage 2 (relevance, cheap text-only call): each inventoried object is
  scored against the user's research question. Levels derive
  deterministically from scores (see relevance.py).

Usage:
    python prototype/analyze_paper.py \
        --pdf samples/eot_shape_correspondence.pdf \
        --question "Can alternative optimal transport costs improve non-rigid shape correspondence?"

    # Re-run only the relevance stage on an existing analysis:
    python prototype/analyze_paper.py \
        --rescore prototype/output.json \
        --question "..." \
        --output prototype/output.json

Output: prototype/output.json (override with --output).

Auth: API key ONLY via the MODEL_API_KEY environment variable (or a
local, never-committed .env file containing it).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from inventory import discover_labels, format_checklist
from relevance import RelevanceLevel, level_for_score

API_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
API_KEY_ENV_VAR = "MODEL_API_KEY"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SymbolMeaning(BaseModel):
    symbol: str
    meaning: str


class EquationDetails(BaseModel):
    latex: Optional[str] = None
    symbols: list[SymbolMeaning] = Field(default_factory=list)
    role_in_method: Optional[str] = None


class FigureDetails(BaseModel):
    components: list[str] = Field(default_factory=list)
    flow: list[str] = Field(default_factory=list)
    role_in_method: Optional[str] = None


class TableDetails(BaseModel):
    metric: Optional[str] = None
    higher_or_lower_is_better: Optional[Literal["higher", "lower"]] = None
    important_comparisons: list[str] = Field(default_factory=list)
    result_interpretation: Optional[str] = None


class SciObjectDraft(BaseModel):
    """Stage 1 output: explanation + paper importance, no research question."""

    id: str
    type: Literal["equation", "figure", "table", "claim"]
    page: Optional[int] = None
    label: Optional[str] = None
    title: str
    what_it_is: str
    why_it_is_here: str
    claim_supported: str
    limitations: str
    paper_importance: float = Field(ge=0.0, le=1.0)
    paper_importance_reason: str
    related_objects: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    equation: Optional[EquationDetails] = None
    figure: Optional[FigureDetails] = None
    table: Optional[TableDetails] = None


class SciObject(SciObjectDraft):
    """Final object: draft plus research-question relevance."""

    research_relevance: str
    research_relevance_score: float = Field(ge=0.0, le=1.0)
    research_relevance_reason: str
    research_relevance_level: RelevanceLevel = "context"

    @model_validator(mode="after")
    def normalize_level(self) -> "SciObject":
        # Single source of truth: the level always derives from the score
        # via the centralized thresholds (whatever the model returned).
        object.__setattr__(self, "research_relevance_level",
                            level_for_score(self.research_relevance_score))
        return self


class RelevanceScore(BaseModel):
    """Stage 2 output item: relevance judgment for one known object id."""

    id: str
    research_relevance: str
    research_relevance_score: float = Field(ge=0.0, le=1.0)
    research_relevance_reason: str


class RelevanceList(BaseModel):
    scores: list[RelevanceScore] = Field(default_factory=list)


REQUIRED_OBJECT_TYPES = ("equation", "figure", "table", "claim")


def _require_all_types(objects: list) -> None:
    present = {o.type for o in objects}
    missing = [t for t in REQUIRED_OBJECT_TYPES if t not in present]
    if missing:
        raise ValueError(
            "objects must include at least one of each required type; "
            f"missing: {missing}"
        )


def _require_unique_ids(objects: list) -> None:
    ids = [o.id for o in objects]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"object ids must be unique; duplicates: {dupes}")


class AnalysisDraft(BaseModel):
    paper_title: str = Field(min_length=1)
    objects: list[SciObjectDraft] = Field(min_length=4)

    @model_validator(mode="after")
    def check_coverage(self) -> "AnalysisDraft":
        _require_all_types(self.objects)
        _require_unique_ids(self.objects)
        return self


class AnalysisResult(BaseModel):
    paper_title: str = Field(min_length=1)
    research_question: str
    objects: list[SciObject] = Field(min_length=4)

    @model_validator(mode="after")
    def check_coverage(self) -> "AnalysisResult":
        _require_all_types(self.objects)
        _require_unique_ids(self.objects)
        return self


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

INVENTORY_SYSTEM_PROMPT = """You are ResearchOS, a scientific paper reader.
Given a scientific paper (PDF), inventory EVERY labeled scientific object:
numbered equations (Equation 1, 2, ...), figures/diagrams (Figure 1, 2, ...),
and tables (Table 1, 2, ...). A label checklist detected in the PDF text is
provided — use it as a completeness aid, but the PDF itself is authoritative:
include every labeled object even if the checklist missed it. Also identify the
paper's major experimentally testable claims (a few central claims only, not
every sentence).

Do NOT limit discovery to any research question: every labeled equation,
figure, and table must be explainable. Do NOT treat every inline mathematical
expression as an object — only displayed, numbered equations.

For EVERY object return all base fields, plus `paper_importance` (0.0-1.0: how
fundamental is this object to the paper's own contribution?) and
`paper_importance_reason` (short justification). Every object must have a
UNIQUE id (e.g. "eq-10", "fig-1", "table-4"). For equations also fill
`equation`, for figures also fill `figure`, for tables also fill `table`. Omit
the details block that does not match the object's type.

Grounding rules (strict):
- Ground every object to the paper: give the 1-based `page` number and the
  paper's own `label` (e.g. "Equation 2", "Figure 1", "Table 2") whenever the
  paper shows them. Use null only when genuinely absent.
- `evidence` must be a concise location/reference in the paper supporting your
  interpretation (e.g. "p.4, Eq.2 and surrounding paragraph").
- NEVER invent evidence, page numbers, labels, numbers, or results. If the PDF
  does not provide something, use null (or "not stated in the paper" for text)
  and lower `confidence`.
- `latex`: transcribe only if recoverable from the PDF, else null.
- `higher_or_lower_is_better`: set only if determinable from the paper, else null.
- `related_objects`: ids of objects in THIS response that are logically
  connected (e.g. a table testing an equation's claim). Every id there must
  exist in `objects`.
- `limitations`: state what this object does NOT establish.
- `confidence`: 0.0-1.0, your confidence that the interpretation is grounded
  in the paper.

Return JSON only, matching the provided schema. No markdown, no commentary."""

RELEVANCE_SYSTEM_PROMPT = """You are a ResearchOS relevance ranker. You receive object summaries from a
paper analysis plus the user's research question. Score EVERY listed object id
exactly once: `research_relevance` (1-2 sentences: how this connects
specifically to the question, or why it does not), `research_relevance_score`
(0.0-1.0: how much would understanding THIS object help answer the question?),
and `research_relevance_reason` (short justification).

Calibration (be selective, use the FULL range including low scores):
- A high score (>= 0.75) means the object is directly needed to answer the
  question — the alternatives, comparisons, or mechanisms the question asks about.
- Foundational background (standard definitions, textbook equations) is at most
  0.5 unless the question is directly about it.
- Do NOT mark everything in the Method section highly relevant. An equation can
  be fundamental to the paper yet score low if the question does not need it.
- Unrelated objects (other tasks, other datasets, background ablations) score
  below 0.40 with a reason saying what they are about instead.

Return JSON only, matching the provided schema. No markdown, no commentary."""


def build_inventory_text(pdf_filename: str, checklist: str) -> str:
    return (
        f'Paper file: "{pdf_filename}"\n\n'
        "Labels detected in the PDF text (completeness checklist — the PDF is "
        "authoritative):\n"
        f"{checklist}\n\n"
        "You MUST inventory the attached paper. An empty objects array is invalid. "
        "Include every labeled equation, figure/diagram, and table, plus the major "
        "experimentally testable claims."
    )


def build_relevance_text(question: str, draft: AnalysisDraft) -> str:
    summaries = [
        {
            "id": o.id,
            "type": o.type,
            "label": o.label,
            "title": o.title,
            "page": o.page,
            "what_it_is": o.what_it_is,
            "why_it_is_here": o.why_it_is_here,
            "claim_supported": o.claim_supported,
        }
        for o in draft.objects
    ]
    return (
        f'Research question: "{question}"\n\n'
        "Object summaries:\n"
        f"{json.dumps(summaries, indent=1)}\n\n"
        "Score every object id exactly once."
    )


# ---------------------------------------------------------------------------
# Model API calls
# ---------------------------------------------------------------------------


def _responses_format(name: str, schema: dict) -> dict:
    return {"type": "json_schema", "name": name, "schema": schema}


def _chat_format(name: str, schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}


def responses_json_schema_format() -> dict:
    return _responses_format("paper_inventory", AnalysisDraft.model_json_schema())


def chat_json_schema_format() -> dict:
    return _chat_format("paper_inventory", AnalysisDraft.model_json_schema())


def extract_json(text: str) -> str:
    """Strip code fences if the model wrapped JSON in markdown."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.rsplit("```", 1):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def sanity_check(result: AnalysisDraft | AnalysisResult) -> None:
    """Defense-in-depth after Pydantic validation: reject empty analyses."""
    if not result.paper_title.strip():
        raise ValueError("sanity check failed: paper_title is blank")
    if len(result.objects) < 4:
        raise ValueError("sanity check failed: fewer than 4 objects")
    present = {o.type for o in result.objects}
    missing = [t for t in REQUIRED_OBJECT_TYPES if t not in present]
    if missing:
        raise ValueError(f"sanity check failed: missing object types: {missing}")


def try_responses_api(
    client: OpenAI, model: str, pdf_b64_url: str, filename: str, checklist: str
) -> AnalysisDraft:
    resp = client.responses.create(
        model=model,
        instructions=INVENTORY_SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": build_inventory_text(filename, checklist)},
                    {
                        "type": "input_file",
                        "filename": filename,
                        "file_data": pdf_b64_url,
                        "detail": "high",
                    },
                ],
            }
        ],
        text={"format": responses_json_schema_format()},
    )
    result = AnalysisDraft.model_validate_json(extract_json(resp.output_text))
    sanity_check(result)
    return result


def try_chat_api(
    client: OpenAI,
    model: str,
    pdf_b64_url: str,
    filename: str,
    checklist: str,
    use_json_schema: bool,
) -> AnalysisDraft:
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": INVENTORY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_inventory_text(filename, checklist)},
                    {
                        "type": "file",
                        "file": {"filename": filename, "file_data": pdf_b64_url},
                    },
                ],
            },
        ],
    }
    if use_json_schema:
        kwargs["response_format"] = chat_json_schema_format()
    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    result = AnalysisDraft.model_validate_json(extract_json(content))
    sanity_check(result)
    return result


def analyze_pdf(
    client: OpenAI, model: str, pdf_bytes: bytes, filename: str, checklist: str
) -> AnalysisDraft:
    """Stage 1: full inventory + explanations + paper importance."""
    pdf_b64_url = (
        "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
    )
    attempts = [
        ("Responses API + json_schema", lambda: try_responses_api(
            client, model, pdf_b64_url, filename, checklist)),
        ("Chat Completions + json_schema", lambda: try_chat_api(
            client, model, pdf_b64_url, filename, checklist, True)),
        ("Chat Completions plain JSON", lambda: try_chat_api(
            client, model, pdf_b64_url, filename, checklist, False)),
    ]
    errors = []
    for name, fn in attempts:
        try:
            print(f"[attempt] stage 1 {name} ...", file=sys.stderr)
            return fn()
        except Exception as e:  # noqa: BLE001 - prototype: fall through to next attempt
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[attempt] stage 1 {name} failed: {e}", file=sys.stderr)
    raise RuntimeError("Stage 1 attempts failed:\n" + "\n".join(errors))


def score_relevance(
    client: OpenAI, model: str, question: str, draft: AnalysisDraft
) -> RelevanceList:
    """Stage 2: score every inventoried object against the question (text-only)."""
    messages = [
        {"role": "system", "content": RELEVANCE_SYSTEM_PROMPT},
        {"role": "user", "content": build_relevance_text(question, draft)},
    ]
    attempts = [
        ("Chat Completions + json_schema",
         {"response_format": _chat_format("relevance_scores", RelevanceList.model_json_schema())}),
        ("Chat Completions plain JSON", {}),
    ]
    errors = []
    for name, kwargs in attempts:
        try:
            print(f"[attempt] stage 2 {name} ...", file=sys.stderr)
            resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
            content = resp.choices[0].message.content or ""
            return RelevanceList.model_validate_json(extract_json(content))
        except Exception as e:  # noqa: BLE001 - prototype: fall through to next attempt
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[attempt] stage 2 {name} failed: {e}", file=sys.stderr)
    raise RuntimeError("Stage 2 attempts failed:\n" + "\n".join(errors))


def merge_relevance(
    draft: AnalysisDraft, rel: RelevanceList, question: str
) -> AnalysisResult:
    """Join stage 1 + stage 2. Every id must be scored exactly once."""
    by_id = {s.id: s for s in rel.scores}
    if len(by_id) != len(rel.scores):
        raise ValueError("relevance scores contain duplicate ids")
    known = {o.id for o in draft.objects}
    unknown = set(by_id) - known
    if unknown:
        raise ValueError(f"relevance scores reference unknown ids: {sorted(unknown)}")
    missing = known - set(by_id)
    if missing:
        raise ValueError(f"relevance scores missing ids: {sorted(missing)}")
    objects = [
        SciObject(
            **o.model_dump(),
            research_relevance=by_id[o.id].research_relevance,
            research_relevance_score=by_id[o.id].research_relevance_score,
            research_relevance_reason=by_id[o.id].research_relevance_reason,
        )
        for o in draft.objects
    ]
    return AnalysisResult(
        paper_title=draft.paper_title, research_question=question, objects=objects
    )


def draft_from_result(result: AnalysisResult) -> AnalysisDraft:
    """Strip relevance fields back to a stage-1 draft (for --rescore)."""
    keep = set(SciObjectDraft.model_fields)
    return AnalysisDraft(
        paper_title=result.paper_title,
        objects=[
            SciObjectDraft(**{k: v for k, v in o.model_dump().items() if k in keep})
            for o in result.objects
        ],
    )


# ---------------------------------------------------------------------------
# Soft grounding checks (warnings only; schema validation is strict).
# ---------------------------------------------------------------------------


def check_grounding(result: AnalysisDraft | AnalysisResult) -> list[str]:
    warnings = []
    known = {o.id for o in result.objects}
    details_field = {"equation": "equation", "figure": "figure", "table": "table"}
    for o in result.objects:
        for rel_id in o.related_objects:
            if rel_id not in known:
                warnings.append(f"{o.id}: related_objects references unknown id '{rel_id}'")
        expected = details_field.get(o.type)
        if expected and getattr(o, expected) is None:
            warnings.append(f"{o.id}: type '{o.type}' has no '{expected}' details")
        if o.page is None:
            warnings.append(f"{o.id}: no page grounding")
    return warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ResearchOS analysis: inventory + explain every labeled "
        "scientific object, then score relevance to a research question."
    )
    p.add_argument("--pdf", default=None, help="Path to the scientific PDF.")
    p.add_argument("--question", required=True, help="User's research question.")
    p.add_argument(
        "--output",
        default="prototype/output.json",
        help="Where to write the validated JSON (default: prototype/output.json).",
    )
    p.add_argument(
        "--rescore",
        default=None,
        help="Existing output.json to re-run ONLY the relevance stage on.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL}).")
    p.add_argument("--base-url", default=API_BASE_URL, help=f"API base URL (default: {API_BASE_URL}).")
    return p.parse_args(argv)


def write_result(result: AnalysisResult, out_path: Path) -> None:
    for w in check_grounding(result):
        print(f"[warning] {w}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    levels: dict[str, int] = {}
    for o in result.objects:
        counts[o.type] = counts.get(o.type, 0) + 1
        levels[o.research_relevance_level] = levels.get(o.research_relevance_level, 0) + 1
    print(
        f"[done] wrote {out_path}: {len(result.objects)} objects {counts}, "
        f"relevance {levels}",
        file=sys.stderr,
    )


def main() -> int:
    load_dotenv()  # local .env only; never committed (see .gitignore)
    args = parse_args()

    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        print(
            f"error: {API_KEY_ENV_VAR} is not set.\n"
            f"  export {API_KEY_ENV_VAR}=...  (or put it in a local .env file; "
            "see .env.example)",
            file=sys.stderr,
        )
        return 2

    client = OpenAI(base_url=args.base_url, api_key=api_key)
    try:
        if args.rescore:
            result_path = Path(args.rescore)
            if not result_path.is_file():
                print(f"error: analysis not found: {result_path}", file=sys.stderr)
                return 2
            draft = draft_from_result(
                AnalysisResult.model_validate_json(result_path.read_text())
            )
            print(
                f"[info] rescoring {len(draft.objects)} objects with {args.model}",
                file=sys.stderr,
            )
            rel = score_relevance(client, args.model, args.question, draft)
            result = merge_relevance(draft, rel, args.question)
        else:
            if not args.pdf:
                print("error: --pdf is required (or use --rescore)", file=sys.stderr)
                return 2
            pdf_path = Path(args.pdf)
            if not pdf_path.is_file():
                print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
                return 2
            pdf_bytes = pdf_path.read_bytes()
            candidates = discover_labels(pdf_path)
            print(
                f"[info] PDF: {pdf_path} ({len(pdf_bytes) / 1e6:.1f} MB), "
                f"{len(candidates)} labels discovered, model: {args.model}",
                file=sys.stderr,
            )
            draft = analyze_pdf(
                client, args.model, pdf_bytes, pdf_path.name,
                format_checklist(candidates),
            )
            rel = score_relevance(client, args.model, args.question, draft)
            result = merge_relevance(draft, rel, args.question)
    except Exception as e:  # noqa: BLE001 - prototype: report and exit non-zero
        print(f"error: analysis failed: {e}", file=sys.stderr)
        return 1

    write_result(result, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
