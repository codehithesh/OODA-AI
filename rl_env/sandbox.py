"""DockerSandbox — hardened Python execution container for action_type='python'.

Only invoked from DataAnalystEnv when the agent emits action_type='python'.
The SQL path never passes through here; it goes directly to DuckDBClient
(read-only guarded) in env.py.

Hardening checklist (all required, not optional):
  ✓ wall-clock timeout with hard container kill (not just stop)
  ✓ mem_limit cap (default 256 MB)
  ✓ nano_cpus cap (default 0.5 CPU)
  ✓ network_disabled=True
  ✓ non-root user (nobody, uid=65534)
  ✓ no host mounts — code is injected via a narrow read-only tmpfs bind
  ✓ try/finally teardown even on timeout or exception

Base image: python:3.12-slim  (matches backend requires-python >=3.12)
"""

from __future__ import annotations

import io
import logging
import tarfile
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker  # docker SDK — not subprocess
import docker.errors
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_BASE_IMAGE = "python:3.12-slim"
_NOBODY_UID = 65534  # uid for 'nobody' on Debian/slim images
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_MEM_LIMIT = "256m"
_DEFAULT_NANO_CPUS = int(0.5 * 1e9)  # 0.5 CPU expressed as nano-CPUs
_ENTRYPOINT_FILENAME = "entrypoint.py"


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class SandboxResult:
    """Structured result returned by DockerSandbox.run()."""

    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None  # set if the Docker API itself raised

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------
class DockerSandbox:
    """Run arbitrary Python code in a hardened, disposable Docker container.

    Usage::

        sandbox = DockerSandbox()
        result = sandbox.run("import pandas as pd; print(pd.__version__)")
        assert result.success

    The sandbox is stateless; each call to ``run()`` creates and destroys its
    own container.  There is no persistent state between calls.
    """

    def __init__(
        self,
        *,
        base_image: str = _BASE_IMAGE,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        mem_limit: str = _DEFAULT_MEM_LIMIT,
        nano_cpus: int = _DEFAULT_NANO_CPUS,
        docker_client: Any | None = None,
    ) -> None:
        self.base_image = base_image
        self.timeout_seconds = timeout_seconds
        self.mem_limit = mem_limit
        self.nano_cpus = nano_cpus
        # Accept an injected client (tests), otherwise build from environment.
        self._client: docker.DockerClient = docker_client or docker.from_env()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, code: str) -> SandboxResult:
        """Execute *code* in a fresh, hardened container and return a SandboxResult.

        The container is always removed in the finally block, even if the
        timeout fires or an unexpected exception propagates.
        """
        t0 = time.perf_counter()
        container = None
        try:
            container = self._create_container(code)
            container.start()
            timed_out = False

            try:
                exit_status = container.wait(timeout=self.timeout_seconds)
                raw_exit_code = exit_status.get("StatusCode", -1)
            except Exception:
                # Timeout or connection drop — kill hard, not graceful stop.
                timed_out = True
                raw_exit_code = -1
                try:
                    container.kill()
                except docker.errors.APIError:
                    pass  # already dead

            duration = time.perf_counter() - t0
            stdout = _decode_logs(container.logs(stdout=True, stderr=False))
            stderr = _decode_logs(container.logs(stdout=False, stderr=True))

            result = SandboxResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=raw_exit_code,
                duration_seconds=round(duration, 3),
                timed_out=timed_out,
            )
            logger.info(
                "sandbox_run_complete",
                exit_code=raw_exit_code,
                timed_out=timed_out,
                duration_s=result.duration_seconds,
            )
            return result

        except docker.errors.DockerException as exc:
            duration = time.perf_counter() - t0
            logger.error("sandbox_docker_error", error=str(exc))
            return SandboxResult(
                stdout="",
                stderr="",
                exit_code=-1,
                duration_seconds=round(duration, 3),
                error=str(exc),
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.APIError as exc:
                    logger.warning("sandbox_remove_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _create_container(self, code: str) -> Any:
        """Create (but do not start) a hardened container with *code* injected."""
        # Wrap user code so syntax errors surface in stderr cleanly.
        wrapped = textwrap.dedent(f"""\
            import sys, traceback
            try:
                exec(compile({code!r}, '<sandbox>', 'exec'))
            except Exception:
                traceback.print_exc()
                sys.exit(1)
        """)

        container = self._client.containers.create(
            image=self.base_image,
            command=["python", f"/sandbox/{_ENTRYPOINT_FILENAME}"],
            # Security constraints
            network_disabled=True,
            user=str(_NOBODY_UID),
            read_only=True,  # root filesystem is read-only
            # Resource limits
            mem_limit=self.mem_limit,
            nano_cpus=self.nano_cpus,
            # Writable tmpfs for Python's bytecode cache (__pycache__)
            tmpfs={"/tmp": "size=64m,noexec,nosuid"},
            # No host mounts — code is injected via the Docker cp API below
            detach=True,
            # Drop all Linux capabilities
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )

        # Inject the code into /sandbox/ inside the container via the tar API.
        # This is the only path into the container; there are no bind mounts.
        self._inject_code(container, wrapped)
        return container

    @staticmethod
    def _inject_code(container: Any, code: str) -> None:
        """Write *code* into /sandbox/entrypoint.py inside *container* via tar stream."""
        encoded = code.encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=_ENTRYPOINT_FILENAME)
            info.size = len(encoded)
            info.mode = 0o444  # read-only inside the container
            tar.addfile(info, io.BytesIO(encoded))
        buf.seek(0)
        # /sandbox must exist; create it via a separate tar entry
        buf2 = io.BytesIO()
        with tarfile.open(fileobj=buf2, mode="w") as tar:
            dir_info = tarfile.TarInfo(name="sandbox")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o555
            tar.addfile(dir_info)
            file_info = tarfile.TarInfo(name=f"sandbox/{_ENTRYPOINT_FILENAME}")
            file_info.size = len(encoded)
            file_info.mode = 0o444
            tar.addfile(file_info, io.BytesIO(encoded))
        buf2.seek(0)
        container.put_archive("/", buf2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _decode_logs(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")
    return str(raw)
