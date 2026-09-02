"""``code_version()`` -- a short, stable tag for the code that produced a run.

Written onto every run-log row (``analysis_run`` / ``pricing_run`` / ``quant_run``
/ ``cycle_run``) so ``portfolio-reports`` can trace an old run back to the code
that made it. Best-effort: the git short SHA (``-dirty`` when the tree has
uncommitted changes), falling back to the installed package version, then
``"unknown"``. Never raises.
"""

from __future__ import annotations

import functools
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT = 5.0


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - constant argv, no shell, no user input
            ["git", "-C", str(_REPO_ROOT), *args],  # noqa: S607 - git resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _git_version() -> str | None:
    sha = _git("rev-parse", "--short", "HEAD")
    if not sha:
        return None
    dirty = _git("status", "--porcelain")
    return f"{sha}-dirty" if dirty else sha


def _package_version() -> str | None:
    try:
        return f"pkg-{version('portfolio-financial-analysis')}"
    except PackageNotFoundError:
        return None


@functools.lru_cache(maxsize=1)
def code_version() -> str:
    """Short tag for the running code. Cached for the process lifetime."""
    return _git_version() or _package_version() or "unknown"
