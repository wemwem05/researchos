#!/usr/bin/env python3
"""ResearchOS Milestone 6.1: bounded external-literature corroboration.

Optional, targeted OpenAlex lookup — never broad autonomous discovery.
Per-claim budget (hard caps):

- at most 2 focused queries
- at most 10 candidate records total
- at most 3 papers selected for closer examination

Only bibliographic metadata + abstracts are used; every external finding
is labeled ABSTRACT-LEVEL EVIDENCE. No PDFs are downloaded or
redistributed. Keyless queries by default; optional OPENALEX_MAILTO /
OPENALEX_API_KEY configuration. Rate limits, timeouts, and unavailable
endpoints fail soft (the audit continues on internal evidence).

External content is UNTRUSTED input: it is quoted as evidence only and
must never change prompts or execution policy.

FakeOpenAlex provides scripted responses for deterministic offline tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

OPENALEX_BASE = "https://api.openalex.org"
MAILTO_ENV_VAR = "OPENALEX_MAILTO"
API_KEY_ENV_VAR = "OPENALEX_API_KEY"

# Hard per-claim budgets.
MAX_QUERIES = 2
MAX_RECORDS = 10
MAX_SELECTED = 3
PER_PAGE_DEFAULT = 5
REQUEST_TIMEOUT_S = 20
MAX_ABSTRACT_CHARS = 3000


class OpenAlexError(RuntimeError):
    """External search failed; the audit continues without it."""


@dataclass
class OpenAlexRecord:
    openalex_id: str
    doi: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    cited_by: int = 0
    abstract: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "openalex_id": self.openalex_id,
            "doi": self.doi,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "cited_by": self.cited_by,
            "abstract": self.abstract,
            "url": self.url,
        }


def reconstruct_abstract(inverted: Optional[dict[str, list[int]]]) -> str:
    if not inverted:
        return ""
    try:
        positions: dict[int, str] = {}
        for word, idxs in inverted.items():
            for i in idxs:
                positions[int(i)] = word
        text = " ".join(positions[i] for i in sorted(positions))
    except (ValueError, TypeError, KeyError):
        return ""
    return text[:MAX_ABSTRACT_CHARS]


def _cache_key(query: str, per_page: int) -> str:
    return hashlib.sha256(f"{query}\n{per_page}".encode()).hexdigest()[:32]


class OpenAlexClient:
    """Keyless-by-default OpenAlex search with disk cache and fail-soft errors."""

    def __init__(self, cache_dir: Optional[Path] = None,
                 mailto: Optional[str] = None, api_key: Optional[str] = None):
        self.cache_dir = cache_dir
        self.mailto = mailto if mailto is not None else os.environ.get(MAILTO_ENV_VAR, "")
        # Never log the key; it only goes into request params.
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR, "")
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, query: str, per_page: int = PER_PAGE_DEFAULT) -> list[OpenAlexRecord]:
        query = (query or "").strip()
        if not query:
            raise OpenAlexError("empty query")
        per_page = max(1, min(per_page, MAX_RECORDS))
        cached = self._read_cache(query, per_page)
        if cached is not None:
            return cached
        records = self._fetch(query, per_page)
        self._write_cache(query, per_page, records)
        return records

    # -- internals ------------------------------------------------------

    def _params(self, query: str, per_page: int) -> dict[str, str]:
        params = {"search": query, "per-page": str(per_page)}
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _fetch(self, query: str, per_page: int) -> list[OpenAlexRecord]:
        url = f"{OPENALEX_BASE}/works?{urllib.parse.urlencode(self._params(query, per_page))}"
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "ResearchOS/6.1 (mailto unset)"})
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    if resp.status == 429:
                        raise OpenAlexError("rate limited (429)")
                    if resp.status != 200:
                        raise OpenAlexError(f"HTTP {resp.status}")
                    doc = json.loads(resp.read().decode("utf-8"))
            except OpenAlexError:
                if attempt == 0:
                    time.sleep(5)  # one backoff, then fail soft via caller
                    continue
                raise
            except Exception as e:  # noqa: BLE001 - timeouts/DNS become OpenAlexError
                raise OpenAlexError(f"request failed: {type(e).__name__}: {e}") from e
            return [self._parse_work(w) for w in doc.get("results", [])[:per_page]]
        raise OpenAlexError("rate limited; giving up after one backoff")

    @staticmethod
    def _parse_work(w: dict[str, Any]) -> OpenAlexRecord:
        authorships = w.get("authorships", []) or []
        authors = []
        for a in authorships[:12]:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)
        return OpenAlexRecord(
            openalex_id=str(w.get("id", "")),
            doi=str(w.get("doi", "") or ""),
            title=str(w.get("title") or w.get("display_name") or ""),
            authors=authors,
            year=w.get("publication_year"),
            cited_by=int(w.get("cited_by_count", 0) or 0),
            abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
            url=str(w.get("id", "")),
        )

    def _cache_path(self, query: str, per_page: int) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{_cache_key(query, per_page)}.json"

    def _read_cache(self, query: str, per_page: int) -> Optional[list[OpenAlexRecord]]:
        path = self._cache_path(query, per_page)
        if path is None or not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text())
            return [OpenAlexRecord(**r) for r in doc.get("records", [])]
        except Exception:  # noqa: BLE001 - corrupt cache is a miss, not fatal
            return None

    def _write_cache(self, query: str, per_page: int, records: list[OpenAlexRecord]) -> None:
        path = self._cache_path(query, per_page)
        if path is None:
            return
        try:
            path.write_text(json.dumps(
                {"query": query, "records": [r.to_dict() for r in records],
                 "cached_at": time.time()}, indent=2), encoding="utf-8")
        except OSError:
            pass


class FakeOpenAlex:
    """Scripted OpenAlex double. script maps query -> records/Exception."""

    def __init__(self, script: Optional[dict[str, Any]] = None):
        self.script = dict(script or {})
        self.queries: list[str] = []

    def search(self, query: str, per_page: int = PER_PAGE_DEFAULT) -> list[OpenAlexRecord]:
        self.queries.append(query)
        if query not in self.script:
            raise AssertionError(f"FakeOpenAlex: unexpected query {query!r}")
        item = self.script[query]
        if isinstance(item, Exception):
            raise item
        return list(item)[:per_page]


def fake_record(openalex_id: str = "https://openalex.org/W1", **over: Any) -> OpenAlexRecord:
    base: dict[str, Any] = {
        "openalex_id": openalex_id,
        "doi": "https://doi.org/10.0000/demo",
        "title": "Demo paper",
        "authors": ["A. Uthor"],
        "year": 2024,
        "cited_by": 7,
        "abstract": "Demo abstract.",
        "url": openalex_id,
    }
    base.update(over)
    return OpenAlexRecord(**base)
