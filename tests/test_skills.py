"""Loading the skills/*/SKILL.md SOPs that back the specialist agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from fundamental_agent.skills import SPECIALIST_SKILL, load_skill


def test_every_mapped_group_has_a_sop() -> None:
    for group in SPECIALIST_SKILL:
        text = load_skill(group)
        assert text is not None
        assert "SKILL" in text.upper()


def test_unmapped_groups_return_none() -> None:
    assert load_skill("profitability") is None
    assert load_skill("does-not-exist") is None


def test_missing_skills_dir_is_tolerated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    load_skill.cache_clear()
    monkeypatch.setenv("FUNDAMENTAL_SKILLS_DIR", str(tmp_path))
    try:
        assert load_skill("roic") is None
    finally:
        load_skill.cache_clear()
