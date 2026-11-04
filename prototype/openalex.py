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

Milestone 8 extends this client (additively — the audit path is
unchanged): richer record fields (venue, dates, arXiv id, OA/pdf URLs,
license, work type), single-work and batch fetch for citation expansion,
bounded exponential-backoff retries, and a client-side throttle.
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

# Hard per-claim budgets (Milestone 6.1 audit path; unchanged).
MAX_QUERIES = 2
MAX_RECORDS = 10
MAX_SELECTED = 3
PER_PAGE_DEFAULT = 5
REQUEST_TIMEOUT_S = 20
MAX_ABSTRACT_CHARS = 3000

# Milestone 8 transport policy: bounded exponential backoff + throttle.
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 8.0)
MAX_RETRY_AFTER_S = 30.0
MIN_CALL_INTERVAL_S = 0.5
MAX_BATCH_IDS = 25

# Default field selection: full enough for discovery candidates, small
# enough to keep payloads cheap. New fields only (audit parsing untouched).
DEFAULT_SELECT = (
    "id,doi,title,display_name,authorships,publication_year,"
    "publication_date,cited_by_count,abstract_inverted_index,"
    "primary_location,best_oa_location,open_access,type,ids"
)


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
    # Milestone 8 additions (all optional; old caches still load).
    venue: str = ""
    publication_date: str = ""
    arxiv_id: str = ""
    pdf_url: str = ""
    landing_page: str = ""
    license: str = ""
    work_type: str = ""
    is_oa: bool = False

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
            "venue": self.venue,
            "publication_date": self.publication_date,
            "arxiv_id": self.arxiv_id,
            "pdf_url": self.pdf_url,
            "landing_page": self.landing_page,
            "license": self.license,
            "work_type": self.work_type,
            "is_oa": self.is_oa,
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


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


