#!/usr/bin/env python3
"""ResearchOS Milestone 8: literature-discovery candidate models + dedup.

Validated models for autonomous literature discovery:

- DiscoveryCandidate: one shortlisted paper with provider metadata,
  evidence level, agent evaluation, novelty notes, and researcher decision.
- DiscoveryRun: one bounded discovery run with strategy, queries,
  reflection, critique, budgets, and provenance.

Deduplication is deterministic and testable (no model calls): stable
identifiers first (DOI, OpenAlex ID, arXiv ID), exact normalized-title
matching as a fallback. Preprint/published-version pairs merge; distinct
papers with merely similar titles never do.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import BaseModel, Field


class DiscoveryFailure(RuntimeError):
    """A discovery run failed closed (agent/model/budget failure). Carries
    the user-facing reason; the job layer converts it to a retryable job
    failure. Never raised for per-query/per-item soft failures."""


# Evidence access levels. A recommendation grounded in an abstract is never
# equivalent to full-paper analysis; object-level claims require ingestion.
EvidenceLevel = Literal[
    "metadata_only",
    "title_and_abstract",
    "full_text_available",
    "full_text_analyzed",
]

# Researcher decision / import lifecycle per candidate.
CandidateStatus = Literal[
    "pending",
    "approved",
    "dismissed",
    "importing",
    "imported",
    "import_failed",
    "full_text_unavailable",
]

RelevanceVerdict = Literal["highly_relevant", "relevant", "tangential", "off_topic"]

# Phrases that overclaim novelty. Deterministic post-check rewrites these to
# the scoped fallback; the critic prompt independently forbids them.
NOVELTY_OVERCLAIM_PATTERNS = (
    r"no\s+(one|body|paper|papers|prior\s+work|existing\s+(work|research|literature))\s+"
    r"(has|have|had)\s+(studied|explored|investigated|published|addressed|considered)",
    r"this\s+is\s+(entirely\s+|completely\s+)?novel",
    r"first\s+(ever|of\s+its\s+kind)",
    r"nothing\s+(has\s+been\s+)?published\s+on",
)

NOVELTY_SCOPED_FALLBACK = (
    "This search found no direct match within the retrieved literature, "
    "but coverage is limited."
)


class CandidateEvaluation(BaseModel):
    """Spark Open's grounded assessment of one shortlisted candidate."""

    verdict: RelevanceVerdict = "tangential"
    why_relevant: str = ""
    may_contribute: str = ""
    may_overlap_with: str = ""
    still_unknown: str = ""
    why_read_next: str = ""
    novelty_overlap: bool = False
    novelty_note: str = ""


class DiscoveryCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    publication_year: Optional[int] = None
    publication_date: Optional[str] = None
    venue: str = ""
    work_type: str = ""  # OpenAlex type: article, preprint, dissertation, ...
    doi: str = ""
    openalex_id: str = ""
    arxiv_id: str = ""
    semantic_scholar_id: str = ""
    abstract: str = ""
    landing_page_url: str = ""
    open_access_url: str = ""
    pdf_url: str = ""
    license: str = ""
    citation_count: Optional[int] = None
    source_provider: str = "openalex"
    retrieval_query: str = ""
    retrieval_round: int = Field(default=1, ge=1)
    retrieved_at: str = ""
    research_question_version: int = Field(default=1, ge=1)
    evidence_access_level: EvidenceLevel = "metadata_only"
    evaluation: CandidateEvaluation = Field(default_factory=CandidateEvaluation)
    recommended: bool = False
    status: CandidateStatus = "pending"
    status_reason: str = ""
    already_in_project: bool = False
    project_paper_id: str = ""  # set when matched to / imported as a paper
    sha256: str = ""  # content hash of the downloaded full text, when fetched
    import_job_id: str = ""
    prior_status: str = ""  # e.g. "dismissed in run <id>" when re-retrieved

    def display_venue(self) -> str:
        """Honest venue label: never present a preprint as peer-reviewed."""
        if self.work_type.lower() == "preprint" or (
            self.arxiv_id and not self.venue
        ):
            return "Preprint" + (f" ({self.venue})" if self.venue else "")
        return self.venue or "Unknown venue"


class DiscoveryQueryRecord(BaseModel):
    query: str
    round: int = 1
    strategy: str = ""
    rationale: str = ""
    provider: str = "openalex"
    n_records: int = 0
    error: str = ""


class DiscoveryBudget(BaseModel):
    """Per-run bounds. Overrides may only lower these caps."""

    max_rounds: int = 2
    max_queries_per_round: int = 3
    max_records: int = 50
    max_shortlist: int = 8
    max_recommendations: int = 5
    max_expansions: int = 2
    max_deep_inspections: int = 3
    max_spark_calls: int = 8

    def clamped(self, overrides: dict[str, Any]) -> "DiscoveryBudget":
        data = self.model_dump()
        for key, cap in data.items():
            if key in overrides:
                try:
                    value = int(overrides[key])
                except (TypeError, ValueError):
                    continue
                data[key] = max(1, min(value, cap))
        return DiscoveryBudget.model_validate(data)


