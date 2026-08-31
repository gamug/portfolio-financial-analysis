"""Load the ``skills/<name>/SKILL.md`` SOPs used as specialist-agent system prompts.

Each SKILL.md is a forensic-analyst standard operating procedure for one metric. It
is handed to the matching specialist agent as context; the agent is separately told
to answer briefly rather than emit the SOP's full report template.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

# metric-group name -> skills/ subdirectory
SPECIALIST_SKILL = {
    "cashflow": "fcf_margin",
    "leverage": "interest_coverage_ratio",
    "roic": "roic",
    "cagr": "cagr",
    "valuation": "free_cash_flow_yield",
}


def skills_dir() -> Path:
    override = os.environ.get("FUNDAMENTAL_SKILLS_DIR")
    if override:
        return Path(override)
    # src/fundamental_agent/skills.py -> repo root -> skills/
    return Path(__file__).resolve().parents[2] / "skills"


@cache
def load_skill(group: str) -> str | None:
    """Return the SOP text for *group*, or ``None`` when no skill is mapped/present."""
    name = SPECIALIST_SKILL.get(group)
    if name is None:
        return None
    try:
        return (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None
