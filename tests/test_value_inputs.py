"""D1 (T-071 part 2b): the VALORIZATION value-input rule input.

Every value is the one the filing's metric rows already carry, on the ratios' basis (a 10-K's
fiscal year, a 10-Q's trailing twelve months), so VALORIZATION v2 can price its measures on the
analysis date instead of a period-end price. The row lives outside the metric dictionary, the
applicability matrix and the prompt.
"""

from __future__ import annotations

import json

from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import agents, value_inputs
from fundamental_agent.agents import FilingContext, FundamentalAnalyst
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements
from kg_schema.versions import (
    METRIC_GROUPS,
    RULE_INPUT_GROUP,
    VALUE_INPUTS_BOOK_EQUITY,
    VALUE_INPUTS_CASH,
    VALUE_INPUTS_EBITDA,
    VALUE_INPUTS_MD38,
    VALUE_INPUTS_METRIC,
    VALUE_INPUTS_MISSING,
    VALUE_INPUTS_NCI,
    VALUE_INPUTS_NET_INCOME,
    VALUE_INPUTS_STI,
    VALUE_INPUTS_TOTAL_DEBT,
)


def _computed(
    stmts: Statements,
    period_key: str,
    *,
    form: str = "10-K",
    ttm_flows: dict[str, TTMFlow] | None = None,
    price: ClosePrice | None = None,
) -> list[tuple[str, MetricResult]]:
    ctx = FilingContext(
        "AAPL",
        "Apple",
        form,
        "FY2023",
        stmts,
        period_key,
        None,
        price=price or ClosePrice(period_key[:10], 100.0),
        ttm_flows=ttm_flows or {},
    )
    return FundamentalAnalyst(None, "test-model")._compute_all(ctx)


def _inputs(computed: list[tuple[str, MetricResult]]) -> dict[str, object]:
    return value_inputs.build(computed).inputs


def _carrier(
    computed: list[tuple[str, MetricResult]], group: str, name: str
) -> MetricResult | None:
    return next((r for g, r in computed if g == group and r.name == name), None)


# -- the values are the metric rows' own ---------------------------------------------------------


def test_the_inputs_are_the_values_the_metric_rows_carry(aapl_10k: Statements) -> None:
    computed = _computed(aapl_10k, "2023-09-30 (FY)")
    got = _inputs(computed)
    ni = _carrier(computed, "profitability", "return_on_assets")
    ebitda = _carrier(computed, "leverage", "ebitda")
    md38 = _carrier(computed, "valuation", "enterprise_fcf_yield")
    equity = _carrier(computed, "leverage", "debt_to_equity")
    ev = _carrier(computed, "valuation", "enterprise_value")
    assert got[VALUE_INPUTS_NET_INCOME] == ni.inputs["net_income_ttm"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_EBITDA] == ebitda.value  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_MD38] == md38.inputs["md38_numerator"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_BOOK_EQUITY] == equity.inputs["equity"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_TOTAL_DEBT] == ev.inputs["total_debt"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_NCI] == ev.inputs["nci"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_CASH] == ev.inputs["cash"]  # type: ignore[union-attr]
    assert got[VALUE_INPUTS_STI] == ev.inputs["short_term_investments"]  # type: ignore[union-attr]


def test_the_ev_bridge_flags_ride_along(aapl_10k: Statements) -> None:
    got = _inputs(_computed(aapl_10k, "2023-09-30 (FY)"))
    flags = got.get("flags")
    assert isinstance(flags, dict)
    assert flags  # the bridge's own `flag_*` keys
    assert all(k.startswith("flag_") for k in flags)