class OpenAlexClient:
    """Keyless-by-default OpenAlex search with disk cache and fail-soft errors."""

    def __init__(self, cache_dir: Optional[Path] = None,
                 mailto: Optional[str] = None, api_key: Optional[str] = None,
                 min_interval_s: float = MIN_CALL_INTERVAL_S):
        self.cache_dir = cache_dir
        self.mailto = mailto if mailto is not None else os.environ.get(MAILTO_ENV_VAR, "")
        # Never log the key; it only goes into request params.
        self.api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV_VAR, "")
        self.min_interval_s = max(0.0, min_interval_s)
        self._last_call = 0.0
        self.api_calls = 0  # real HTTP calls (cache hits excluded)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, query: str, per_page: int = PER_PAGE_DEFAULT,
               select: Optional[str] = None, page: int = 1,
               max_per_page: int = MAX_RECORDS) -> list[OpenAlexRecord]:
        query = (query or "").strip()
        if not query:
            raise OpenAlexError("empty query")
        per_page = max(1, min(per_page, max_per_page))
        page = max(1, page)
        key = _cache_key("search", query, str(per_page), str(page), select or "")
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        params = self._params({"search": query, "per-page": str(per_page),
                               "page": str(page)}, select)
        doc = self._get_json(f"{OPENALEX_BASE}/works", params)
        records = [self._parse_work(w) for w in doc.get("results", [])[:per_page]]
        self._write_cache(key, query, records)
        return records

    def search_filtered(self, filters: dict[str, str],
                        per_page: int = PER_PAGE_DEFAULT,
                        select: Optional[str] = None) -> list[OpenAlexRecord]:
        """Filter-based lookup (e.g. {"cites": "W123"} for citing works)."""
        if not filters:
            raise OpenAlexError("empty filters")
        per_page = max(1, min(per_page, MAX_BATCH_IDS))
        filt = ",".join(f"{k}:{v}" for k, v in sorted(filters.items()))
        key = _cache_key("filter", filt, str(per_page), select or "")
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        params = self._params({"filter": filt, "per-page": str(per_page)}, select)
        doc = self._get_json(f"{OPENALEX_BASE}/works", params)
        records = [self._parse_work(w) for w in doc.get("results", [])[:per_page]]
        self._write_cache(key, filt, records)
        return records

    def get_work(self, openalex_id: str,
                 select: Optional[str] = None) -> OpenAlexRecord:
        """Fetch one work by id (W123 or full URL)."""
        bare = (openalex_id or "").strip().rsplit("/", 1)[-1]
        if not bare:
            raise OpenAlexError("empty work id")
        key = _cache_key("work", bare, select or "")
        cached = self._read_cache(key)
        if cached is not None and cached:
            return cached[0]
        params = self._params({}, select)
        doc = self._get_json(
            f"{OPENALEX_BASE}/works/{urllib.parse.quote(bare, safe='')}", params)
        record = self._parse_work(doc)
        self._write_cache(key, bare, [record])
        return record

    def works_batch(self, openalex_ids: list[str],
                    select: Optional[str] = None) -> list[OpenAlexRecord]:
        """Fetch up to MAX_BATCH_IDS works in ONE call (OR filter)."""
        bare = [str(i).strip().rsplit("/", 1)[-1] for i in (openalex_ids or [])
                if str(i).strip()]
        if not bare:
            return []
        bare = bare[:MAX_BATCH_IDS]
        key = _cache_key("batch", "|".join(sorted(bare)), select or "")
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        params = self._params({"filter": "openalex:" + "|".join(bare),
                               "per-page": str(len(bare))}, select)
        doc = self._get_json(f"{OPENALEX_BASE}/works", params)
        records = [self._parse_work(w) for w in doc.get("results", [])]
        self._write_cache(key, ",".join(bare), records)
        return records

    def referenced_ids(self, openalex_id: str, limit: int = 25) -> list[str]:
        """Referenced-work OpenAlex ids (for citation expansion)."""
        bare = (openalex_id or "").strip().rsplit("/", 1)[-1]
        if not bare:
            raise OpenAlexError("empty work id")
        key = _cache_key("refs", bare)
        path = self._cache_path(key)
        if path is not None and path.is_file():
            try:
                doc = json.loads(path.read_text())
                return [str(i) for i in doc.get("ids", [])][:limit]
            except Exception:  # noqa: BLE001 - corrupt cache is a miss
                pass
        params = self._params({}, "id,referenced_works")
        doc = self._get_json(
            f"{OPENALEX_BASE}/works/{urllib.parse.quote(bare, safe='')}", params)
        ids = [str(i).rsplit("/", 1)[-1]
               for i in (doc.get("referenced_works") or [])[:limit]
               if str(i)]
        if path is not None:
            try:
                path.write_text(json.dumps({"ids": ids}, indent=2), encoding="utf-8")
            except OSError:
                pass
        return ids

    # -- internals ------------------------------------------------------

    def _params(self, extra: dict[str, str], select: Optional[str]) -> dict[str, str]:
        params = dict(extra)
        params["select"] = select or DEFAULT_SELECT
        if self.mailto:
            params["mailto"] = self.mailto
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        last_error = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                req = urllib.request.Request(
                    full, headers={"User-Agent": self._user_agent()})
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                    self._last_call = time.monotonic()
                    self.api_calls += 1
                    if resp.status != 200:
                        raise OpenAlexError(f"HTTP {resp.status}")
                    doc = json.loads(resp.read().decode("utf-8"))
                    if not isinstance(doc, dict):
                        raise OpenAlexError("malformed response (not an object)")
                    return doc
            except urllib.error.HTTPError as e:
                self._last_call = time.monotonic()
                if e.code == 429:
                    last_error = "rate limited (429)"
                    self._sleep_backoff(attempt, self._retry_after(e))
                    continue
                if e.code is not None and 500 <= e.code < 600:
                    last_error = f"HTTP {e.code}"
                    self._sleep_backoff(attempt, None)
                    continue
                raise OpenAlexError(f"HTTP {e.code}") from e
            except OpenAlexError:
                raise
            except Exception as e:  # noqa: BLE001 - timeouts/DNS are transient
                last_error = f"request failed: {type(e).__name__}: {e}"
                self._sleep_backoff(attempt, None)
                continue
        raise OpenAlexError(f"{last_error}; giving up after {MAX_ATTEMPTS} attempts")

    def _sleep_backoff(self, attempt: int, retry_after: Optional[float]) -> None:
        if attempt + 1 >= MAX_ATTEMPTS:
            return
        if retry_after is not None:
            time.sleep(min(retry_after, MAX_RETRY_AFTER_S))
        elif attempt < len(BACKOFF_S):
            time.sleep(BACKOFF_S[attempt])

    @staticmethod
    def _retry_after(error: urllib.error.HTTPError) -> Optional[float]:
        try:
            raw = (error.headers or {}).get("Retry-After", "")
            value = float(str(raw).strip())
            return value if value >= 0 else None
        except (TypeError, ValueError, AttributeError):
            return None

    def _user_agent(self) -> str:
        who = f"mailto {self.mailto}" if self.mailto else "mailto unset"
        return f"ResearchOS/8 ({who})"

    @staticmethod
    def _parse_work(w: dict[str, Any]) -> OpenAlexRecord:
        authorships = w.get("authorships", []) or []
        authors = []
        for a in authorships[:12]:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)
        primary = w.get("primary_location") or {}
        best_oa = w.get("best_oa_location") or {}
        source = primary.get("source") or {}
        oa = w.get("open_access") or {}
        ids = w.get("ids") or {}
        arxiv = str(ids.get("arxiv") or "")
        if arxiv and "/" in arxiv:
            arxiv = arxiv.rsplit("/", 1)[-1]
        return OpenAlexRecord(
            openalex_id=str(w.get("id", "")),
            doi=str(w.get("doi", "") or ""),
            title=str(w.get("title") or w.get("display_name") or ""),
            authors=authors,
            year=w.get("publication_year"),
            cited_by=int(w.get("cited_by_count", 0) or 0),
            abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
            url=str(w.get("id", "")),
            venue=str(source.get("display_name") or ""),
            publication_date=str(w.get("publication_date") or ""),
            arxiv_id=arxiv,
            pdf_url=str(primary.get("pdf_url") or best_oa.get("pdf_url") or ""),
            landing_page=str(primary.get("landing_page_url")
                             or best_oa.get("landing_page_url") or ""),
            license=str(primary.get("license") or ""),
            work_type=str(w.get("type") or ""),
            is_oa=bool(oa.get("is_oa", False)),
        )

    def _cache_path(self, key: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> Optional[list[OpenAlexRecord]]:
        path = self._cache_path(key)
        if path is None or not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text())
            out = []
            for r in doc.get("records", []):
                if isinstance(r, dict):
                    # Drop unknown keys so newer caches never break old reads.
                    known = {f for f in OpenAlexRecord.__dataclass_fields__}
                    out.append(OpenAlexRecord(**{k: v for k, v in r.items() if k in known}))
            return out
        except Exception:  # noqa: BLE001 - corrupt cache is a miss, not fatal
            return None

    def _write_cache(self, key: str, query: str, records: list[OpenAlexRecord]) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.write_text(json.dumps(
                {"query": query, "records": [r.to_dict() for r in records],
                 "cached_at": time.time()}, indent=2), encoding="utf-8")
        except OSError:
            pass


