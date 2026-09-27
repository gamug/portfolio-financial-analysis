"""code_version(): git short SHA -> package version -> "unknown", never raises."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from kg_schema import provenance


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """Clears the ``lru_cache`` before every test in this file (each one mocks its own
    git state), and re-primes it with the same safe, deterministic value
    ``tests/conftest.py``'s session-scoped fixture originally established, after every
    test. Without the second half, a test here that computes a *real* value (the
    ``_DIRTY_SCOPE`` tests below run actual ``git`` against a throwaway repo) would leave
    that real value cached process-wide, leaking into every other test file's pipeline
    tests that assume an ambient clean `code_version()` and never explicitly mock it."""
    provenance.code_version.cache_clear()
    yield
    provenance.code_version.cache_clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provenance, "_git_version", lambda: "testclean")
        mp.setattr(provenance, "_package_version", lambda: None)
        provenance.code_version()


def _fake_run(dirty: bool) -> Any:
    def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        is_rev_parse = argv[3] == "rev-parse"
        out = "abc1234" if is_rev_parse else (" M src/x.py" if dirty else "")
        return subprocess.CompletedProcess(argv, 0, stdout=out + "\n", stderr="")

    return run


def test_uses_git_sha_with_dirty_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(dirty=True))
    assert provenance.code_version() == "abc1234-dirty"


def test_clean_tree_has_no_dirty_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(dirty=False))
    assert provenance.code_version() == "abc1234"


def test_falls_back_when_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(provenance, "_package_version", lambda: "pkg-9.9.9")
    assert provenance.code_version() == "pkg-9.9.9"

    provenance.code_version.cache_clear()
    monkeypatch.setattr(provenance, "_package_version", lambda: None)
    assert provenance.code_version() == "unknown"


def test_dirty_tree_reason_is_none_for_a_clean_version() -> None:
    assert provenance.dirty_tree_reason("abc1234") is None


def test_dirty_tree_reason_names_the_dirty_version() -> None:
    reason = provenance.dirty_tree_reason("abc1234-dirty")
    assert reason is not None
    assert "abc1234-dirty" in reason


def test_dirty_tree_reason_defaults_to_code_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_run(dirty=True))
    assert provenance.dirty_tree_reason() is not None
    provenance.code_version.cache_clear()
    monkeypatch.setattr(subprocess, "run", _fake_run(dirty=False))
    assert provenance.dirty_tree_reason() is None


def test_a_fallback_tag_is_never_treated_as_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pkg-*``/``unknown`` never end in ``-dirty`` -- a database with no git repo at all
    (an installed package) must not be refused over a guard that can't apply to it."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(provenance, "_package_version", lambda: None)
    assert provenance.dirty_tree_reason() is None


# -- dirty scope (T-114 PR #92 review): "dirty" is scoped to the paths that reach a
# running package, not any uncommitted file in the checkout -- exercised against a real,
# throwaway git repo rather than a mocked subprocess, so the actual pathspec is what's
# under test.


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - constant argv, no shell, no user input
        ["git", *args],  # noqa: S607 - git resolved from PATH by design
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_scoped_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "skills").mkdir()
    (root / "src" / "x.py").write_text("x = 1\n")
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "uv.lock").write_text("")
    (root / "README.md").write_text("hello\n")
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "t@example.com")
    _run_git(root, "config", "user.name", "t")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "init")


def test_an_untracked_file_outside_the_dirty_scope_stays_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_scoped_repo(tmp_path)
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    (tmp_path / "notes_scratch.md").write_text("wip\n")  # untracked, outside the dirty scope

    assert not provenance.code_version().endswith("-dirty")


def test_an_edit_under_src_makes_the_tree_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_scoped_repo(tmp_path)
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    (tmp_path / "src" / "x.py").write_text("x = 2\n")  # tracked file, edited

    assert provenance.code_version().endswith("-dirty")


def test_an_untracked_file_under_skills_makes_the_tree_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_scoped_repo(tmp_path)
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    (tmp_path / "skills" / "new_skill.md").write_text("# new\n")  # untracked, in scope

    assert provenance.code_version().endswith("-dirty")
