"""code_version(): git short SHA -> package version -> "unknown", never raises."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from kg_schema import provenance


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    provenance.code_version.cache_clear()


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
