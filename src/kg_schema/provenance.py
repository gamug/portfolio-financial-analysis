"""``code_version()`` -- a short, stable tag for the code that produced a run.

Written onto every run-log row (``analysis_run`` / ``pricing_run`` / ``quant_run``
/ ``cycle_run``) so ``portfolio-reports`` can trace an old run back to the code
that made it. Best-effort: the git short SHA (``-dirty`` when ``src/``, ``skills/``,
``pyproject.toml`` or ``uv.lock`` has an uncommitted change -- see ``_DIRTY_SCOPE``),
falling back to the installed package version, then ``"unknown"``. Never raises.
"""

from __future__ import annotations

import functools
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_TIMEOUT = 5.0

# T-114 PR #92 review: "dirty" means the *executed* code differs from HEAD, not that the
# checkout has any uncommitted file at all -- an untracked review note or a stray .DS_Store
# must not turn every run into a forced --allow-dirty. Scoped to the paths that actually
# reach a running package: the source tree, the ratio-skill markdown a prompt can cite, and
# the two files that pin the resolved dependency set.
_DIRTY_SCOPE = ("src", "skills", "pyproject.toml", "uv.lock")


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
    dirty = _git("status", "--porcelain", "--", *_DIRTY_SCOPE)
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


class DirtyTree(RuntimeError):
    """A production run's ``code_version()`` traces to an uncommitted (dirty) working tree,
    without an explicit override (T-114): its results come from code that ``HEAD`` alone
    cannot reproduce."""


def dirty_tree_reason(version: str | None = None) -> str | None:
    """Why a run should refuse to start, or ``None`` if the tree is clean. *version*
    defaults to :func:`code_version` itself, which already appends ``-dirty`` when ``git
    status --porcelain`` reports anything uncommitted; a fallback tag (no git repo, e.g. an
    installed package) never ends in ``-dirty`` and is never refused over."""
    v = version if version is not None else code_version()
    if not v.endswith("-dirty"):
        return None
    return f"code_version {v} is from an uncommitted (dirty) working tree"
