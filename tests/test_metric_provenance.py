"""Every metric carries a provenance block (T-151 part 3, D14)."""

from __future__ import annotations

import json
from typing import Any

from fundamental_agent.metrics import provenance
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.metrics.efficiency import compute as efficiency_compute
from fundamental_agent.metrics.profitability import compute as profitability_compute
from fundamental_agent.metrics.roic import compute as roic_compute
from fundamental_agent.statements import Statements

G = "us-gaap_"
KEY = "2024-12-31 (FY)"


def _row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    return {
        "concept": concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        **periods,
    }


def _payload(*, prior_equity: float = 400.0, operating_income: float | None = 100.0) -> Statements:
    income = [
        _row(G + "NetIncomeLoss", "Net income", **{KEY: 80.0}),
    ]
    if operating_income is not None:
        income.append(
            _row(G + "OperatingIncomeLoss", "Operating income", **{KEY: operating_income})
        )
    stmts = Statements.from_payload(
        {
            "income_statement": income,
            "balance_sheet": [
                _row(G + "Assets", "Assets", **{"2024-12-31": 1_100.0, "2023-12-31": 1_000.0}),
                _row(
                    G + "StockholdersEquity",
                    "Equity",
                    **{"2024-12-31": 450.0, "2023-12-31": prior_equity},
                ),
            ],
            "cash_flow": [],
        }
    )
    stmts.accession = "0000320193-24-000001"
    return stmts


def test_a_filled_metric_carries_its_provenance() -> None:
    stmts = _payload()
    results = profitability_compute(stmts, KEY)
    provenance.attach([("profitability", r) for r in results], stmts, KEY)
    roe = next(r for r in results if r.name == "return_on_equity")
    block = roe.inputs["provenance"]
    assert block["accession"] == "0000320193-24-000001"
    assert block["period"] == KEY
    assert "MD-10" in block["rule_ids"]
    assert block["empty_reason"] is None
    assert block["items"]["net_income"]["concept"] == G + "NetIncomeLoss"
    assert block["items"]["net_income"]["how"] == "concept"
    assert block["items"]["equity"]["concept"] == G + "StockholdersEquity"


def test_an_empty_metric_carries_a_reason_from_the_fixed_list() -> None:
    stmts = _payload(prior_equity=-10.0)
    results = profitability_compute(stmts, KEY)
    provenance.attach([("profitability", r) for r in results], stmts, KEY)
    roe = next(r for r in results if r.name == "return_on_equity")
    assert roe.value is None
    block = roe.inputs["provenance"]
    assert block["empty_reason"] in provenance.EMPTY_REASONS
    assert block["empty_reason"] == provenance.NOT_APPLICABLE  # APP-07, negative book equity
    assert "flag_return_on_equity_negative_equity" in block["flags"]


def test_a_missing_input_reason_is_the_default() -> None:
    # inventory_turnover with no cogs and no inventory: a plain missing input, no flag
    stmts = _payload(operating_income=None)
    results = efficiency_compute(stmts, KEY)
    provenance.attach([("efficiency", r) for r in results], stmts, KEY)
    turnover = next(r for r in results if r.name == "inventory_turnover")
    assert turnover.value is None
    assert turnover.inputs["provenance"]["empty_reason"] == provenance.MISSING_INPUT


def test_a_capture_failure_reason_comes_from_the_flags() -> None:
    # roic on a payload with no debt: the invested capital is a capture failure
    stmts = _payload()
    results = roic_compute(stmts, KEY)
    provenance.attach([("roic", r) for r in results], stmts, KEY)
    roic = next(r for r in results if r.name == "return_on_invested_capital")
    assert roic.value is None
    assert roic.inputs["provenance"]["empty_reason"] in provenance.EMPTY_REASONS


def test_attach_covers_every_result() -> None:
    stmts = _payload()
    results = profitability_compute(stmts, KEY)
    provenance.attach([("profitability", r) for r in results], stmts, KEY)
    for result in results:
        assert "provenance" in result.inputs


def test_the_declarations_are_consistent() -> None:
    # every metric with items also has rule IDs, and vice versa
    assert set(provenance.ITEMS) == set(provenance.RULE_IDS)
    assert provenance.MISSING_INPUT in provenance.EMPTY_REASONS


def test_provenance_is_json_serialisable() -> None:
    stmts = _payload()
    results = profitability_compute(stmts, KEY)
    provenance.attach([("profitability", r) for r in results], stmts, KEY)
    for result in results:
        json.dumps(result.inputs)


def test_a_metric_with_no_declaration_gets_an_empty_item_map() -> None:
    # a hypothetical metric not in the declarations still gets a block (with no items)
    result = MetricResult("not_a_metric", 1.0, "ratio", {})
    block = provenance.build(_payload(), KEY, result)
    assert block["items"] == {}
    assert block["rule_ids"] == []
    assert block["empty_reason"] is None
