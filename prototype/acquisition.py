#!/usr/bin/env python3
"""ResearchOS Milestone 8: lawful full-text acquisition for discovery imports.

Approved discovery candidates flow through here BEFORE the existing M7
ingest pipeline: resolve the candidate's advertised PDF URL, fetch the
bytes under strict SSRF/size/type guards, and hand validated bytes to
``paper_pipeline.run_ingest`` (the caller does that last step).

Security policy (fail closed):
- http/https only, no userinfo, default ports only.
- Every redirect hop is re-validated (max 5); meta-refresh/JS are not
  followed (we only fetch one URL chain, never render HTML).
- The destination hostname must resolve, and EVERY resolved A/AAAA
  address must be globally routable: private, loopback, link-local,
  multicast, reserved, and unspecified ranges are all rejected.
- Content-Length is capped before reading; the body stream is capped
  while reading (default: the same RESEARCHOS_MAX_PDF_MB as uploads).
- Content-Type must look like PDF, and the first bytes must be the
  ``%PDF-`` magic. HTML error pages can never pass as papers.
- No cookies, no auth headers, no paywall bypassing. License metadata
  from OpenAlex is preserved on the candidate record by the caller.

Proxy note: when the process would fetch through an HTTP(S) proxy (proxy
env vars + no proxy_bypass for the host), the proxy — not this host —
resolves and connects, so local DNS validation is skipped for that hop:
it would be both ineffective (different resolver view) and wrong (it can
reject names only the proxy can resolve). Scheme/host/port/redirect/
size/type guards still apply. The proxy is then the egress control point.

DNS note (direct connections): the guard resolves each hop before
connecting, but does not pin the connection to the resolved address
(TOCTOU). That matches this local single-user tool's threat model; a
public deployment must fetch through an IP-pinning egress proxy.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT_S = 30.0
MAX_REDIRECTS = 5
READ_CHUNK = 256 * 1024

# Conservative default when the pipeline module is unavailable (tests that
# import this module standalone). The server path always resolves the real
# M7 limit via paper_pipeline.max_pdf_bytes().
_FALLBACK_MAX_PDF_MB = 32


class AcquisitionError(RuntimeError):
    """Full-text fetch failed. ``retryable`` tells the job layer whether a
    researcher retry could plausibly succeed (network flake: yes; SSRF
    rejection or HTML landing page: no)."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class FetchedPDF:
    pdf_bytes: bytes
    final_url: str
    content_type: str
    sha256: str
    size: int


def max_acquisition_bytes() -> int:
    try:
        import paper_pipeline

        return int(paper_pipeline.max_pdf_bytes())
    except (ImportError, AttributeError, ValueError):
        return _FALLBACK_MAX_PDF_MB * 1024 * 1024


def _reject(message: str) -> AcquisitionError:
    return AcquisitionError(message, retryable=False)