class DiscoveryRun(BaseModel):
    run_id: str = Field(min_length=1)
    thread_id: str = ""
    project_id: str = ""
    research_question: str = ""
    research_question_version: int = Field(default=1, ge=1)
    focus: str = ""
    status: Literal["running", "completed", "failed"] = "running"
    stale: bool = False
    created_at: str = ""
    updated_at: str = ""
    budget: DiscoveryBudget = Field(default_factory=DiscoveryBudget)
    strategy_notes: str = ""
    queries: list[DiscoveryQueryRecord] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    reflection_decision: str = ""
    reflection_explanation: str = ""
    critic_verdict: str = ""
    critic_notes: str = ""
    revision_used: bool = False
    limitations: str = ""
    error: str = ""
    candidates: list[DiscoveryCandidate] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Identifier normalization (deterministic, no model calls)
# ---------------------------------------------------------------------------


def normalize_doi(doi: str) -> str:
    """Lowercased bare DOI (no resolver prefix, no trailing punctuation)."""
    d = (doi or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip().rstrip(".,;)")


def normalize_openalex_id(value: str) -> str:
    """Bare OpenAlex work id (W + digits, uppercase)."""
    v = (value or "").strip()
    if "/openalex.org/" in v:
        v = v.rsplit("/openalex.org/", 1)[1]
    v = v.strip().strip("/")
    return v.upper() if re.fullmatch(r"W\d+", v.upper()) else ""


def normalize_arxiv_id(value: str) -> str:
    """Full arXiv id incl. version (lowercased, no prefix/URL)."""
    v = (value or "").strip().lower()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/",
                   "arxiv:", "arxiv.org/abs/"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return v.strip().rstrip(".,;)")


def arxiv_base(arxiv_id: str) -> str:
    """arXiv id without the version suffix (2401.12345v2 -> 2401.12345)."""
    return re.sub(r"v\d+$", "", normalize_arxiv_id(arxiv_id))


def normalize_title(title: str) -> str:
    """Lowercased alphanumeric token stream for exact-match fallback."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (title or "").lower())).strip()


def author_surnames(authors: list[str]) -> set[str]:
    surnames = set()
    for name in authors or []:
        parts = re.sub(r"[^a-zA-Z\-' ]+", "", name).strip().split()
        if parts:
            surnames.add(parts[-1].lower())
    return surnames


def stable_keys(candidate: dict[str, Any]) -> set[str]:
    """Stable identity keys present on a candidate/record dict."""
    keys = set()
    doi = normalize_doi(str(candidate.get("doi") or ""))
    if doi:
        keys.add(f"doi:{doi}")
    oa = normalize_openalex_id(str(candidate.get("openalex_id") or ""))
    if oa:
        keys.add(f"oa:{oa}")
    ax = arxiv_base(str(candidate.get("arxiv_id") or ""))
    if ax:
        keys.add(f"ax:{ax}")
    return keys


def clamp_novelty_wording(text: str) -> tuple[str, bool]:
    """Replace novelty overclaims with the scoped fallback. Returns
    (possibly rewritten text, whether a rewrite happened)."""
    rewritten = text or ""
    hit = False
    for pattern in NOVELTY_OVERCLAIM_PATTERNS:
        new, n = re.subn(pattern, NOVELTY_SCOPED_FALLBACK, rewritten,
                         flags=re.IGNORECASE)
        if n:
            hit = True
            rewritten = new
    return rewritten, hit


# ---------------------------------------------------------------------------
# Deterministic dedup
# ---------------------------------------------------------------------------


class DedupIndex:
    """Keys + titles already seen (project papers, prior runs, this run)."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.key_owners: dict[str, str] = {}
        # normalized title -> list of (year, surnames, label) for fallback
        self.titles: dict[str, list[tuple[Optional[int], set[str], str]]] = {}

    def add(self, item: dict[str, Any], label: str) -> None:
        for key in stable_keys(item):
            self.keys.add(key)
            self.key_owners.setdefault(key, label)
        title = normalize_title(str(item.get("title") or ""))
        if len(title) >= 12:
            try:
                year = item.get("publication_year") or item.get("year")
                year_int = int(year) if year is not None else None
            except (TypeError, ValueError):
                year_int = None
            self.titles.setdefault(title, []).append(
                (year_int, author_surnames(item.get("authors") or []), label)
            )

    def match(self, item: dict[str, Any], *,
              exact_title_counts: bool = False) -> tuple[str, str]:
        """Return (reason, label); reason is '' when this item is new.

        exact_title_counts covers metadata-bare project papers (title only,
        no year/authors/ids): an exact normalized-title match then counts,
        since distinct papers with identical full titles are vanishingly
        rare and the consequence (surfaced as already-in-project, never
        silently dropped) is mild and visible.
        """
        shared = stable_keys(item) & self.keys
        if shared:
            key = sorted(shared)[0]
            return f"stable-id {key}", self.key_owners.get(key, "")
        title = normalize_title(str(item.get("title") or ""))
        if len(title) >= 12 and title in self.titles:
            try:
                year = item.get("publication_year") or item.get("year")
                year_int = int(year) if year is not None else None
            except (TypeError, ValueError):
                year_int = None
            mine = author_surnames(item.get("authors") or [])
            for other_year, other_names, label in self.titles[title]:
                same_year = (
                    year_int is not None
                    and other_year is not None
                    and year_int == other_year
                )
                shared_author = bool(mine & other_names)
                bare_exact = (exact_title_counts and other_year is None
                              and not other_names)
                if same_year or shared_author or bare_exact:
                    return f"title match ({label})", label
        return "", ""


def dedupe_records(
    records: list[dict[str, Any]],
    project_items: list[dict[str, Any]],
    prior_items: list[tuple[dict[str, Any], str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split retrieved records into (unique, duplicates).

    project_items: papers already in the project (title/author/year dicts).
    prior_items: (candidate dict, run label) from earlier discovery runs.
    Duplicates carry `duplicate_of` + `duplicate_reason`; project matches
    additionally get `already_in_project`.
    """
    project_index = DedupIndex()
    for item in project_items:
        project_index.add(item, f"project paper '{item.get('paper_id', '?')}'")
    run_index = DedupIndex()
    prior_index = DedupIndex()
    for item, label in prior_items:
        prior_index.add(item, label)

    unique: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for record in records:
        dup = dict(record)
        reason, _ = run_index.match(record)
        if reason:
            dup["duplicate_of"] = "this run"
            dup["duplicate_reason"] = reason
            duplicates.append(dup)
            continue
        reason, label = project_index.match(record, exact_title_counts=True)
        if reason:
            dup["duplicate_of"] = "project"
            dup["duplicate_reason"] = reason
            dup["already_in_project"] = True
            dup["project_paper_id"] = label
            duplicates.append(dup)
            run_index.add(record, "this run")
            continue
        reason, label = prior_index.match(record)
        if reason:
            dup["duplicate_of"] = "prior discovery"
            dup["duplicate_reason"] = reason
            dup["prior_label"] = label
            duplicates.append(dup)
            run_index.add(record, "this run")
            continue
        run_index.add(record, "this run")
        unique.append(record)
    return unique, duplicates


def candidate_from_record(
    record: dict[str, Any],
    candidate_id: str,
    question_version: int,
    retrieved_at: str,
) -> DiscoveryCandidate:
    """Build a validated candidate from a normalized provider record.

    Absent metadata stays absent (empty/None) — never invented.
    """
    abstract = str(record.get("abstract") or "")
    pdf_url = str(record.get("pdf_url") or "")
    if pdf_url:
        level: EvidenceLevel = "full_text_available"
    elif abstract.strip():
        level = "title_and_abstract"
    else:
        level = "metadata_only"
    try:
        year = record.get("publication_year")
        year_int = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_int = None
    try:
        cited = record.get("citation_count")
        cited_int = int(cited) if cited is not None else None
    except (TypeError, ValueError):
        cited_int = None
    return DiscoveryCandidate(
        candidate_id=candidate_id,
        title=str(record.get("title") or ""),
        authors=[str(a) for a in (record.get("authors") or []) if str(a).strip()],
        publication_year=year_int,
        publication_date=str(record.get("publication_date") or "") or None,
        venue=str(record.get("venue") or ""),
        work_type=str(record.get("work_type") or ""),
        doi=str(record.get("doi") or ""),
        openalex_id=str(record.get("openalex_id") or ""),
        arxiv_id=str(record.get("arxiv_id") or ""),
        semantic_scholar_id=str(record.get("semantic_scholar_id") or ""),
        abstract=abstract,
        landing_page_url=str(record.get("landing_page_url") or ""),
        open_access_url=str(record.get("open_access_url") or ""),
        pdf_url=pdf_url,
        license=str(record.get("license") or ""),
        citation_count=cited_int,
        source_provider=str(record.get("source_provider") or "openalex"),
        retrieval_query=str(record.get("retrieval_query") or ""),
        retrieval_round=int(record.get("retrieval_round") or 1),
        retrieved_at=retrieved_at,
        research_question_version=question_version,
        evidence_access_level=level,
        already_in_project=bool(record.get("already_in_project", False)),
        project_paper_id=str(record.get("project_paper_id") or ""),
    )