def test_a_ten_q_carries_the_trailing_year_not_the_quarter(msft_10q: Statements) -> None:
    key = max(msft_10q.quarter_periods(), key=lambda p: p.date).key
    quarter = msft_10q.get("net_income", key)
    assert quarter is not None
    ttm = {"net_income": TTMFlow(4.0 * quarter + 7.0, "ytd")}
    computed = _computed(msft_10q, key, form="10-Q", ttm_flows=ttm)
    ni = _carrier(computed, "profitability", "return_on_assets")
    got = _inputs(computed)
    assert ni is not None
    # the group's own inputs: the quarter stays, the trailing year is what the rule input takes
    assert ni.inputs["net_income"] == quarter
    assert got[VALUE_INPUTS_NET_INCOME] == 4.0 * quarter + 7.0


# -- MET-00: a missing term is empty, with its reason ---------------------------------------------


def test_a_missing_term_is_empty_and_carries_its_reason() -> None:
    computed = [
        (
            "profitability",
            MetricResult(
                "return_on_assets",
                None,
                "ratio",
                {"provenance": {"empty_reason": "capture_failure"}},
            ),
        ),
        ("leverage", MetricResult("ebitda", None, "usd", {"provenance": {"empty_reason": "x"}})),
    ]
    got = _inputs(computed)
    assert got[VALUE_INPUTS_NET_INCOME] is None
    assert got[VALUE_INPUTS_EBITDA] is None
    missing = got[VALUE_INPUTS_MISSING]
    assert isinstance(missing, dict)
    assert missing[VALUE_INPUTS_NET_INCOME] == "capture_failure"
    # a carrier the filing has no row for at all
    assert missing[VALUE_INPUTS_MD38] == "missing_input"


def test_a_quarter_times_four_gives_no_net_income() -> None:
    computed = [
        (
            "profitability",
            MetricResult(
                "return_on_assets",
                1.0,
                "ratio",
                {"net_income_ttm": 400.0, "annualized_x4": 1.0, "provenance": {}},
            ),
        )
    ]
    assert _inputs(computed)[VALUE_INPUTS_NET_INCOME] is None


# -- outside the metric dictionary, the matrix and the prompt --------------------------------------


def test_it_is_a_rule_input_not_a_metric_and_never_reaches_the_prompt(aapl_10k: Statements) -> None:
    assert VALUE_INPUTS_METRIC not in METRIC_GROUPS
    computed = _computed(aapl_10k, "2023-09-30 (FY)")
    flat = {f"{g}.{r.name}": r.value for g, r in computed}
    assert not any(VALUE_INPUTS_METRIC in key for key in flat)
    by_group = agents._group(computed)
    brief = agents._brief(
        FilingContext("AAPL", "Apple", "10-K", "FY2023", aapl_10k, "2023-09-30 (FY)", None),
        by_group,
    )
    for key in (
        VALUE_INPUTS_NET_INCOME,
        VALUE_INPUTS_MD38,
        VALUE_INPUTS_BOOK_EQUITY,
        VALUE_INPUTS_TOTAL_DEBT,
        VALUE_INPUTS_STI,
        VALUE_INPUTS_METRIC,
    ):
        assert key not in brief


# -- the writer ------------------------------------------------------------------------------------


def test_write_stores_one_idempotent_rule_input_row(
    memory_db: Database, aapl_10k: Statements
) -> None:
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAPL')")
    fid = seed_filing(conn, 1, period_end="2023-09-30", filing_date="2023-11-03")
    computed = _computed(aapl_10k, "2023-09-30 (FY)")
    assert value_inputs.write(conn, fid, computed) == 1
    value_inputs.write(conn, fid, computed)  # idempotent
    rows = list(
        conn.execute(
            "SELECT metric_group, metric_name, value, inputs_json FROM fundamental_metrics "
            "WHERE filing_id = ? AND metric_group = ?",
            (fid, RULE_INPUT_GROUP),
        )
    )
    assert len(rows) == 1
    assert rows[0]["metric_name"] == VALUE_INPUTS_METRIC
    inputs = json.loads(rows[0]["inputs_json"])
    assert inputs[VALUE_INPUTS_NET_INCOME] == rows[0]["value"]  # the row's own scalar