def validate_fetch_url(raw_url: str) -> urllib.parse.ParseResult:
    """Parse + SSRF-guard one URL. Returns the parsed URL or raises."""
    url = (raw_url or "").strip()
    if not url:
        raise _reject("empty full-text URL")
    if len(url) > 2048:
        raise _reject("full-text URL exceeds 2048 characters")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as e:
        raise _reject(f"unparseable full-text URL: {e}") from e
    if parts.scheme not in ("http", "https"):
        raise _reject(f"refusing non-http(s) full-text URL scheme "
                      f"'{parts.scheme or '(none)'}'")
    if parts.username or parts.password:
        raise _reject("refusing full-text URL with embedded credentials")
    host = parts.hostname or ""
    if not host:
        raise _reject("full-text URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise _reject("full-text URL has an invalid port") from None
    if port is not None and (
            (parts.scheme == "http" and port != 80)
            or (parts.scheme == "https" and port != 443)):
        raise _reject(f"refusing non-default port {port} on full-text URL")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        # No DNS involved: unambiguous, enforced proxy or not.
        if not literal.is_global:
            raise _reject(f"refusing non-public full-text address '{host}'")
        return parts
    if _uses_proxy(parts.scheme, host):
        return parts
    _require_global_host(host)
    return parts


def _uses_proxy(scheme: str, host: str) -> bool:
    """True when this process would fetch the URL through a proxy."""
    try:
        if urllib.request.proxy_bypass(host):
            return False
    except Exception:  # noqa: BLE001 - conservative: assume direct
        return False
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - conservative: assume direct
        return False
    return bool(proxies.get(scheme))


def _require_global_host(host: str) -> None:
    """Every resolved address must be globally routable."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        if e.errno is not None and e.errno == getattr(socket, "EAI_AGAIN", -3):
            raise AcquisitionError(
                f"temporary DNS failure for '{host}': {e}") from e
        raise _reject(f"full-text host '{host}' does not resolve: {e}") from e
    if not infos:
        raise _reject(f"full-text host '{host}' does not resolve")
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise _reject(f"full-text host '{host}' resolved oddly") from None
        if not addr.is_global:
            raise _reject(f"refusing non-public full-text host '{host}' "
                          f"(resolves to {ip})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001,ANN002 - stdlib signature
        return None


def _build_opener() -> Any:
    # Per-call timeouts are passed to opener.open(); the opener itself is
    # just transport + redirect suppression.
    opener = urllib.request.build_opener(_NoRedirect)
    opener.addheaders = [("User-Agent", "ResearchOS/8 (full-text import)"),
                         ("Accept", "application/pdf")]
    return opener


def _looks_like_pdf_content_type(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in ("application/pdf", "application/octet-stream",
                  "binary/octet-stream", "")


def fetch_pdf(
    url: str,
    *,
    max_bytes: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener_factory: Optional[Callable[[], Any]] = None,
) -> FetchedPDF:
    """Fetch one PDF URL with manual redirect validation.

    ``opener_factory`` exists for deterministic tests (fake transport);
    production always uses the real urllib opener.
    """
    limit = max_bytes if max_bytes is not None else max_acquisition_bytes()
    opener = opener_factory() if opener_factory else _build_opener()
    current = url
    final_parts = None
    for _ in range(MAX_REDIRECTS + 1):
        final_parts = validate_fetch_url(current)
        request = urllib.request.Request(urllib.parse.urlunsplit(final_parts),
                                         method="GET")
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = (e.headers or {}).get("Location", "")
                if not location:
                    raise AcquisitionError(
                        f"redirect without Location from {current}") from e
                current = urllib.parse.urljoin(current, location)
                continue
            if e.code == 429 or 500 <= e.code:
                raise AcquisitionError(f"full-text host returned HTTP {e.code}")
            raise _reject(f"full-text host returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AcquisitionError(f"could not reach full-text host: {e}") from e
        with response:
            status = getattr(response, "status", 200)
            if status in (301, 302, 303, 307, 308):
                location = (response.headers or {}).get("Location", "")
                if not location:
                    raise AcquisitionError(
                        f"redirect without Location from {current}")
                current = urllib.parse.urljoin(current, location)
                continue
            if status != 200:
                if status == 429 or 500 <= status:
                    raise AcquisitionError(
                        f"full-text host returned HTTP {status}")
                raise _reject(f"full-text host returned HTTP {status}")
            headers = response.headers or {}
            length_raw = str(headers.get("Content-Length", "") or "").strip()
            if length_raw:
                try:
                    if int(length_raw) > limit:
                        raise _reject(
                            f"full-text PDF reports {length_raw} bytes; "
                            f"limit is {limit}")
                except ValueError:
                    pass  # untrustworthy header; the stream cap still applies
            content_type = str(headers.get("Content-Type", "") or "")
            if not _looks_like_pdf_content_type(content_type):
                raise _reject(f"full-text URL served '{content_type}', not a PDF")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _reject(f"full-text PDF exceeds {limit} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)
        if len(body) < 5 or not body.startswith(b"%PDF-"):
            raise _reject("downloaded bytes are not a PDF (missing %PDF- magic)")
        return FetchedPDF(
            pdf_bytes=body,
            final_url=urllib.parse.urlunsplit(final_parts),
            content_type=content_type,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )
    raise _reject(f"too many redirects (>{MAX_REDIRECTS}) for full-text URL")


def stage_downloaded_pdf(project_dir: Path, paper_id: str, pdf_bytes: bytes) -> Path:
    """Atomically stage validated PDF bytes as a paper's source.pdf.

    Uses the M7 paper dir layout so the existing ingest path works
    unchanged. Rejects path-unsafe paper ids via workspace.paper_dir.
    """
    import workspace

    pdir = workspace.paper_dir(project_dir, paper_id)
    pdir.mkdir(parents=True, exist_ok=True)
    dest = pdir / "source.pdf"
    fd, tmp = tempfile.mkstemp(prefix="source-", suffix=".pdf", dir=str(pdir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
