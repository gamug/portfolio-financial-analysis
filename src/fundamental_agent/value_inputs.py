"""The VALORIZATION value inputs (D1, T-071 part 2b).

VALORIZATION v2 (D2) prices its four measures on the **analysis date** (MKT-01), so it needs each
filing's numerators and its enterprise-value bridge *independently of any price*. Today the
numerators exist only inside metrics that were already divided by a period-end price, and a
period-end market cap that is missing makes the stored yield empty even when the market cap on D
exists.

This module stores them as a *rule input* -- a ``fundamental_metrics`` row under the pseudo-group
:data:`GROUP`, which is not one of ``kg_schema.versions.METRIC_GROUPS``, so no metric consumer
reads it, it is not a pillar member, it is **not shown to the model** (``agents.flat_metrics``
carries the metric *values* only, and this is not one of them), and it has no applicability-matrix
row.

**Every value is the one the filing's metric rows already carry**, read from the results just
computed -- never re-derived:

===================  ===================================================================
input                carrier (its own ``inputs_json``)
===================  ===================================================================
net income           ``profitability.return_on_assets``'s ``net_income_ttm`` (a 10-K's fiscal
                     year, a 10-Q's real trailing twelve months; a quarter-times-four
                     (``annualized_x4``) gives none -- that is the quarter again)
EBITDA               MD-01's persisted ``leverage.ebitda`` (the TTM on a 10-Q)
MD-38 numerator      ``valuation.enterprise_fcf_yield``'s ``md38_numerator`` -- NOPAT -
                     (capex - D&A + Δ non-cash working capital), the value
                     ``valuation._md38`` itself returned
book equity          ``leverage.debt_to_equity``'s ``equity``: parent stockholders' equity at the
                     filing's balance-sheet date, the same resolved value MD-10/MD-15 use
the EV bridge        ``valuation.enterprise_value``'s ``total_debt``, ``nci``, ``cash`` and
                     ``short_term_investments``, with the ``flag_*`` keys they carry
===================  ===================================================================

**Book equity's two declared differences from C4** (the checklist leaves both pending): balance-sheet
deferred taxes are **not** added, and preferred stock is **not** subtracted (as in MD-35).

**A missing term leaves its input empty** (MET-00), with the reason the carrier metric recorded
(``provenance.empty_reason``) in :data:`VALUE_INPUTS_MISSING`.
"""

from __future__ import annotations

from typing import Any, Final

from portfolio_common.db import Database

from fundamental_agent import db as agent_db
from fundamental_agent.metrics.base import MetricResult
from kg_schema.versions import (
    RULE_INPUT_GROUP,
    VALUE_INPUTS_BOOK_EQUITY,
    VALUE_INPUTS_CASH,
    VALUE_INPUTS_EBITDA,
    VALUE_INPUTS_FLAGS,
    VALUE_INPUTS_MD38,
    VALUE_INPUTS_METRIC,
    VALUE_INPUTS_MISSING,
    VALUE_INPUTS_NCI,
    VALUE_INPUTS_NET_INCOME,
    VALUE_INPUTS_STI,
    VALUE_INPUTS_TOTAL_DEBT,
)

GROUP: Final[str] = RULE_INPUT_GROUP
NAME: Final[str] = VALUE_INPUTS_METRIC

#: The metric row that carries each input, as ``(group, name)``.
NET_INCOME_CARRIER: Final[tuple[str, str]] = ("profitability", "return_on_assets")
EBITDA_CARRIER: Final[tuple[str, str]] = ("leverage", "ebitda")
MD38_CARRIER: Final[tuple[str, str]] = ("valuation", "enterprise_fcf_yield")
EQUITY_CARRIER: Final[tuple[str, str]] = ("leverage", "debt_to_equity")
EV_CARRIER: Final[tuple[str, str]] = ("valuation", "enterprise_value")

#: input key -> its carrier, for the empty-reason lookup.
_CARRIER_OF: Final[dict[str, tuple[str, str]]] = {
    VALUE_INPUTS_NET_INCOME: NET_INCOME_CARRIER,
    VALUE_INPUTS_EBITDA: EBITDA_CARRIER,
    VALUE_INPUTS_MD38: MD38_CARRIER,
    VALUE_INPUTS_BOOK_EQUITY: EQUITY_CARRIER,
    VALUE_INPUTS_TOTAL_DEBT: EV_CARRIER,
    VALUE_INPUTS_NCI: EV_CARRIER,
    VALUE_INPUTS_CASH: EV_CARRIER,
    VALUE_INPUTS_STI: EV_CARRIER,
}


