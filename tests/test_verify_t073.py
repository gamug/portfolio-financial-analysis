"""``scripts/verify_t073.py``: the master-side fallback loader and the sensitivity helpers.

The measurements' "before" side is the master commit's own ``_fallback_assessment``, executed by the
script (AGENTS.md: run the old code through the same method). These tests pin the loader to that
rule's semantics, so a wrong load cannot pass as the old code.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fundamental_agent import pillars
from fundamental_agent.metrics import CORE_GROUPS, OPTIONAL_GROUPS

_SPEC = importlib.util.spec_from_file_location(
    "verify_t073", Path(__file__).resolve().parent.parent / "scripts" / "verify_t073.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_t073"] = verify
_SPEC.loader.exec_module(verify)


def _master_or_skip() -> Any:
    """The master commit's ``agents`` module, or a skip when this clone has no history for it (the
    pytest job checks out the branch alone, ``actions/checkout`` with no ``fetch-depth``)."""
    try:
        return verify.master_agents()
    except subprocess.CalledProcessError:
        pytest.skip(f"{verify.MASTER_COMMIT} is not in this clone's history")


def test_the_master_fallback_is_the_master_commits_own_flat_rule() -> None:
    """T-076's fallback, before T-073: 50 plus the flat metric keys' nudges, the leverage penalty
    and the withheld-value rule -- not a pillar total."""
    master = _master_or_skip()
    assert master._fallback_assessment({"profitability.net_margin": 0.20}).score == 62.0
    assert master._fallback_assessment({"profitability.net_margin": 0.01}).score == 38.0
    assert master._fallback_assessment({"leverage.debt_to_equity": 4.0}).score == 38.0
    assert (
        master._fallback_assessment(
            {"profitability.net_margin": 0.20}, {("profitability", "net_margin"): "DQ_MARGIN"}
        ).score
        == 50.0
    )


def test_the_master_synthesis_prompt_is_the_masters() -> None:
    """It asked for a score; the branch asks for the pillar ratings instead."""
    master_prompt = verify._master_synthesis_prompt(_master_or_skip())
    assert '"score"' in master_prompt
    assert "pillars" not in master_prompt


def test_spearman_is_one_for_a_series_with_itself_and_minus_one_reversed() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert verify._spearman(values, values) == 1.0
    assert verify._spearman(values, list(reversed(values))) == -1.0
    assert verify._spearman([1.0], [1.0]) == 0.0


def test_the_distribution_reports_distinct_values_and_the_mode_share() -> None:
    out = verify._distribution([50.0, 50.0, 62.0])
    assert out["filings"] == 3
    assert out["distinct_values"] == 2
    assert out["top_10"][0] == {"score": 50.0, "filings": 2}
    assert out["share_at_the_most_common_value"] == 2 / 3


def test_the_pillar_names_and_group_order_are_the_pipelines() -> None:
    """The brief rebuilds in the pipeline's own group order, and the four names come from the
    shared contract: a drift here would render a different prompt."""
    assert (*CORE_GROUPS, *OPTIONAL_GROUPS, "valuation") == verify.GROUP_ORDER
    assert tuple(pillars.PILLARS) == pillars.PILLAR_NAMES
