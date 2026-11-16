"""Load the ``skills/<group>/SKILL.md`` notes used as specialist-agent system prompts.

Each skill is the interpretation a specialist reads beside its group's already-computed numbers: how to read
them, what a flag or an empty value means, and what is not applicable to which company type. The numbers
themselves are computed by :mod:`fundamental_agent.metrics`; a skill states no formula (T-076, D2). The two
generated data files beside this module -- ``metric_definitions.csv`` --
carries the checklist's own definitions into the prompt, and :mod:`kg_schema.applicability` carries the
applicability matrix (T-071 part 1 moved it there so every package reads one copy).
"""

from __future__ import annotations

import csv
import os
import re
from functools import cache
from pathlib import Path

from kg_schema import applicability

# The nine specialist groups, and the metrics each group's ``compute`` returns (T-076, D2). The metric names
# are pinned to the code by ``tests/test_skills.py``, which runs every group on a filing fixture.
GROUP_METRICS: dict[str, tuple[str, ...]] = {
    "profitability": (
        "gross_margin",
        "operating_margin",
        "net_margin",
        "return_on_assets",
        "return_on_equity",
    ),
    "liquidity": ("current_ratio", "quick_ratio", "cash_ratio"),
    "leverage": (
        "debt_to_equity",
        "debt_to_assets",
        "interest_coverage",
        "net_debt_to_ebitda",
        "ebitda",
    ),
    "efficiency": ("asset_turnover", "inventory_turnover", "receivables_turnover"),
    "growth": (
        "revenue_growth",
        "operating_income_growth",
        "net_income_growth",
        "free_cash_flow_growth",
    ),
    "cashflow": (
        "operating_cash_flow_margin",
        "free_cash_flow_margin",
        "free_cash_flow_conversion",
        "capex_intensity",
    ),
    "roic": (
        "effective_tax_rate",
        "nopat",
        "return_on_invested_capital",
        "return_on_invested_capital_effective_rate",
    ),
    "cagr": ("revenue_cagr", "net_income_cagr", "operating_cash_flow_cagr"),
    "valuation": (
        "market_capitalization",
        "enterprise_value",
        "free_cash_flow_yield",
        "sbc_adjusted_fcf_yield",
        "enterprise_fcf_yield",
    ),
}

# group -> the ``skills/`` subdirectory holding its SKILL.md (one skill per specialist group).
SPECIALIST_SKILL: dict[str, str] = {group: group for group in GROUP_METRICS}

_DATA = Path(__file__).resolve().parent / "data"
_MD_REFERENCE_RE = re.compile(r"\bMD-\d+\b")


def skills_dir() -> Path:
    override = os.environ.get("FUNDAMENTAL_SKILLS_DIR")
    if override:
        return Path(override)
    # src/fundamental_agent/skills.py -> repo root -> skills/
    return Path(__file__).resolve().parents[2] / "skills"


@cache
def load_skill(group: str) -> str | None:
    """Return the SKILL.md text for *group*, or ``None`` when no skill is mapped/present."""
    name = SPECIALIST_SKILL.get(group)
    if name is None:
        return None
    try:
        return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None


@cache
def _definitions() -> dict[str, dict[str, str]]:
    """``{metric: {md_id, formula, basis}}`` from the generated ``metric_definitions.csv``."""
    with (_DATA / "metric_definitions.csv").open() as handle:
        return {row["metric"]: row for row in csv.DictReader(handle)}


@cache
def _applicability() -> dict[str, list[dict[str, str]]]:
    """``{metric: [row]}`` from the checklist's matrix (:mod:`kg_schema.applicability`), in column order."""
    rows: dict[str, list[dict[str, str]]] = {}
    for name in _matrix_metrics():
        rows[name] = [
            {"metric": name, "company_type": column, "code": code, "rule": rule}
            for column, (code, rule) in applicability.cells(name).items()
        ]
    return rows


def _matrix_metrics() -> tuple[str, ...]:
    return applicability.metrics()


def group_definitions(group: str) -> list[dict[str, str]]:
    """The generated definition rows for *group*'s metrics, plus the building-block rows their formulas name.

    A metric's formula cites a building block by its ``MD-nn`` id (``invested capital (MD-02)``), so the block
    has to carry that row too, or the definition cannot be read.
    """
    definitions = _definitions()
    own = [definitions[name] for name in GROUP_METRICS.get(group, ()) if name in definitions]
    referenced = {
        ref
        for row in own
        for ref in _MD_REFERENCE_RE.findall(row["formula"])
        if ref != row["md_id"]
    }
    blocks = sorted(
        (row for row in definitions.values() if row["md_id"] in referenced),
        key=lambda row: row["md_id"],
    )
    return own + blocks


def group_applicability(group: str) -> list[dict[str, str]]:
    """The generated applicability rows for *group*'s metrics the checklist's matrix covers."""
    matrix = _applicability()
    return [row for name in GROUP_METRICS.get(group, ()) for row in matrix.get(name, [])]