def _carrier(computed: list[tuple[str, MetricResult]], key: tuple[str, str]) -> MetricResult | None:
    group, name = key
    for g, result in computed:
        if g == group and result.name == name:
            return result
    return None


def _number(value: Any) -> float | None:
    """*value* as a float, or ``None`` -- ``bool`` is not a number here (a flag is not a value)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _reason(carrier: MetricResult | None) -> str:
    """Why an input is empty: the carrier metric's own ``provenance.empty_reason``, else
    ``missing_input`` (a carrier the filing has no row for at all)."""
    if carrier is None:
        return "missing_input"
    return (carrier.inputs.get("provenance") or {}).get("empty_reason") or "missing_input"


def build(computed: list[tuple[str, MetricResult]]) -> MetricResult:
    """The rule input for one filing, from the metric results just computed.

    The row's own ``value`` is the trailing-year parent net income (the first input); every other
    value is in ``inputs_json``."""
    ni = _carrier(computed, NET_INCOME_CARRIER)
    ebitda = _carrier(computed, EBITDA_CARRIER)
    md38 = _carrier(computed, MD38_CARRIER)
    equity = _carrier(computed, EQUITY_CARRIER)
    ev = _carrier(computed, EV_CARRIER)
    ev_inputs: dict[str, Any] = dict(ev.inputs) if ev is not None else {}

    net_income = None
    annualized = ni is not None and bool(ni.inputs.get("annualized_x4"))
    if ni is not None and not annualized:
        net_income = _number(ni.inputs.get(VALUE_INPUTS_NET_INCOME))

    inputs: dict[str, Any] = {
        VALUE_INPUTS_NET_INCOME: net_income,
        VALUE_INPUTS_EBITDA: _number(ebitda.value if ebitda is not None else None),
        VALUE_INPUTS_MD38: _number(md38.inputs.get(VALUE_INPUTS_MD38) if md38 else None),
        VALUE_INPUTS_BOOK_EQUITY: _number(equity.inputs.get("equity") if equity else None),
        VALUE_INPUTS_TOTAL_DEBT: _number(ev_inputs.get(VALUE_INPUTS_TOTAL_DEBT)),
        VALUE_INPUTS_NCI: _number(ev_inputs.get(VALUE_INPUTS_NCI)),
        VALUE_INPUTS_CASH: _number(ev_inputs.get(VALUE_INPUTS_CASH)),
        VALUE_INPUTS_STI: _number(ev_inputs.get(VALUE_INPUTS_STI)),
    }
    carriers = {
        NET_INCOME_CARRIER: ni,
        EBITDA_CARRIER: ebitda,
        MD38_CARRIER: md38,
        EQUITY_CARRIER: equity,
        EV_CARRIER: ev,
    }
    missing = {
        key: _reason(carriers[_CARRIER_OF[key]]) for key, value in inputs.items() if value is None
    }
    if net_income is None and annualized:
        # audit round #11 (Part 2): a trailing year built as x4 was not formed -- the prior terms are missing (H2)
        missing[VALUE_INPUTS_NET_INCOME] = "prior_year_missing"
    if missing:
        inputs[VALUE_INPUTS_MISSING] = missing
    flags = {
        str(k): v for k, v in ev_inputs.items() if isinstance(k, str) and k.startswith("flag_")
    }
    if flags:
        inputs[VALUE_INPUTS_FLAGS] = flags
    if net_income is None:
        # audit round #11 (Part 2): the row's own value is the trailing-year net income -- its reason is that input's
        inputs["provenance"] = {"empty_reason": missing[VALUE_INPUTS_NET_INCOME]}
    return MetricResult(NAME, net_income, "usd", inputs)


def write(conn: Database, filing_id: int, computed: list[tuple[str, MetricResult]]) -> int:
    """Compute and store the rule input for one filing (any form). Idempotent."""
    agent_db.record_metrics(conn, filing_id, [(GROUP, build(computed))])
    return 1
