"""The specialist skills (T-076, D2).

Reads only tracked files: the nine ``skills/<group>/SKILL.md`` notes, ``skills/SOURCES.md`` and the
generated ``src/fundamental_agent/data/checklist_rules.csv``. A skill states no formula, no extraction
procedure, no output template and no threshold no rule states; every bullet cites the rule or the loaded
source that backs it.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

from fundamental_agent.metrics import COMPUTERS, valuation
from fundamental_agent.skills import (
    GROUP_METRICS,
    SPECIALIST_SKILL,
    group_applicability,
    group_definitions,
    load_skill,
)
from fundamental_agent.statements import Statements

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
SOURCES = SKILLS / "SOURCES.md"
RULES = REPO / "src" / "fundamental_agent" / "data" / "checklist_rules.csv"

# A skill may not contain a formula, an extraction procedure, an output template or a persona.
BANNED = ("$", "=", "extract", "step", "template", "persona")
_CITATION_RE = re.compile(r"\[[^\]]+\]")
_DIGIT_RE = re.compile(r"\d")
_KEY = "2023-09-30 (FY)"
_PRIOR = "2022-09-24 (FY)"


def _rules() -> dict[str, str]:
    with RULES.open() as handle:
        return {row["id"]: row["rule"] for row in csv.DictReader(handle)}


def _source_keys() -> set[str]:
    """The source citations SOURCES.md carries, as a skill writes them: ``C7 p.12``."""
    keys = set()
    for line in SOURCES.read_text().splitlines():
        if not line.startswith("| ") or line.startswith(("| Source |", "|---")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4:
            keys.add(f"{cells[0]} {cells[1]}")
    return keys


def _bullets(text: str) -> list[str]:
    """Every bullet of a skill, with its wrapped continuation lines joined."""
    bullets: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                bullets.append(" ".join(current))
            current = [stripped]
        elif current is not None and stripped and not stripped.startswith("#"):
            current.append(stripped)
        else:
            if current is not None:
                bullets.append(" ".join(current))
            current = None
    if current is not None:
        bullets.append(" ".join(current))
    return bullets


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_every_group_has_a_skill(group: str) -> None:
    text = load_skill(group)
    assert text is not None, group
    assert SPECIALIST_SKILL[group] == group


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_a_skill_states_no_formula_and_no_procedure(group: str) -> None:
    text = load_skill(group) or ""
    lowered = text.lower()
    for word in BANNED:
        assert word not in lowered, (group, word)
    # A digit may appear only inside a citation -- a rule ID or a source citation.
    without_citations = _CITATION_RE.sub("", text)
    assert _DIGIT_RE.search(without_citations) is None, (group, without_citations)


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_every_bullet_cites_its_backing(group: str) -> None:
    bullets = _bullets(load_skill(group) or "")
    assert bullets
    known = set(_rules()) | _source_keys()
    for bullet in bullets:
        citations = _CITATION_RE.findall(bullet)
        assert citations, bullet
        assert any(citation[1:-1] in known for citation in citations), bullet


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_every_metric_of_the_group_is_named(group: str, aapl_10k: Statements) -> None:
    text = load_skill(group) or ""
    if group == "valuation":
        names = [result.name for result in valuation.compute(aapl_10k, _KEY)]
    else:
        names = [result.name for result in COMPUTERS[group](aapl_10k, _KEY, _PRIOR, None)]
    assert names
    for name in names:
        assert name in text, (group, name)


def test_the_generated_blocks_carry_the_groups_data() -> None:
    for group in GROUP_METRICS:
        definitions = group_definitions(group)
        applicability = group_applicability(group)
        assert definitions, group
        assert all(
            row["basis"] in {"Sourced", "Inference", "Source pending"} for row in definitions
        )
        assert all(row["code"] in {"A", "ADJ", "NA"} for row in applicability)


# -- T-076 fix round 1 (K1, K2) ------------------------------------------------------------------------------


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_no_skill_restates_applicability(group: str) -> None:
    """K1: the generated matrix block is the single source; a skill defines the reason code only."""
    text = load_skill(group) or ""
    assert "not_applicable" in text
    assert "not applicable" not in text.lower(), group
    headings = [line.lower() for line in text.splitlines() if line.startswith("## ")]
    assert not any("applicab" in heading for heading in headings), (group, headings)


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_no_skill_tells_the_model_to_compare_with_history_or_peers(group: str) -> None:
    """K2: the specialist sees only its own filing's numbers."""
    text = (load_skill(group) or "").lower()
    for word in ("history", "peer"):
        assert word not in text, (group, word)


def test_sources_rows_and_skill_citations_agree() -> None:
    """Fix round 2: every SOURCES.md row backs a bullet that exists, and every source citation in a skill
    has a row (the source and its page, not a rule ID)."""
    rows = _source_keys()
    cited: set[str] = set()
    for group in GROUP_METRICS:
        for bullet in _bullets(load_skill(group) or ""):
            for citation in _CITATION_RE.findall(bullet):
                key = " ".join(citation[1:-1].split())
                if key not in _rules():
                    cited.add(key)
    assert rows == cited, (sorted(rows - cited), sorted(cited - rows))
