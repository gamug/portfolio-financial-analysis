"""T-113 (PR #91 review): the prompt-version hash identifies which prompts/model config
produced a score -- constant across every filing in a run, not a per-filing transcript hash."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from fundamental_agent import agents
from fundamental_agent.agents import FundamentalAnalyst, _prompt_version_hash
from fundamental_agent.metrics import provenance
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.skills import GROUP_METRICS
from fundamental_agent.statements import Statements


def _analyst(model_name: str = "deepseek-chat") -> FundamentalAnalyst:
    return FundamentalAnalyst(cast("Any", None), model_name)


def test_two_filings_in_one_run_share_the_same_prompt_hash() -> None:
    """One analyst instance serves every filing in a run; its prompt_hash is fixed at
    construction from the prompt templates + model config alone, never re-derived per filing,
    so any two filings it scores necessarily share it."""
    analyst = _analyst()
    assert analyst.prompt_hash == _prompt_version_hash("deepseek-chat")
    # re-deriving independently for a second "filing" gives the identical value
    assert _prompt_version_hash("deepseek-chat") == analyst.prompt_hash


def test_changing_one_specialist_prompt_changes_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-076: a specialist's prompt is its skill (and the generated blocks), so rewriting one skill
    changes the version hash. Replaces the T-113 version that patched the removed inline prompts."""
    before = _prompt_version_hash("deepseek-chat")
    real = agents.load_skill
    monkeypatch.setattr(
        agents,
        "load_skill",
        lambda group: (
            "a rewritten profitability specialist prompt"
            if group == "profitability"
            else real(group)
        ),
    )
    after = _prompt_version_hash("deepseek-chat")
    assert before != after


def test_changing_the_model_id_changes_the_hash() -> None:
    assert _prompt_version_hash("deepseek-chat") != _prompt_version_hash("deepseek-reasoner")


# -- T-076, D3: the 10-Q basis paragraph -------------------------------------------------------------------

_BASIS_LABELS = {
    "ttm": "trailing twelve months",
    "period": "this filing's own quarter",
    "prior_year": "the prior fiscal year end",
    "prior": "the same quarter a year earlier",
}


def _labels(items: dict[str, Any]) -> set[str]:
    return {
        _BASIS_LABELS[basis]
        for bases in items.values()
        for basis in ((bases,) if isinstance(bases, str) else bases)
    }


@pytest.mark.parametrize("group", sorted(GROUP_METRICS))
def test_the_basis_paragraph_names_exactly_each_metrics_bases(group: str) -> None:
    results = [
        MetricResult(name, 1.0, "usd" if name in {"nopat", "ebitda"} else "ratio", {})
        for name in GROUP_METRICS[group]
    ]
    paragraph = agents.basis_paragraph(group, results)
    assert paragraph
    for line in paragraph.splitlines()[1:]:
        name, _, rest = line.partition(": ")
        expected = _labels(provenance.ITEMS.get(name, {}))
        assert expected, (group, name)
        assert {label for label in _BASIS_LABELS.values() if f"({label})" in rest} == expected, (
            group,
            name,
        )


def test_a_ten_q_brief_carries_the_basis_paragraph_and_a_ten_k_does_not(
    aapl_10k: Statements,
) -> None:
    results = {"profitability": [MetricResult("net_margin", 0.1, "ratio", {})]}
    ten_k = agents.FilingContext(
        "AAPL", "Apple", "10-K", "FY2023", aapl_10k, "2023-09-30 (FY)", None
    )
    ten_q = replace(ten_k, form="10-Q")
    assert "Basis of the numbers below" not in agents._brief(ten_k, results)
    assert "Basis of the numbers below" in agents._brief(ten_q, results)


# -- T-076, D4: an empty metric reaches the brief with its reason -------------------------------------------


def test_an_empty_metric_reaches_the_brief_with_its_reason() -> None:
    empty = MetricResult(
        "quick_ratio", None, "ratio", {"provenance": {"empty_reason": "not_applicable"}}
    )
    assert agents._metric_lines("liquidity", [empty]) == ["quick_ratio: empty (not_applicable)"]
    assert agents._metric_lines("liquidity", [MetricResult("cash_ratio", 1.5, "ratio", {})]) == [
        "cash_ratio: 1.5"
    ]


# -- T-076, D5: a HARD DQ screen withholds a value -----------------------------------------------------------


def _all_present() -> dict[str, Any]:
    """T-073: a filing whose four pillars all carry a value, for the fallback's own tests."""
    return agents.pillars.pillar_statuses(
        [
            (
                pillar,
                MetricResult(
                    name,
                    1.0,
                    "ratio",
                    {"current_assets": 100.0, "current_liabilities": 50.0}
                    if name == "current_ratio"
                    else {},
                ),
            )
            for pillar, names in agents.pillars.PILLARS.items()
            for name in names
        ]
    )


def test_a_hard_screen_withholds_a_value_from_the_model_and_the_fallback() -> None:
    # |net_margin| > 5 trips DQ_MARGIN (Annex A-01): the value is withheld, not read.
    result = MetricResult("net_margin", 6.0, "ratio", {})
    withheld = agents.withheld_metrics([("profitability", result)])
    assert withheld == {("profitability", "net_margin"): "DQ_MARGIN"}
    assert agents._metric_lines("profitability", [result], withheld) == [
        "net_margin: withheld (DQ_MARGIN)"
    ]
    # T-073 (D3): the fallback rates each pillar 50 plus its own metrics' nudges, and the total is
    # the weighted pillar ratings -- so the net-margin rule's 12 points lift profitability to 62 and
    # the total to (62 + 50 + 50 + 50) / 4. Withheld, they lift nothing and the total stays 50.
    statuses = _all_present()
    lifted = agents._fallback_synthesis({"profitability.net_margin": 6.0}, statuses)
    assert lifted.assessment.score == 53.0
    assert lifted.pillars["pillars"]["profitability"]["rating"] == 62
    assert (
        agents._fallback_synthesis(
            {"profitability.net_margin": 6.0}, statuses, withheld
        ).assessment.score
        == 50.0
    )


def test_a_clean_metric_is_not_withheld() -> None:
    result = MetricResult("net_margin", 0.1, "ratio", {})
    assert agents.withheld_metrics([("profitability", result)]) == {}


def test_an_empty_value_keeps_its_own_reason_when_a_gate_would_quarantine_it() -> None:
    """K5: a negative-equity filing can trip DQ_NEG_EQUITY, which quarantines D/E and ROE. Both are already
    empty (APP-07), so neither is relabelled as a data-quality matter -- negative equity is not a data error:
    ROE keeps its own `not_applicable` reason."""
    results = [
        (
            "profitability",
            MetricResult(
                "return_on_equity",
                None,
                "ratio",
                {"equity": -1.0, "provenance": {"empty_reason": "not_applicable"}},
            ),
        ),
        ("leverage", MetricResult("debt_to_equity", None, "ratio", {"equity": -1.0})),
        (
            "leverage",
            MetricResult("net_debt_to_ebitda", 8.0, "x", {"total_debt": 9.0, "cash": 1.0}),
        ),
    ]
    assert agents.withheld_metrics(results) == {}
    assert agents._metric_lines("profitability", [results[0][1]], {}) == [
        "return_on_equity: empty (not_applicable)"
    ]
