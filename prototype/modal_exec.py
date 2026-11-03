#!/usr/bin/env python3
"""ResearchOS Milestone 6: Modal Sandbox execution for computational verification.

Runs LLM-generated experiment code inside an isolated Modal Sandbox — never
on the host. One place centralizes the safety policy:

- CPU-only, explicit cpu / memory / lifetime / per-exec time limits
- outbound networking disabled (block_network=True); dependencies are baked
  into the trusted image BEFORE network-blocked execution
- pinned dependency versions on a trusted base image
- host credentials (Modal profile/env, BASETEN_API_KEY) are never passed
  into the sandbox environment
- artifacts are pulled to the host before the sandbox is terminated

Authentication: the standard Modal SDK resolution is used as-is, which
accepts BOTH environment variables (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET)
and the local profile (~/.modal.toml). ResearchOS never requires
credentials to be duplicated into .env. Call verify_auth() before the
first live run to fail fast with a clear message.

For deterministic offline tests, FakeModal implements the same interface
with scripted outcomes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import modal
from modal.exception import SandboxTimeoutError

APP_NAME = "researchos-verify"
WORKDIR = "/work"
EXPERIMENT_FILENAME = "experiment.py"

# Cost-safety limits (CPU-only; one constants block).
CPU_CORES = 2.0
MEMORY_MIB = 4096
SANDBOX_TIMEOUT_S = 1200  # max sandbox lifetime: 20 minutes
SANDBOX_IDLE_TIMEOUT_S = 180
EXEC_TIMEOUT_S = 600  # max single experiment run: 10 minutes
ENV_PROBE_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 200_000  # truncate captured streams beyond this

# Trusted base + pinned dependencies (baked into the image pre-execution).
BASE_PYTHON_VERSION = "3.12"
PINNED_PACKAGES = (
    "numpy==2.1.3",
    "matplotlib==3.9.2",
)


class ModalError(RuntimeError):
    """Modal execution failed (auth, sandbox, timeout, or transfer)."""


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


@dataclass
class ModalRun:
    sandbox_id: str
    image_desc: str
    env_probe: dict[str, Any]
    exec_result: ExecResult
    artifacts: dict[str, bytes] = field(default_factory=dict)
    missing_artifacts: list[str] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


class ModalExec:
    """Real Modal Sandbox runner. All limits come from the constants above."""

    def __init__(
        self,
        *,
        cpu: float = CPU_CORES,
        memory_mib: int = MEMORY_MIB,
        sandbox_timeout_s: int = SANDBOX_TIMEOUT_S,
        exec_timeout_s: int = EXEC_TIMEOUT_S,
    ):
        self.cpu = cpu
        self.memory_mib = memory_mib
        self.sandbox_timeout_s = sandbox_timeout_s
        self.exec_timeout_s = exec_timeout_s

    def verify_auth(self) -> dict[str, str]:
        """Cheap pre-flight: prove Modal credentials work before spending on runs."""
        try:
            app = modal.App.lookup(APP_NAME, create_if_missing=True)
        except Exception as e:  # noqa: BLE001 - message surfaced, never secrets
            raise ModalError(
                "Modal authentication failed. Use `modal token new` (profile) or set "
                f"MODAL_TOKEN_ID / MODAL_TOKEN_SECRET. Detail: {type(e).__name__}: {e}"
            ) from e
        return {"app": APP_NAME, "app_id": getattr(app, "app_id", "?")}

    def build_image(self) -> "modal.Image":
        return modal.Image.debian_slim(python_version=BASE_PYTHON_VERSION).pip_install(
            *PINNED_PACKAGES
        )

    def image_desc(self) -> str:
        return (
            f"debian_slim python={BASE_PYTHON_VERSION} "
            f"pip=[{', '.join(PINNED_PACKAGES)}]"
        )

    def run_experiment(
        self,
        code: str,
        *,
        artifact_paths: list[str],
        thread_id: str = "",
    ) -> ModalRun:
        """Write code into a fresh network-blocked sandbox, execute, pull artifacts.

        Never executes anything on the host. Raises ModalError on
        infrastructure failure; experiment-level failure (nonzero exit) is
        returned normally in ExecResult for the graph to diagnose.
        """
        from spark_open import utc_now_iso  # local import: single time-source helper

        started_at = utc_now_iso()
        try:
            app = modal.App.lookup(APP_NAME, create_if_missing=True)
        except Exception as e:  # noqa: BLE001
            raise ModalError(f"Modal app lookup failed: {type(e).__name__}: {e}") from e
        try:
            sb = modal.Sandbox.create(
                app=app,
                image=self.build_image(),
                cpu=self.cpu,
                memory=self.memory_mib,
                timeout=self.sandbox_timeout_s,
                idle_timeout=SANDBOX_IDLE_TIMEOUT_S,
                block_network=True,
                workdir=WORKDIR,
                name=f"verify-{thread_id}"[:64] if thread_id else None,
            )
        except Exception as e:  # noqa: BLE001
            raise ModalError(f"Sandbox creation failed: {type(e).__name__}: {e}") from e
        try:
            sb.filesystem.write_text(code, f"{WORKDIR}/{EXPERIMENT_FILENAME}")
            env_probe = self._env_probe(sb)
            exec_result = self._exec(sb, ["python", f"{WORKDIR}/{EXPERIMENT_FILENAME}"])
            artifacts: dict[str, bytes] = {}
            missing: list[str] = []
            for path in artifact_paths:
                try:
                    artifacts[path] = sb.filesystem.read_bytes(path)
                except Exception:  # noqa: BLE001 - missing artifact is data, not infra failure
                    missing.append(path)
            sandbox_id = getattr(sb, "object_id", "") or ""
            return ModalRun(
                sandbox_id=sandbox_id,
                image_desc=self.image_desc(),
                env_probe=env_probe,
                exec_result=exec_result,
                artifacts=artifacts,
                missing_artifacts=missing,
                started_at=started_at,
                ended_at=utc_now_iso(),
            )
        finally:
            try:
                sb.terminate()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _env_probe(self, sb: "modal.Sandbox") -> dict[str, Any]:
        """Record exact runtime facts (local-only commands; no network needed)."""
        probe_code = (
            "import json, platform, sys; "
            "print(json.dumps({'python': sys.version.split()[0], "
            "'platform': platform.platform(), 'machine': platform.machine(), "
            "'cpu_count': __import__('os').cpu_count()}))"
        )
        info: dict[str, Any] = {"image": self.image_desc()}
        try:
            p = sb.exec("python", "-c", probe_code, timeout=ENV_PROBE_TIMEOUT_S)
            p.wait()
            info["runtime"] = (p.stdout.read() or "").strip()[:2000]
        except Exception as e:  # noqa: BLE001 - probe failure must not kill the run
            info["runtime_error"] = f"{type(e).__name__}: {e}"
        try:
            p = sb.exec("pip", "freeze", timeout=ENV_PROBE_TIMEOUT_S)
            p.wait()
            info["pip_freeze"] = (p.stdout.read() or "").strip()[:8000]
        except Exception as e:  # noqa: BLE001 - probe failure must not kill the run
            info["pip_freeze_error"] = f"{type(e).__name__}: {e}"
        return info

    def _exec(self, sb: "modal.Sandbox", cmd: list[str]) -> ExecResult:
        t0 = time.time()
        try:
            proc = sb.exec(*cmd, timeout=self.exec_timeout_s)
            try:
                stdout = proc.stdout.read() or ""
                stderr = proc.stderr.read() or ""
                code = proc.wait()
            except SandboxTimeoutError:
                return ExecResult(
                    returncode=-1, stdout="", stderr=_timeout_msg(self.exec_timeout_s),
                    timed_out=True, duration_s=time.time() - t0,
                )
            duration = time.time() - t0
            # Observed live: Modal may kill the exec at the timeout and
            # report returncode -1 WITHOUT raising SandboxTimeoutError.
            timed_out = infer_timeout(code, duration, self.exec_timeout_s)
            if timed_out and not stderr.strip():
                stderr = _timeout_msg(self.exec_timeout_s)
            return ExecResult(
                returncode=code,
                stdout=_truncate(stdout),
                stderr=_truncate(stderr),
                timed_out=timed_out,
                duration_s=duration,
            )
        except SandboxTimeoutError:
            return ExecResult(
                returncode=-1, stdout="", stderr=_timeout_msg(self.exec_timeout_s),
                timed_out=True, duration_s=time.time() - t0,
            )


def _timeout_msg(limit_s: int) -> str:
    return f"exec exceeded {limit_s}s and was terminated"


def infer_timeout(returncode: int, duration_s: float, limit_s: int) -> bool:
    """A nonzero exit at the time limit is a timeout, even when the SDK
    reports it as a plain exit code instead of raising."""
    return returncode != 0 and duration_s >= limit_s * 0.99


class FakeModal:
    """Scripted Modal double: outcomes maps attempt index -> ModalRun/Exception."""

    def __init__(self, outcomes: list[Any]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.auth_verified = False

    def verify_auth(self) -> dict[str, str]:
        self.auth_verified = True
        return {"app": APP_NAME, "app_id": "fake-app"}

    def image_desc(self) -> str:
        return "fake-image"

    def run_experiment(self, code: str, *, artifact_paths: list[str], thread_id: str = "") -> ModalRun:
        import hashlib

        self.calls.append({
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "artifact_paths": list(artifact_paths),
            "thread_id": thread_id,
        })
        if not self.outcomes:
            raise AssertionError("FakeModal: unexpected Modal execution (no scripted outcome)")
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fake_modal_run(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    artifacts: Optional[dict[str, bytes]] = None,
    missing_artifacts: Optional[list[str]] = None,
) -> ModalRun:
    return ModalRun(
        sandbox_id="fake-sandbox",
        image_desc="fake-image",
        env_probe={"image": "fake-image", "runtime": '{"python": "3.12"}',
                   "pip_freeze": "numpy==2.1.3"},
        exec_result=ExecResult(returncode=returncode, stdout=stdout, stderr=stderr,
                               timed_out=timed_out, duration_s=1.0),
        artifacts=artifacts or {},
        missing_artifacts=missing_artifacts or [],
        started_at="2026-11-03T00:00:00+00:00",
        ended_at="2026-11-03T00:00:01+00:00",
    )
