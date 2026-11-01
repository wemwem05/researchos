#!/usr/bin/env python3
"""ResearchOS Milestone 1 prototype.

Sends a scientific PDF + a research question to Muse (Meta Model API)
and produces structured JSON describing the paper's key scientific objects:
equations, figures, tables, and experimentally testable claims.

Usage:
    python prototype/analyze_paper.py \
        --pdf samples/eot_shape_correspondence.pdf \
        --question "Can alternative optimal transport costs improve non-rigid shape correspondence?"

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

API_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
API_KEY_ENV_VAR = "MODEL_API_KEY"


# ---------------------------------------------------------------------------
# Pydantic models: the contract for the model's structured output.
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


class SciObject(BaseModel):
    id: str
    type: Literal["equation", "figure", "table", "claim"]
    page: Optional[int] = None
    label: Optional[str] = None
    title: str
    what_it_is: str
    why_it_is_here: str
    claim_supported: str
    limitations: str
    research_relevance: str
    related_objects: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str
    equation: Optional[EquationDetails] = None
    figure: Optional[FigureDetails] = None
    table: Optional[TableDetails] = None


REQUIRED_OBJECT_TYPES = ("equation", "figure", "table", "claim")


class AnalysisResult(BaseModel):
    paper_title: str = Field(min_length=1)
    research_question: str
    objects: list[SciObject] = Field(min_length=4)

    @model_validator(mode="after")
    def require_all_object_types(self) -> "AnalysisResult":
        present = {o.type for o in self.objects}
        missing = [t for t in REQUIRED_OBJECT_TYPES if t not in present]
        if missing:
            raise ValueError(
                "objects must include at least one of each required type; "
                f"missing: {missing}"
            )
        return self


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are ResearchOS, a research-question-aware scientific paper reader.
Given a scientific paper (PDF) and the user's research question, extract the
paper's important scientific objects: equations, figures/architecture diagrams,
tables, and experimentally testable claims.

For EVERY object return all base fields. For equations also fill `equation`,
for figures also fill `figure`, for tables also fill `table`. Omit the details
block that does not match the object's type.

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
- `research_relevance`: connect the object specifically to the user's research
  question, not to the paper in general.
- `limitations`: state what this object does NOT establish.
- `confidence`: 0.0-1.0, your confidence that the interpretation is grounded
  in the paper.

Return JSON only, matching the provided schema. No markdown, no commentary."""


def build_question_text(pdf_filename: str, question: str) -> str:
    return (
        f'Paper file: "{pdf_filename}"\n'
        f'Research question: "{question}"\n\n'
        "You MUST analyze the attached paper. An empty objects array is invalid. "
        "Identify at least one important equation, one figure/diagram, one results "
        "table, and one experimentally testable claim. Prefer objects most relevant "
        "to the user's research question."
    )


# ---------------------------------------------------------------------------
# Model API calls: direct PDF input, structured output where supported.
# Tries Responses API first, then Chat Completions with/without json_schema.
# ---------------------------------------------------------------------------


def responses_json_schema_format() -> dict:
    return {
        "type": "json_schema",
        "name": "paper_analysis",
        "schema": AnalysisResult.model_json_schema(),
    }


def chat_json_schema_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "paper_analysis",
            "schema": AnalysisResult.model_json_schema(),
        },
    }


def extract_json(text: str) -> str:
    """Strip code fences if the model wrapped JSON in markdown."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.rsplit("```", 1):
            text = text.rsplit("```", 1)[0]
    return text.strip()


def sanity_check(result: AnalysisResult) -> None:
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
    client: OpenAI, model: str, pdf_b64_url: str, filename: str, question: str
) -> AnalysisResult:
    resp = client.responses.create(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": build_question_text(filename, question)},
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
    result = AnalysisResult.model_validate_json(extract_json(resp.output_text))
    sanity_check(result)
    return result


def try_chat_api(
    client: OpenAI,
    model: str,
    pdf_b64_url: str,
    filename: str,
    question: str,
    use_json_schema: bool,
) -> AnalysisResult:
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_question_text(filename, question)},
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
    result = AnalysisResult.model_validate_json(extract_json(content))
    sanity_check(result)
    return result


def analyze_pdf(
    client: OpenAI, model: str, pdf_bytes: bytes, filename: str, question: str
) -> AnalysisResult:
    pdf_b64_url = (
        "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode("ascii")
    )
    attempts = [
        ("Responses API + json_schema", lambda: try_responses_api(
            client, model, pdf_b64_url, filename, question)),
        ("Chat Completions + json_schema", lambda: try_chat_api(
            client, model, pdf_b64_url, filename, question, True)),
        ("Chat Completions plain JSON", lambda: try_chat_api(
            client, model, pdf_b64_url, filename, question, False)),
    ]
    errors = []
    for name, fn in attempts:
        try:
            print(f"[attempt] {name} ...", file=sys.stderr)
            return fn()
        except Exception as e:  # noqa: BLE001 - prototype: fall through to next attempt
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[attempt] {name} failed: {e}", file=sys.stderr)
    raise RuntimeError("All API attempts failed:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Soft grounding checks (warnings only; schema validation is strict).
# ---------------------------------------------------------------------------


def check_grounding(result: AnalysisResult) -> list[str]:
    warnings = []
    ids = [o.id for o in result.objects]
    if len(set(ids)) != len(ids):
        warnings.append("duplicate object ids found")
    known = set(ids)
    details_field = {"equation": "equation", "figure": "figure", "table": "table"}
    for o in result.objects:
        for rel in o.related_objects:
            if rel not in known:
                warnings.append(f"{o.id}: related_objects references unknown id '{rel}'")
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
        description="ResearchOS Milestone 1: extract structured scientific "
        "objects from a paper PDF for a research question."
    )
    p.add_argument("--pdf", required=True, help="Path to the scientific PDF.")
    p.add_argument("--question", required=True, help="User's research question.")
    p.add_argument(
        "--output",
        default="prototype/output.json",
        help="Where to write the validated JSON (default: prototype/output.json).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model id (default: {DEFAULT_MODEL}).")
    p.add_argument("--base-url", default=API_BASE_URL, help=f"API base URL (default: {API_BASE_URL}).")
    return p.parse_args(argv)


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

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"error: PDF not found: {pdf_path}", file=sys.stderr)
        return 2
    pdf_bytes = pdf_path.read_bytes()
    print(
        f"[info] PDF: {pdf_path} ({len(pdf_bytes) / 1e6:.1f} MB), "
        f"model: {args.model}",
        file=sys.stderr,
    )

    client = OpenAI(base_url=args.base_url, api_key=api_key)
    try:
        result = analyze_pdf(client, args.model, pdf_bytes, pdf_path.name, args.question)
    except Exception as e:  # noqa: BLE001 - prototype: report and exit non-zero
        print(f"error: analysis failed: {e}", file=sys.stderr)
        return 1

    for w in check_grounding(result):
        print(f"[warning] {w}", file=sys.stderr)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for o in result.objects:
        counts[o.type] = counts.get(o.type, 0) + 1
    print(
        f"[done] wrote {out_path}: {len(result.objects)} objects {counts}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
