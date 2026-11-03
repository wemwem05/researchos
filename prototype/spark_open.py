#!/usr/bin/env python3
"""ResearchOS Milestone 5: Muse Spark Open client via Baseten.

Centralizes everything about the hackathon open-weight model path:

- base URL (Baseten OpenAI-compatible endpoint)
- EXACT model ID (the hackathon key is scoped to this ID only)
- authentication via BASETEN_API_KEY (never hardcoded, never logged)
- latency measurement + structured telemetry per call
- structured JSON response handling with one bounded repair attempt

This module NEVER falls back to another model. If Spark Open is unreachable,
misconfigured, or returns unusable output, callers get a clear SparkOpenError.

The pre-existing PDF extraction/localization pipeline keeps using the hosted
Meta Model API (see analyze_paper.py); only the agentic research workflow
(investigator / critic / revision) uses this client.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional, TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

from analyze_paper import extract_json

# Hackathon Baseten configuration. Do NOT change the model ID:
# the hackathon key works specifically with meta-models/muse-spark-open-1026.
BASE_URL = "https://inference.baseten.co/v1"
MODEL_ID = "meta-models/muse-spark-open-1026"
PROVIDER = "Baseten"
API_KEY_ENV_VAR = "BASETEN_API_KEY"

# Spark Open is a reasoning model: it spends completion tokens on reasoning
# before emitting text, so generation budgets must be generous.
DEFAULT_MAX_TOKENS = 8000

# Bounded structured-output repair: at most ONE correction attempt per call.
MAX_REPAIRS = 1


class SparkOpenError(RuntimeError):
    """Spark Open call failed; no fallback to any other model is attempted.

    Carries the failure's telemetry record (if measurable) so callers can
    persist it alongside successful calls.
    """

    def __init__(self, message: str, telemetry: Optional["TelemetryRecord"] = None):
        super().__init__(message)
        self.telemetry = telemetry


class TelemetryRecord(BaseModel):
    """One Spark Open call, safe to persist (never contains credentials)."""

    timestamp: str
    provider: str = PROVIDER
    model: str = MODEL_ID
    thread_id: str
    node: str
    latency_ms: int = Field(ge=0)
    success: bool
    error: Optional[str] = None
    retry_count: int = Field(ge=0, default=0)
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    request_id: Optional[str] = None


T = TypeVar("T", bound=BaseModel)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_api_key() -> str:
    load_dotenv()
    key = os.environ.get(API_KEY_ENV_VAR, "")
    if not key:
        raise SparkOpenError(
            f"{API_KEY_ENV_VAR} is not set. Put it in the environment or a "
            "local (never-committed) .env file. No fallback model will be used."
        )
    return key


class SparkOpenClient:
    """Thin authenticated wrapper around the Baseten OpenAI-compatible API."""

    def __init__(self, api_key: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS):
        self._api_key = api_key if api_key is not None else load_api_key()
        if not self._api_key:
            raise SparkOpenError(f"{API_KEY_ENV_VAR} is empty; refusing to call any other model.")
        self._client = OpenAI(base_url=BASE_URL, api_key=self._api_key)
        self.max_tokens = max_tokens

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def base_url(self) -> str:
        return BASE_URL

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        node: str,
        thread_id: str,
    ) -> tuple[T, TelemetryRecord]:
        """Request structured JSON and validate it. One bounded repair retry.

        Returns (parsed_model, telemetry). Raises SparkOpenError on transport
        failure or after the repair budget is exhausted. Never substitutes
        another model.
        """
        t0 = time.time()
        retries = 0
        last_error: Optional[str] = None
        try:
            content = self._chat(messages)
            try:
                return (
                    schema.model_validate_json(extract_json(content or "")),
                    self._telemetry(thread_id, node, t0, True, None, retries),
                )
            except Exception as e:  # noqa: BLE001 - repair path below needs the message
                last_error = f"{type(e).__name__}: {e}"
            # Bounded repair: show the model its error + schema, ask once more.
            retries = 1
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content or ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply failed validation with this error:\n"
                        f"{last_error}\n\n"
                        "Reply again with ONLY a corrected JSON object matching "
                        "the required schema. No commentary, no markdown fences."
                    ),
                },
            ]
            content = self._chat(repair_messages)
            try:
                parsed = schema.model_validate_json(extract_json(content or ""))
            except Exception as e:  # noqa: BLE001 - message surfaced in SparkOpenError
                raise SparkOpenError(
                    f"Spark Open ({node}) returned invalid structured output after "
                    f"{MAX_REPAIRS} repair attempt: {e}",
                    self._telemetry(thread_id, node, t0, False, f"invalid output: {e}", retries),
                ) from e
            return parsed, self._telemetry(thread_id, node, t0, True, None, retries)
        except SparkOpenError:
            raise
        except Exception as e:  # noqa: BLE001 - transport/API failures become SparkOpenError
            ms = int((time.time() - t0) * 1000)
            raise SparkOpenError(
                f"Spark Open call failed in node '{node}' after {ms}ms: "
                f"{type(e).__name__}: {e}. No fallback model was used.",
                self._telemetry(thread_id, node, t0, False, f"{type(e).__name__}: {e}", retries),
            ) from e

    def _chat(self, messages: list[dict[str, Any]]) -> str:
        resp = self._client.chat.completions.create(
            model=MODEL_ID, messages=messages, max_tokens=self.max_tokens
        )
        # Stash usage where complete_json can read it (single-threaded CLI use).
        self._last_usage = resp.usage
        self._last_request_id = getattr(resp, "id", None)
        return resp.choices[0].message.content or ""

    def _telemetry(
        self,
        thread_id: str,
        node: str,
        t0: float,
        success: bool,
        error: Optional[str],
        retries: int,
    ) -> TelemetryRecord:
        last_usage = getattr(self, "_last_usage", None)
        return TelemetryRecord(
            timestamp=utc_now_iso(),
            thread_id=thread_id,
            node=node,
            latency_ms=int((time.time() - t0) * 1000),
            success=success,
            error=error,
            retry_count=retries,
            prompt_tokens=getattr(last_usage, "prompt_tokens", None),
            completion_tokens=getattr(last_usage, "completion_tokens", None),
            total_tokens=getattr(last_usage, "total_tokens", None),
            request_id=getattr(self, "_last_request_id", None),
        )


def verify_connectivity() -> TelemetryRecord:
    """One minimal real call proving the hackathon model is reachable."""
    client = SparkOpenClient()

    class _Ping(BaseModel):
        ok: bool

    _, tel = client.complete_json(
        [
            {"role": "system", "content": "Return JSON only, no commentary."},
            {"role": "user", "content": 'Reply with exactly: {"ok": true}'},
        ],
        _Ping,
        node="connectivity_probe",
        thread_id="connectivity-probe",
    )
    return tel
