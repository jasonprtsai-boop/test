"""Runtime build identity used by durable game records and release checks."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, SITE_ROOT, AppConfig


@lru_cache(maxsize=32)
def _hash_snapshot(path_text: str, size: int, modified_ns: int) -> str:
    del size, modified_ns  # They intentionally form part of the cache key.
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _hash_snapshot(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=4)
def git_identity(project_root_text: str) -> tuple[str, bool | None]:
    """Return commit and tracked-dirty state without making Git a runtime dependency."""
    override = os.getenv("TMCHESS_BUILD_REVISION", "").strip()
    if override:
        return override, None
    root = Path(project_root_text)
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    return revision or "unknown", bool(status.strip())


def runtime_build_identity(config: AppConfig) -> dict[str, Any]:
    revision, dirty = git_identity(str(PROJECT_ROOT.resolve()))
    manifest = PROJECT_ROOT / "release_manifest.json"
    return {
        "python_version": platform.python_version(),
        "revision": revision,
        "tracked_worktree_dirty": dirty,
        "release_manifest_sha256": sha256_file(manifest),
        "requirements_lock_sha256": sha256_file(SITE_ROOT / "requirements.lock"),
        "yolo_model_sha256": sha256_file(config.vision.model_path),
        "pikafish_executable_sha256": sha256_file(config.engine.executable),
        "pikafish_nnue_sha256": sha256_file(config.engine.nnue),
    }