class FakeOpenAlex:
    """Scripted OpenAlex double.

    script maps query -> records/Exception for search(), plus:
      "work:<bare-id>" -> OpenAlexRecord/Exception for get_work(),
      "batch" -> records/Exception for works_batch(),
      "cites:<bare-id>" -> records/Exception for search_filtered(cites=...),
      "refs:<bare-id>" -> list-of-ids/Exception for referenced_ids().
    """

    def __init__(self, script: Optional[dict[str, Any]] = None):
        self.script = dict(script or {})
        self.queries: list[str] = []
        self.calls: list[tuple[str, Any]] = []

    def _play(self, key: str) -> Any:
        if key not in self.script:
            raise AssertionError(f"FakeOpenAlex: unexpected call {key!r}")
        item = self.script[key]
        if isinstance(item, Exception):
            raise item
        return item

    def search(self, query: str, per_page: int = PER_PAGE_DEFAULT,
               select: Optional[str] = None, page: int = 1,
               max_per_page: int = MAX_RECORDS) -> list[OpenAlexRecord]:
        self.queries.append(query)
        self.calls.append(("search", query))
        return list(self._play(query))[:per_page]

    def search_filtered(self, filters: dict[str, str],
                        per_page: int = PER_PAGE_DEFAULT,
                        select: Optional[str] = None) -> list[OpenAlexRecord]:
        self.calls.append(("search_filtered", dict(filters)))
        if set(filters) == {"cites"}:
            return list(self._play(f"cites:{filters['cites']}"))[:per_page]
        return list(self._play(f"filter:{sorted(filters.items())}"))[:per_page]

    def get_work(self, openalex_id: str,
                 select: Optional[str] = None) -> OpenAlexRecord:
        bare = (openalex_id or "").strip().rsplit("/", 1)[-1]
        self.calls.append(("get_work", bare))
        return self._play(f"work:{bare}")

    def works_batch(self, openalex_ids: list[str],
                    select: Optional[str] = None) -> list[OpenAlexRecord]:
        self.calls.append(("works_batch", list(openalex_ids or [])))
        return list(self._play("batch"))

    def referenced_ids(self, openalex_id: str, limit: int = 25) -> list[str]:
        bare = (openalex_id or "").strip().rsplit("/", 1)[-1]
        self.calls.append(("referenced_ids", bare))
        return list(self._play(f"refs:{bare}"))[:limit]


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
        "venue": "Demo Venue",
        "publication_date": "2024-01-15",
        "arxiv_id": "",
        "pdf_url": "",
        "landing_page": openalex_id,
        "license": "",
        "work_type": "article",
        "is_oa": False,
    }
    base.update(over)
    return OpenAlexRecord(**base)
