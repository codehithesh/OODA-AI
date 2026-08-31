"""Reads the git-versioned agent context directory.

The ``context/`` directory is its own git repository (mounted as a volume in
Docker). Every DecisionLog row stores the commit SHA returned here, and a
ContextSnapshot row stores the full file manifest so any decision can be
reproduced against the exact context it was made with.

When the directory is not a git repository (or git is unavailable) the client
falls back to a deterministic SHA-256 content hash so the audit trail never
degrades to "unknown".
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from config import get_settings

logger = structlog.get_logger(__name__)


class ContextManifest(BaseModel):
    """Version identity + file hash manifest of the context directory."""

    commit_sha: str
    files: dict[str, str]
    file_count: int
    source: str  # "git" | "content-hash"


class GitContextClient:
    """Resolves the version of the git-versioned context directory."""

    def __init__(self, context_dir: Path | None = None) -> None:
        self.context_dir = context_dir or get_settings().context_path

    # ---------------------------------------------------------------- git
    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.context_dir), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    def commit_sha(self) -> str:
        """HEAD commit SHA of the context repo, or a content hash fallback."""
        try:
            return self._git("rev-parse", "HEAD")
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("git_sha_unavailable_fallback_to_content_hash", error=str(exc))
            return self.content_hash()

    # --------------------------------------------------------------- hash
    def _iter_files(self) -> list[Path]:
        if not self.context_dir.exists():
            return []
        return sorted(
            p for p in self.context_dir.rglob("*") if p.is_file() and ".git" not in p.parts
        )

    def content_hash(self) -> str:
        """Deterministic SHA-256 over the file tree (fallback identity)."""
        digest = hashlib.sha256()
        for path in self._iter_files():
            rel = path.relative_to(self.context_dir).as_posix()
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{rel}:{file_hash}\n".encode())
        return digest.hexdigest()

    # ----------------------------------------------------------- manifest
    def manifest(self) -> ContextManifest:
        """Commit SHA plus a {relative_path: sha256[:16]} manifest."""
        try:
            sha = self._git("rev-parse", "HEAD")
            source = "git"
        except (subprocess.SubprocessError, OSError):
            sha = self.content_hash()
            source = "content-hash"
        files: dict[str, str] = {}
        for path in self._iter_files():
            rel = path.relative_to(self.context_dir).as_posix()
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        return ContextManifest(commit_sha=sha, files=files, file_count=len(files), source=source)

    async def amanifest(self) -> ContextManifest:
        """Thread-offloaded manifest read for async callers."""
        return await asyncio.to_thread(self.manifest)


_default_client: GitContextClient | None = None


def get_git_context_client(config: dict[str, Any] | None = None) -> GitContextClient:
    """Context client for this node run: config override first, then default."""
    if config is not None:
        configurable = config.get("configurable") or {}
        override = configurable.get("git_context_client")
        if override is not None:
            return override
    global _default_client
    if _default_client is None:
        _default_client = GitContextClient()
    return _default_client
