"""Ring-1 deterministic data-quality gates (``DQ_*``, T-065).

Seven threshold checks over a filing's *stored* ``fundamental_metrics``, zero LLM cost. A hit
is recorded in ``data_quality_issue`` (one row per gated metric) and does one or both of:

- **quarantine** the metric: consumers (``cycle``) read it as NULL, because the stored value
  is implausible enough to be a data error rather than a business fact;
- **HARD**: ``cycle`` raises a ``DATA_QUALITY`` veto on the asset. A SOFT row that is not
  quarantined is only a review item.

The gates are a permanent backstop, not a fix: F1/F2/F4 (``docs/model_fixes.md``) corrected
the known causes, and these catch whatever unknown cause produces the same symptoms next.
Thresholds are ``PLAN.md`` Work item 7's calibrated table; the rationale and the verification
against production data are in ``docs/model_fixes.md``'s T-065 entry.

| Rule               | Trigger                                            | Severity  | Quarantines |
|--------------------|----------------------------------------------------|-----------|-------------|
| ``DQ_FCF_YIELD``   | ``|free_cash_flow_yield| > 0.50``                   | HARD      | that yield  |
| ``DQ_MARGIN``      | ``|net_margin| > 5``                               | HARD      | net margin  |
| ``DQ_MARGIN_REVIEW``| ``|net_margin|`` in ``(1, 5]``                     | SOFT      | nothing     |
| ``DQ_OCF_MARGIN``  | ``|operating_cash_flow_margin| > 3``               | HARD      | OCF margin  |
| ``DQ_MCAP_SCALE``  | ``market_cap / total_assets`` outside ``[0.001, 100]`` | HARD  | market cap and every valuation metric built on it |
| ``DQ_NEG_EQUITY``  | ``equity <= 0``                                    | HARD if also ``net_debt_to_ebitda > 5.0``, ``ebitda <= 0`` with positive net debt, or ``interest_coverage < 1.5``, else SOFT | D/E and ROE |
| ``DQ_REVENUE_POS`` | ``revenue <= 0`` or missing, with net income present | HARD    | the revenue-denominated ratios |

The gates read the stored rows (value + ``inputs_json``) of one metrics engine version at a
time, so every ``data_quality_issue`` row names the version it judged (T-090): a consumer that
reads ``metrics-v3`` is never quarantined by a verdict on ``metrics-v2``. Recording is
append-only under :data:`GATE_VERSION`; re-gating the same stored metrics is a no-op.

**Accounting identities (T-150).** A second family of gates reads the filing's *stored facts*
(``financial_facts``) -- one filing, its own period, no dimensions, in the filed sign -- and
checks the checklist's identity rules (``docs/checklist_sec/checklist_v1.3.md``, Layer CON) with
DQ-01's tolerance and L-10's rounding term:

| Rule      | Identity                                                      | Sev.  |
|-----------|---------------------------------------------------------------|-------|
| ``CON_01``| ``Assets = LiabilitiesAndStockholdersEquity``                 | DQ-01 |
| ``CON_02``| ``Assets = AssetsCurrent + AssetsNoncurrent``                 | WARN  |
| ``CON_03``| ``Liabilities = LiabilitiesCurrent + LiabilitiesNoncurrent``  | WARN  |
| ``CON_04``| equity incl. NCI ``= StockholdersEquity + MinorityInterest``  | DQ-01 |
| ``CON_06``| a complete contiguous quarter set ``=`` its aggregate column, inside one filing | DQ-01 |
| ``CON_07``| change in cash ``=`` operating``+``investing``+``financing``  | WARN  |
| ``CON_08``| ``NetCashProvidedByUsedInOperatingActivities`` ``=`` continuing ``+`` discontinued | DQ-01 |
| ``CON_19``| ``ProfitLoss = NetIncomeLoss + NCI income``                   | DQ-01 |

**DQ-01.** ``diff = |left - right|``; within ``rounding`` it passes; above ``rounding`` and at
most ``threshold = max(rounding, 0.5% x |base|)`` it WARNs; above ``threshold`` it is material
and the rule's metric inputs are blocked (quarantined). ``base`` is total assets (balance sheet),
net income (income statement) or the period's change in cash (cash flow); when the base is not
filed the level is decided on ``rounding`` alone and a not-pass is a WARN, never a BLOCK
(``base_missing``). **L-10.** ``decimals`` is not captured, so ``rounding = 0.5 x unit x
n_terms``: ``n_terms`` the filed values entering the identity, ``unit`` the largest of USD 1M and
USD 1k that divides every term exactly, else USD 1. ``CON_06`` is the existing
:func:`record_ttm_crosscheck` (``DQ_TTM_CROSSCHECK``): it compares one filing's year-to-date
identity with four quarters taken from *earlier* filings (not DQC_0084's within-one-instance
check), takes DQ-01's level, and writes only a SOFT review row -- never a quarantine.

A hit writes one SOFT row under the pseudo-group ``identity`` (``metric_name`` = the rule, like
the ``ttm`` group), so no consumer reads it as a metric; a BLOCK also writes the affected
metrics of the rule (``metrics affected`` in the checklist) as SOFT, quarantined rows, so
``cycle`` reads them as NULL. The affected metrics use their real ``metric_group``. There is no
HARD and no asset veto (DQ-01 blocks the inputs, it does not veto the company).
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from portfolio_common.db import Database

from fundamental_agent import fiscal
from fundamental_agent.metrics.base import TTMFlow
from kg_schema import queries
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, METRIC_GROUPS, MetricVersions

GATE_VERSION: Final[str] = DATA_QUALITY_GATE_VERSION

FCF_YIELD_MAX: Final[float] = 0.50
NET_MARGIN_HARD: Final[float] = 5.0
NET_MARGIN_REVIEW: Final[float] = 1.0
OCF_MARGIN_MAX: Final[float] = 3.0
MCAP_TO_ASSETS_MIN: Final[float] = 0.001
MCAP_TO_ASSETS_MAX: Final[float] = 100.0
NEG_EQUITY_NET_DEBT_TO_EBITDA: Final[float] = 5.0
NEG_EQUITY_INTEREST_COVERAGE: Final[float] = 1.5

MetricKey = tuple[str, str]  # (metric_group, metric_name)

# Everything valuation derives from the market capitalisation (valuation.py).
_MCAP_DERIVED: Final[tuple[MetricKey, ...]] = (
    ("valuation", "market_capitalization"),
    ("valuation", "enterprise_value"),
    ("valuation", "free_cash_flow_yield"),
    ("valuation", "enterprise_fcf_yield"),
    ("valuation", "sbc_adjusted_fcf_yield"),
)
# Ratios with book equity in the denominator: meaningless when equity <= 0.
_EQUITY_DENOMINATED: Final[tuple[MetricKey, ...]] = (
    ("leverage", "debt_to_equity"),
    ("profitability", "return_on_equity"),
)
# Ratios with revenue in the denominator (or revenue as the growth base).
_REVENUE_DENOMINATED: Final[tuple[MetricKey, ...]] = (
    ("profitability", "gross_margin"),
    ("profitability", "operating_margin"),
    ("profitability", "net_margin"),
    ("cashflow", "operating_cash_flow_margin"),
    ("cashflow", "free_cash_flow_margin"),
    ("efficiency", "asset_turnover"),
    ("growth", "revenue_growth"),
)
# Where each raw statement input is read from, in order: the metric whose inputs carry it.
_INPUT_SOURCES: Final[dict[str, tuple[MetricKey, ...]]] = {
    "equity": (("leverage", "debt_to_equity"), ("profitability", "return_on_equity")),
    "total_assets": (("leverage", "debt_to_assets"), ("profitability", "return_on_assets")),
    "revenue": (("profitability", "net_margin"), ("cashflow", "operating_cash_flow_margin")),
    "net_income": (("profitability", "net_margin"), ("cashflow", "operating_cash_flow_margin")),
    # T-116 (PR #95 review): reconstructing EBITDA/net debt directly, rather than trusting the
    # sign of the already-divided net_debt_to_ebitda ratio -- see `_ebitda`/`_net_debt` below.
    "total_debt": (("leverage", "net_debt_to_ebitda"), ("leverage", "debt_to_equity")),
    "cash": (("leverage", "net_debt_to_ebitda"), ("leverage", "debt_to_equity")),
    "operating_income": (("leverage", "net_debt_to_ebitda"), ("leverage", "interest_coverage")),
    "depreciation_amortization": (("leverage", "net_debt_to_ebitda"),),
    "ebitda_ttm": (("leverage", "net_debt_to_ebitda"),),
}


@dataclass(frozen=True)
class StoredMetric:
    value: float | None
    inputs: Mapping[str, Any] = field(default_factory=dict)


FilingMetrics = Mapping[MetricKey, StoredMetric]


@dataclass(frozen=True)
class Issue:
    rule_id: str
    severity: str  # 'HARD' | 'SOFT'
    quarantined: bool
    metric: MetricKey
    value: float | None
    evidence: Mapping[str, Any]


def _value(fm: FilingMetrics, key: MetricKey) -> float | None:
    stored = fm.get(key)
    return None if stored is None else stored.value


def _input(fm: FilingMetrics, name: str) -> float | None:
    """A raw statement figure (``equity``, ``revenue``, ...) from the inputs audit blob of the
    first metric that recorded it. Absent and NULL are the same: not reported."""
    for key in _INPUT_SOURCES[name]:
        stored = fm.get(key)
        if stored is not None and stored.inputs.get(name) is not None:
            return float(stored.inputs[name])
    return None


def _hits(  # noqa: PLR0913 - one issue per stored target metric
    fm: FilingMetrics,
    targets: Iterable[MetricKey],
    rule_id: str,
    severity: str,
    *,
    quarantined: bool,
    evidence: Mapping[str, Any],
) -> list[Issue]:
    """One issue per *target* metric the filing actually stored (value NULL or not)."""
    return [
        Issue(rule_id, severity, quarantined, key, fm[key].value, evidence)
        for key in targets
        if key in fm
    ]


def _abs_above(
    rule_id: str, key: MetricKey, limit: float
) -> Callable[[FilingMetrics], list[Issue]]:
    def gate(fm: FilingMetrics) -> list[Issue]:
        v = _value(fm, key)
        if v is None or abs(v) <= limit:
            return []
        return _hits(fm, [key], rule_id, "HARD", quarantined=True, evidence={"limit": limit})

    return gate


def _margin_review(fm: FilingMetrics) -> list[Issue]:
    key = ("profitability", "net_margin")
    v = _value(fm, key)
    if v is None or not NET_MARGIN_REVIEW < abs(v) <= NET_MARGIN_HARD:
        return []
    evidence = {"range": [NET_MARGIN_REVIEW, NET_MARGIN_HARD]}
    return _hits(fm, [key], "DQ_MARGIN_REVIEW", "SOFT", quarantined=False, evidence=evidence)


def _mcap_scale(fm: FilingMetrics) -> list[Issue]:
    mcap = _value(fm, ("valuation", "market_capitalization"))
    assets = _input(fm, "total_assets")
    if mcap is None or assets is None or assets <= 0:
        return []
    ratio = mcap / assets
    if MCAP_TO_ASSETS_MIN <= ratio <= MCAP_TO_ASSETS_MAX:
        return []
    evidence = {
        "market_capitalization": mcap,
        "total_assets": assets,
        "ratio": ratio,
        "range": [MCAP_TO_ASSETS_MIN, MCAP_TO_ASSETS_MAX],
    }
    return _hits(fm, _MCAP_DERIVED, "DQ_MCAP_SCALE", "HARD", quarantined=True, evidence=evidence)


def _ebitda(fm: FilingMetrics) -> float | None:
    """TTM EBITDA on a 10-Q (already annualized, T-105); else the raw quarter/annual figure."""
    ttm = _input(fm, "ebitda_ttm")
    if ttm is not None:
        return ttm
    operating = _input(fm, "operating_income")
    dep_amort = _input(fm, "depreciation_amortization")
    return operating + dep_amort if operating is not None and dep_amort is not None else None


def _net_debt(fm: FilingMetrics) -> float | None:
    debt = _input(fm, "total_debt")
    cash = _input(fm, "cash")
    return debt - cash if debt is not None and cash is not None else None


def _neg_equity(fm: FilingMetrics) -> list[Issue]:
    """T-116 (PR #95 review): ``net_debt_to_ebitda = safe_div(net_debt, ebitda)`` goes
    *negative* when EBITDA itself is negative, even with substantial positive net debt --
    exactly the most distressed profile, and a plain ``> NEG_EQUITY_NET_DEBT_TO_EBITDA`` check
    reads that negative ratio as comfortably healthy (real data: WAT 10-Q 2026-04-04,
    EBITDA -$11M, net debt $4.4B, ratio -399). Distinguished from a genuine net-cash position
    (HUM 10-Q 2023-03-31, ratio -1.1, healthy) by reconstructing EBITDA and net debt from the
    same stored inputs the ratio itself was divided from, rather than trusting the ratio's sign
    alone."""
    equity = _input(fm, "equity")
    if equity is None or equity > 0:
        return []
    ndte = _value(fm, ("leverage", "net_debt_to_ebitda"))
    cov = _value(fm, ("leverage", "interest_coverage"))
    ebitda = _ebitda(fm)
    net_debt = _net_debt(fm)
    burning_cash_with_debt = (
        ebitda is not None and ebitda <= 0 and net_debt is not None and net_debt > 0
    )
    distressed = (
        burning_cash_with_debt
        or (ndte is not None and ndte > NEG_EQUITY_NET_DEBT_TO_EBITDA)
        or (cov is not None and cov < NEG_EQUITY_INTEREST_COVERAGE)
    )
    evidence = {
        "equity": equity,
        "net_debt_to_ebitda": ndte,
        "interest_coverage": cov,
        "ebitda": ebitda,
        "net_debt": net_debt,
    }
    return _hits(
        fm,
        _EQUITY_DENOMINATED,
        "DQ_NEG_EQUITY",
        "HARD" if distressed else "SOFT",
        quarantined=True,
        evidence=evidence,
    )


def _revenue_pos(fm: FilingMetrics) -> list[Issue]:
    net_income = _input(fm, "net_income")
    revenue = _input(fm, "revenue")
    if net_income is None or (revenue is not None and revenue > 0):
        return []
    evidence = {"revenue": revenue, "net_income": net_income}
    return _hits(
        fm, _REVENUE_DENOMINATED, "DQ_REVENUE_POS", "HARD", quarantined=True, evidence=evidence
    )


# (rule_id, severity as catalogued, description, gate) -- in the order they are evaluated.
GATES: Final[tuple[tuple[str, str, str, Callable[[FilingMetrics], list[Issue]]], ...]] = (
    (
        "DQ_FCF_YIELD",
        "HARD",
        f"|free-cash-flow yield| > {FCF_YIELD_MAX}",
        _abs_above("DQ_FCF_YIELD", ("valuation", "free_cash_flow_yield"), FCF_YIELD_MAX),
    ),
    (
        "DQ_MARGIN",
        "HARD",
        f"|net margin| > {NET_MARGIN_HARD}",
        _abs_above("DQ_MARGIN", ("profitability", "net_margin"), NET_MARGIN_HARD),
    ),
    (
        "DQ_MARGIN_REVIEW",
        "SOFT",
        f"|net margin| in ({NET_MARGIN_REVIEW}, {NET_MARGIN_HARD}]",
        _margin_review,
    ),
    (
        "DQ_OCF_MARGIN",
        "HARD",
        f"|operating-cash-flow margin| > {OCF_MARGIN_MAX}",
        _abs_above("DQ_OCF_MARGIN", ("cashflow", "operating_cash_flow_margin"), OCF_MARGIN_MAX),
    ),
    (
        "DQ_MCAP_SCALE",
        "HARD",
        f"market cap / total assets outside [{MCAP_TO_ASSETS_MIN}, {MCAP_TO_ASSETS_MAX}]",
        _mcap_scale,
    ),
    (
        "DQ_NEG_EQUITY",
        "HARD/SOFT",
        "book equity <= 0: D/E and ROE quarantined; HARD only if net debt/EBITDA > "
        f"{NEG_EQUITY_NET_DEBT_TO_EBITDA} or interest coverage < {NEG_EQUITY_INTEREST_COVERAGE}",
        _neg_equity,
    ),
    (
        "DQ_REVENUE_POS",
        "HARD",
        "revenue <= 0 or missing while net income is reported",
        _revenue_pos,
    ),
)
# -- accounting identities (CON, DQ-01, L-10; T-150) -------------------------------------------
#
# A second gate family. Unlike the DQ_* gates above, it reads the filing's *stored facts*
# (`financial_facts`), not its metrics: one filing, its own period, no dimensions, in the filed
# sign (ID-11/ID-12, ID-17). Rules CON-01 to CON-04, CON-07, CON-19 (checklist Layer CON); CON-06
# is the existing `record_ttm_crosscheck` below, whose tolerance becomes DQ-01's.

G = "us-gaap_"
IDENTITY_GROUP: Final[str] = "identity"
MATERIALITY: Final[float] = 0.005  # DQ-01: 0.5% of the base
CON_RULE_IDS: Final[tuple[str, ...]] = (
    "CON_01",
    "CON_02",
    "CON_03",
    "CON_04",
    "CON_06",
    "CON_07",
    "CON_08",
    "CON_19",
)

# The stored concepts the identities read (C7's default: a balance-sheet concept is read from the
# balance sheet, an income concept from the income statement, a cash-flow concept from the
# cash-flow statement -- never a concept that merely appears on another stored statement).
_ASSETS: Final[str] = G + "Assets"
_ASSETS_CURRENT: Final[str] = G + "AssetsCurrent"
_ASSETS_NONCURRENT: Final[str] = G + "AssetsNoncurrent"
_LIABILITIES: Final[str] = G + "Liabilities"
_LIABILITIES_CURRENT: Final[str] = G + "LiabilitiesCurrent"
_LIABILITIES_NONCURRENT: Final[str] = G + "LiabilitiesNoncurrent"
_LIAB_EQ: Final[str] = G + "LiabilitiesAndStockholdersEquity"
_STOCKHOLDERS_EQUITY: Final[str] = G + "StockholdersEquity"
_EQUITY_INCL_NCI: Final[str] = (
    G + "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
)
_MINORITY_INTEREST: Final[str] = G + "MinorityInterest"
_PROFIT_LOSS: Final[str] = G + "ProfitLoss"
_NET_INCOME: Final[str] = G + "NetIncomeLoss"
_NCI_INCOME: Final[str] = G + "NetIncomeLossAttributableToNoncontrollingInterest"
_OCF: Final[str] = G + "NetCashProvidedByUsedInOperatingActivities"
_OCF_CONT: Final[str] = G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"
_OCF_DISC: Final[str] = G + "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"
_ICF: Final[str] = G + "NetCashProvidedByUsedInInvestingActivities"
_FCF: Final[str] = G + "NetCashProvidedByUsedInFinancingActivities"
# DQ-01's "period's change in cash" (C3): the first filed of these three. CON-07's own identity
# lists only the first two (`...ExcludingExchangeRateEffect`); the third is a base fallback.
_CHANGE_IN_CASH: Final[tuple[str, ...]] = (
    G + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecrease"
    "ExcludingExchangeRateEffect",
    G + "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
    G + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecrease"
    "IncludingExchangeRateEffect",
)
# concept -> the statement it is filed on (C7).
_CONCEPT_STATEMENT: Final[dict[str, str]] = {
    _ASSETS: "balance_sheet",
    _ASSETS_CURRENT: "balance_sheet",
    _ASSETS_NONCURRENT: "balance_sheet",
    _LIABILITIES: "balance_sheet",
    _LIABILITIES_CURRENT: "balance_sheet",
    _LIABILITIES_NONCURRENT: "balance_sheet",
    _LIAB_EQ: "balance_sheet",
    _STOCKHOLDERS_EQUITY: "balance_sheet",
    _EQUITY_INCL_NCI: "balance_sheet",
    _MINORITY_INTEREST: "balance_sheet",
    _PROFIT_LOSS: "income_statement",
    _NET_INCOME: "income_statement",
    _NCI_INCOME: "income_statement",
    _OCF: "cash_flow",
    _OCF_CONT: "cash_flow",
    _OCF_DISC: "cash_flow",
    _ICF: "cash_flow",
    _FCF: "cash_flow",
}
_CONCEPT_STATEMENT.update(dict.fromkeys(_CHANGE_IN_CASH, "cash_flow"))
IDENTITY_CONCEPTS: Final[tuple[str, ...]] = tuple(_CONCEPT_STATEMENT)
_BALANCE_CONCEPTS: Final[tuple[str, ...]] = tuple(
    c for c, s in _CONCEPT_STATEMENT.items() if s == "balance_sheet"
)
_FLOW_CONCEPTS: Final[tuple[str, ...]] = tuple(
    c for c, s in _CONCEPT_STATEMENT.items() if s != "balance_sheet"
)

# (rule, lhs, rhs, base concept, WARN-only per the checklist's "Sev." column). Read from the
# filing's instant column (CON-01..04) or its income-flow column (CON-19).
_BALANCE_IDENTITIES: Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...], str, bool], ...]] = (
    ("CON_01", (_ASSETS,), (_LIAB_EQ,), _ASSETS, False),
    ("CON_02", (_ASSETS,), (_ASSETS_CURRENT, _ASSETS_NONCURRENT), _ASSETS, True),
    ("CON_03", (_LIABILITIES,), (_LIABILITIES_CURRENT, _LIABILITIES_NONCURRENT), _ASSETS, True),
    ("CON_04", (_EQUITY_INCL_NCI,), (_STOCKHOLDERS_EQUITY, _MINORITY_INTEREST), _ASSETS, False),
)
_INCOME_IDENTITIES: Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...], str, bool], ...]] = (
    ("CON_19", (_PROFIT_LOSS,), (_NET_INCOME, _NCI_INCOME), _NET_INCOME, False),
)

# C6: the stored metrics quarantined when a rule reaches the material level ("Metrics affected" in
# the checklist's CON table). Each metric keeps its real ``metric_group`` so ``cycle`` picks the
# quarantine up unchanged.
_CON01_METRICS: Final[tuple[MetricKey, ...]] = (  # "asset- and equity-based ratios"
    ("profitability", "return_on_assets"),
    ("profitability", "return_on_equity"),
    ("leverage", "debt_to_assets"),
    ("leverage", "debt_to_equity"),
    ("efficiency", "asset_turnover"),
    ("roic", "return_on_invested_capital"),
)
_CON04_METRICS: Final[tuple[MetricKey, ...]] = (  # equity, per the checklist's CON-04 row
    ("profitability", "return_on_equity"),
    ("leverage", "debt_to_equity"),
    ("roic", "return_on_invested_capital"),
)
_CON08_METRICS: Final[tuple[MetricKey, ...]] = (  # cash-flow ratios
    ("cashflow", "operating_cash_flow_margin"),
    ("cashflow", "free_cash_flow_margin"),
    ("cashflow", "free_cash_flow_conversion"),
)
# CON-19 ("as ID-03"): every stored metric whose formula reads ``net_income`` (C6), with the
# source in ``src/fundamental_agent/metrics/``:
# profitability.py:51 net_margin, :52 return_on_assets, :53 return_on_equity;
# growth.py:30/:48 net_income_growth; cagr.py:17 net_income_cagr;
# cashflow.py:47/:78 free_cash_flow_conversion.
_CON19_METRICS: Final[tuple[MetricKey, ...]] = (
    ("profitability", "net_margin"),
    ("profitability", "return_on_assets"),
    ("profitability", "return_on_equity"),
    ("growth", "net_income_growth"),
    ("cagr", "net_income_cagr"),
    ("cashflow", "free_cash_flow_conversion"),
)
_AFFECTED_METRICS: Final[dict[str, tuple[MetricKey, ...]]] = {
    "CON_01": _CON01_METRICS,
    "CON_04": _CON04_METRICS,
    "CON_08": _CON08_METRICS,
    "CON_19": _CON19_METRICS,
}

# The TTM flow items filed on the income statement (C3: CON-06's base is then the TTM net income).
_INCOME_FLOW_ITEMS: Final[frozenset[str]] = frozenset(
    {"net_income", "revenue", "cogs", "operating_income"}
)
# CON_06 proper (F1b): the stored TTM-based metrics the checklist's CON-06 row marks ("every ratio
# that uses TTM flows") -- the union of the per-item map removed in F1. The source is each metric's
# formula: profitability.py:52 return_on_assets, :53 return_on_equity; efficiency.py:38
# asset_turnover, :39 inventory_turnover, :40 receivables_turnover; leverage.py:72
# net_debt_to_ebitda; roic.py:71 return_on_invested_capital; valuation.py:160 free_cash_flow_yield,
# :163 sbc_adjusted_fcf_yield, :166 enterprise_fcf_yield.
_CON06_TTM_METRICS: Final[tuple[MetricKey, ...]] = (
    ("profitability", "return_on_assets"),
    ("profitability", "return_on_equity"),
    ("efficiency", "asset_turnover"),
    ("efficiency", "inventory_turnover"),
    ("efficiency", "receivables_turnover"),
    ("leverage", "net_debt_to_ebitda"),
    ("roic", "return_on_invested_capital"),
    ("valuation", "free_cash_flow_yield"),
    ("valuation", "sbc_adjusted_fcf_yield"),
    ("valuation", "enterprise_fcf_yield"),
)
# CON_06 proper's constants: a quarter column chains into an aggregate by PER-08's quarter range;
# a filed quarter more than a year before an aggregate belongs to another year.
_SUB_QUARTER_GAP: Final[tuple[int, int]] = (89, 98)
_SUB_YEAR_WINDOW: Final[int] = 372
_SUB_MIN_QUARTERS: Final[int] = 2  # a "set" needs at least two quarters

# The FASB 2026 US-GAAP schema's element ids and additive-duration concepts
# (``type='xbrli:monetaryItemType'`` + ``xbrli:periodType='duration'``), generated from
# ``elts/us-gaap-2026.xsd`` in the pinned zip by ``scripts/build_us_gaap_types.py`` (the schema,
# a loaded B6 source, is not the filing's calculation linkbase -- L-07 is untouched). CON-06 tests
# only the additive-duration concepts; shares / per-share / other non-monetary concepts and every
# concept the 2026 schema does not declare are not tested, and counted separately.
_DATA_DIR: Final[Path] = Path(__file__).resolve().parent / "data"
_SCHEMA_ELEMENTS_FILE: Final[Path] = _DATA_DIR / "us_gaap_2026_elements.txt"
_ADDITIVE_FILE: Final[Path] = _DATA_DIR / "us_gaap_2026_duration_monetary.txt"


@lru_cache(maxsize=2)
def _concept_set(path: str) -> frozenset[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))


def schema_concepts() -> frozenset[str]:
    """Every element id the FASB 2026 US-GAAP schema declares."""
    return _concept_set(str(_SCHEMA_ELEMENTS_FILE))


def additive_duration_concepts() -> frozenset[str]:
    """The schema's additive duration amounts (monetary + duration), the only concepts CON-06
    tests."""
    return _concept_set(str(_ADDITIVE_FILE))


# The number of filed values entering the cross-check's two constructions (F1): the year-to-date
# identity has 3 (current YTD, prior fiscal year, prior-year YTD), the four recorded quarters have
# 4. Only the two constructed values are available here, so the unit is inferred from those two
# (declared) and `n_terms` is this constant.
_TTM_CROSSCHECK_TERMS: Final[int] = 7


_EXACT_EPSILON: Final[float] = 1e-9


def l10_unit(terms: Sequence[float]) -> float:
    """L-10's inferred unit: the largest of USD 1,000,000 and USD 1,000 that divides every term
    exactly, else USD 1. A term is divided exactly when its quotient to the unit is a whole
    number (the stored facts are the filers' own rounded figures, so exact integer multiples)."""
    if not terms:
        return 1.0
    for unit in (1_000_000.0, 1_000.0):
        if all(
            value == 0.0 or abs(value / unit - round(value / unit)) < _EXACT_EPSILON
            for value in terms
        ):
            return unit
    return 1.0


def l10_rounding(terms: Sequence[float]) -> float:
    """L-10: ``decimals`` is not captured, so DQ-01's rounding term is
    ``0.5 x unit x n_terms`` with the unit inferred by :func:`l10_unit`. CON-07, four terms on a
    statement in millions, gets USD 2,000,000 (review 3.3)."""
    return 0.5 * l10_unit(terms) * len(terms)


def dq01_level(diff: float, rounding: float, base: float | None) -> str:
    """DQ-01: ``pass`` within ``rounding``, ``warn`` above it and at most
    ``threshold = max(rounding, 0.5% x |base|)``, ``block`` above it. A missing base decides on
    the rounding term alone and never blocks (``base_missing``)."""
    if diff <= rounding:
        return "pass"
    if base is None:
        return "warn"
    return "warn" if diff <= max(rounding, MATERIALITY * abs(base)) else "block"


@dataclass(frozen=True)
class IdentityResult:
    """One identity's outcome: the two sides, DQ-01's terms, and the level reached."""

    rule_id: str
    level: str  # 'pass' | 'warn' | 'block'
    left: float
    right: float
    diff: float
    rounding: float
    threshold: float
    base: float | None
    n_terms: int
    unit: float
    warn_only: bool
    column: str | None
    ytd: bool
    concepts: tuple[str, ...]

    @property
    def base_missing(self) -> bool:
        return self.base is None


def identity_result(  # noqa: PLR0913, PLR0917 - one identity's operands and its DQ-01 parameters
    rule_id: str,
    lhs: tuple[str, ...],
    rhs: tuple[str, ...],
    base_concept: str | None,
    values: Mapping[str, float | None],
    column: str | None,
    *,
    rounding: float | None = None,
    warn_only: bool = False,
    ytd: bool = False,
) -> IdentityResult | None:
    """One identity over *values* (``{concept: filed value}``), or ``None`` when any operand is
    not filed (C8: "when ... filed"). *rounding* overrides L-10 (only T-147's reproduction passes
    a fixed USD 1M); *base_concept* is looked up in *values* (``None`` there = base missing)."""
    operands = (*lhs, *rhs)
    resolved: dict[str, float] = {}
    for concept in operands:
        value = values.get(concept)
        if value is None:
            return None
        resolved[concept] = value
    terms = [resolved[c] for c in operands]
    left = sum(resolved[c] for c in lhs)
    right = sum(resolved[c] for c in rhs)
    diff = abs(left - right)
    base = None if base_concept is None else values.get(base_concept)
    terms_rounding = l10_rounding(terms)
    used_rounding = terms_rounding if rounding is None else rounding
    threshold = used_rounding if base is None else max(used_rounding, MATERIALITY * abs(base))
    return IdentityResult(
        rule_id=rule_id,
        level=dq01_level(diff, used_rounding, base),
        left=left,
        right=right,
        diff=diff,
        rounding=used_rounding,
        threshold=threshold,
        base=base,
        n_terms=len(terms),
        unit=l10_unit(terms),
        warn_only=warn_only,
        column=column,
        ytd=ytd,
        concepts=operands,
    )


def _first_filed(values: Mapping[str, float | None], concepts: Sequence[str]) -> str | None:
    return next((c for c in concepts if values.get(c) is not None), None)


def con07_result(
    values: Mapping[str, float | None],
    column: str | None,
    *,
    base_concept: str | None = None,
    rounding: float | None = None,
    ytd: bool = False,
) -> IdentityResult | None:
    """CON-07: change in cash (excluding the exchange-rate effect) ``=`` operating ``+``
    investing ``+`` financing. *base_concept* defaults to the first filed change-in-cash concept
    (C3); CON-19's T-147 reproduction passes no override here either."""
    change = _first_filed(values, _CHANGE_IN_CASH[:2])
    if change is None:
        return None
    base = base_concept or _first_filed(values, _CHANGE_IN_CASH)
    return identity_result(
        "CON_07",
        (change,),
        (_OCF, _ICF, _FCF),
        base,
        values,
        column,
        rounding=rounding,
        warn_only=True,
        ytd=ytd,
    )


def con08_result(
    values: Mapping[str, float | None],
    column: str | None,
    *,
    base_concept: str | None = None,
    rounding: float | None = None,
    ytd: bool = False,
) -> IdentityResult | None:
    """CON-08: operating cash flow ``=`` continuing ``+`` discontinued. With no discontinued line
    filed the identity reads total ``=`` continuing (C8). *base_concept* defaults to the first
    filed change-in-cash concept (C3)."""
    if values.get(_OCF) is None or values.get(_OCF_CONT) is None:
        return None
    rhs = (_OCF_CONT, _OCF_DISC) if values.get(_OCF_DISC) is not None else (_OCF_CONT,)
    base = base_concept if base_concept is not None else _first_filed(values, _CHANGE_IN_CASH)
    return identity_result("CON_08", (_OCF,), rhs, base, values, column, rounding=rounding, ytd=ytd)


def evaluate_identities(  # noqa: PLR0913 - the filing's balance/flow/ytd columns
    balance: Mapping[str, float | None],
    flow: Mapping[str, float | None],
    *,
    instant_column: str | None = None,
    flow_column: str | None = None,
    ytd: Mapping[str, float | None] | None = None,
    ytd_column: str | None = None,
    skip_con19: bool = False,
) -> list[IdentityResult]:
    """Every CON identity over the filing's balance (instant) and flow columns. CON-07/CON-08 on
    a 10-Q are retried on the year-to-date column (C7) when the filing's own quarter column does
    not hold their operands. *skip_con19* drops CON-19 (F2: its NCI operand has no filed sign)."""
    out: list[IdentityResult] = []
    for rule, lhs, rhs, base, warn in _BALANCE_IDENTITIES:
        result = identity_result(rule, lhs, rhs, base, balance, instant_column, warn_only=warn)
        if result is not None:
            out.append(result)
    if not skip_con19:
        for rule, lhs, rhs, base, warn in _INCOME_IDENTITIES:
            result = identity_result(rule, lhs, rhs, base, flow, flow_column, warn_only=warn)
            if result is not None:
                out.append(result)
    for spec in (con07_result, con08_result):
        result = spec(flow, flow_column)
        if result is None and ytd_column is not None and ytd:
            result = spec(ytd, ytd_column, ytd=True)
        if result is not None:
            out.append(result)
    return out


# -- the stored facts of one filing (C7) --------------------------------------------------------


@dataclass(frozen=True)
class FilingFacts:
    """The identity concepts of one filing, by ``(statement, concept) -> {column: filed value}``,
    in the filing's own period columns. ``instant_column`` / ``flow_column`` / ``ytd_column`` are
    the filing's balance-sheet, duration and year-to-date columns (C7)."""

    form: str
    period_end: str
    by_key: Mapping[tuple[str, str], Mapping[str, float]]
    instant_column: str | None
    flow_column: str | None
    ytd_column: str | None
    # (statement, concept, column) rows whose value carried a ``preferred_sign`` (T-148/m013). A
    # row stored without one (a database that predates m013, or a gateway row with no hint) has no
    # filed sign -- F2 treats that as missing data, not a value.
    signed: frozenset[tuple[str, str, str]] = frozenset()

    def value(
        self, concept: str, column: str | None, *, any_statement: bool = False
    ) -> float | None:
        """The concept's filed value in *column*: its own statement's row, or (T-147's
        reproduction) the first of ``income_statement`` / ``balance_sheet`` / ``cash_flow``."""
        if column is None:
            return None
        if any_statement:
            for statement in ("income_statement", "balance_sheet", "cash_flow"):
                value = self.by_key.get((statement, concept), {}).get(column)
                if value is not None:
                    return value
            return None
        return self.by_key.get((_CONCEPT_STATEMENT[concept], concept), {}).get(column)

    def has_sign(self, concept: str, column: str | None) -> bool:
        """Whether the concept's own-statement row in *column* carried a ``preferred_sign``."""
        if column is None:
            return False
        return (_CONCEPT_STATEMENT[concept], concept, column) in self.signed


_COLUMN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s*\(([A-Za-z0-9]+)\))?$")


def _column_parts(key: str) -> tuple[str, str] | None:
    """``(date, tag)`` of a raw column key (``"2025-06-30 (Q2)"`` -> ``("2025-06-30", "Q2")``);
    ``"INSTANT"`` for a bare date."""
    match = _COLUMN_RE.match(key.strip())
    if match is None:
        return None
    return match.group(1), match.group(2) or "INSTANT"


def _instant_column(keys: Iterable[str], period_end: str) -> str | None:
    """The balance-sheet column of the period *period_end* ends (ID-05, PER-08): the exact date,
    else the column whose date rounds to the same month end (a 52/53-week filer's balance sheet
    is dated days from the calendar month end), the closest one. Never an earlier period."""
    instants = [(p[0], k) for k in keys if (p := _column_parts(k)) and p[1] == "INSTANT"]
    if not instants or not period_end:
        return None
    exact = [k for d, k in instants if d == period_end]
    if exact:
        return exact[0]
    try:
        target = date.fromisoformat(period_end)
    except ValueError:
        return None
    near = [(d, k) for d, k in instants if fiscal.same_period_end(date.fromisoformat(d), target)]
    if not near:
        return None
    return min(near, key=lambda dk: (abs((date.fromisoformat(dk[0]) - target).days), dk[0]))[1]


def _flow_column(keys: Iterable[str], form: str, period_end: str) -> str | None:
    """The filing's own duration column (C7): ``<period_end> (FY)`` on a 10-K, the quarter column
    ending at ``period_end`` on a 10-Q (its own period, not a comparative)."""
    for key in keys:
        parts = _column_parts(key)
        if parts is None or parts[0] != period_end:
            continue
        if form.startswith("10-K") and parts[1] == "FY":
            return key
        if form.startswith("10-Q") and re.fullmatch(r"Q[1-4]", parts[1]):
            return key
    return None


def _ytd_column(keys: Iterable[str], period_end: str) -> str | None:
    return next(
        (k for k in keys if (p := _column_parts(k)) and p[0] == period_end and p[1] == "YTD"),
        None,
    )


_FACTS_SQL: Final[dict[bool, str]] = {
    # `preferred_sign` (T-148/m013) may be absent on a database that predates it: a fixed pair of
    # literal statements, never SQL built from a variable (as `sec_xcheck.common._FACTS_SQL`).
    True: (
        "SELECT filing_id, statement, concept, period_key, value, preferred_sign "
        "FROM financial_facts "
        "WHERE filing_id IN (SELECT value FROM json_each(?)) "
        "AND concept IN (SELECT value FROM json_each(?)) ORDER BY filing_id, id"
    ),
    False: (
        "SELECT filing_id, statement, concept, period_key, value, NULL AS preferred_sign "
        "FROM financial_facts "
        "WHERE filing_id IN (SELECT value FROM json_each(?)) "
        "AND concept IN (SELECT value FROM json_each(?)) ORDER BY filing_id, id"
    ),
}


def _has_preferred_sign(conn: Database) -> bool:
    return conn.relation_exists("financial_facts") and "preferred_sign" in conn.table_columns(
        "financial_facts"
    )


def load_facts(conn: Database, filing_ids: Iterable[int]) -> dict[int, FilingFacts]:
    """``filing_id -> FilingFacts`` for the identity concepts of each filing. Values are in the
    filed sign (displayed x ``preferred_sign``; as displayed when the column or the value is
    absent -- a database that predates m013, or a row the gateway sent without a hint)."""
    ids = sorted(set(int(f) for f in filing_ids))
    if not ids:
        return {}
    ids_json = json.dumps(ids)
    concepts_json = json.dumps(list(IDENTITY_CONCEPTS))
    meta = {
        int(r[0]): (str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT id, form, period_end FROM sec_filings WHERE id IN (SELECT value FROM json_each(?))",
            (ids_json,),
        )
    }
    by_filing: dict[int, dict[tuple[str, str], dict[str, float]]] = {}
    signed_filing: dict[int, set[tuple[str, str, str]]] = {}
    for row in conn.execute(_FACTS_SQL[_has_preferred_sign(conn)], (ids_json, concepts_json)):
        displayed = row["value"]
        if displayed is None:
            continue
        sign = row["preferred_sign"]
        value = float(displayed) * float(sign) if sign in (1, -1) else float(displayed)
        fid = int(row["filing_id"])
        statement, concept, column = (
            str(row["statement"]),
            str(row["concept"]),
            str(row["period_key"]),
        )
        columns = by_filing.setdefault(fid, {}).setdefault((statement, concept), {})
        columns.setdefault(column, value)
        if sign in (1, -1):
            signed_filing.setdefault(fid, set()).add((statement, concept, column))
    out: dict[int, FilingFacts] = {}
    for fid, by_key in by_filing.items():
        form, period_end = meta.get(fid, ("", ""))
        keys = {column for columns in by_key.values() for column in columns}
        out[fid] = FilingFacts(
            form=form,
            period_end=period_end,
            by_key=by_key,
            instant_column=_instant_column(keys, period_end),
            flow_column=_flow_column(keys, form, period_end),
            ytd_column=_ytd_column(keys, period_end),
            signed=frozenset(signed_filing.get(fid, ())),
        )
    return out


def con19_sign_missing(facts: FilingFacts) -> bool:
    """F2: whether CON-19's NCI operand is filed but carries **no filed sign** (a database without
    the ``preferred_sign`` column, or that row's ``preferred_sign`` is NULL). CON-19 is then not
    tested -- missing data, not a value -- and counted under its own label."""
    column = facts.flow_column
    if column is None or facts.value(_NCI_INCOME, column) is None:
        return False
    return not facts.has_sign(_NCI_INCOME, column)


def identity_issues(
    facts: FilingFacts,
    metrics: FilingMetrics,
) -> list[Issue]:
    """The identity gate's rows for one filing: a SOFT ``identity`` row per warn/block hit, plus
    the quarantined affected metrics of a DQ-01 rule at the material level (C5/C6)."""
    balance = {c: facts.value(c, facts.instant_column) for c in _BALANCE_CONCEPTS}
    flow = {c: facts.value(c, facts.flow_column) for c in _FLOW_CONCEPTS}
    ytd = (
        {c: facts.value(c, facts.ytd_column) for c in _FLOW_CONCEPTS}
        if facts.ytd_column is not None
        else None
    )
    return _identity_issues_from_results(
        evaluate_identities(
            balance,
            flow,
            instant_column=facts.instant_column,
            flow_column=facts.flow_column,
            ytd=ytd,
            ytd_column=facts.ytd_column,
            skip_con19=con19_sign_missing(facts),
        ),
        metrics,
    )


def _identity_issues_from_results(
    results: Iterable[IdentityResult], metrics: FilingMetrics
) -> list[Issue]:
    out: list[Issue] = []
    for result in results:
        if result.level == "pass":
            continue
        evidence = _identity_evidence(result)
        out.append(
            Issue(
                result.rule_id,
                "SOFT",
                False,
                (IDENTITY_GROUP, result.rule_id),
                result.diff,
                evidence,
            )
        )
        if result.level == "block" and not result.warn_only:
            for key in _AFFECTED_METRICS.get(result.rule_id, ()):
                stored = metrics.get(key)
                if stored is not None:
                    out.append(Issue(result.rule_id, "SOFT", True, key, stored.value, evidence))
    return out


def _identity_evidence(result: IdentityResult) -> dict[str, Any]:
    return {
        "rule": result.rule_id,
        "level": result.level,
        "left": result.left,
        "right": result.right,
        "diff": result.diff,
        "rounding": result.rounding,
        "unit": result.unit,
        "n_terms": result.n_terms,
        "base": result.base,
        "base_missing": result.base_missing,
        "threshold": result.threshold,
        "column": result.column,
        "ytd": result.ytd,
        "concepts": list(result.concepts),
    }


# -- CON_06 proper: contiguous sub-periods sum to the aggregate, inside one filing (F1b/F1c) ----
#
# DQC_0084 (B3): "values of a durational element for a series of periods of time do not aggregate
# to the value representing the aggregated period in the instance". A test needs, for one concept
# on one stored statement face, a **complete** contiguous set of quarter columns (Q1..Qk, PER-08
# gaps) inside the same filing that exactly spans one of its aggregate columns (YTD or FY).
#
# F1c-1: only amounts that add are tested -- the concepts the FASB 2026 US-GAAP schema
# (``elts/us-gaap-2026.xsd``) declares ``xbrli:monetaryItemType`` + ``xbrli:periodType='duration'``
# (:func:`additive_duration_concepts`). A share count (``WeightedAverageNumberOfDilutedShares
# Outstanding`` is ``sharesItemType``), a per-share ratio and every concept the schema does not
# declare are not tested and counted separately. The schema is a loaded B6 source, not the
# filing's calculation linkbase, so L-07 is untouched.
#
# F1c-2: an aggregate's expected quarter count comes from its own dates and the company's
# fiscal-year end (the 10-K period ends, :func:`_aggregate_expected`), never from the filing's
# ``fiscal_period`` label. A partial chain is not tested; counted separately as an incomplete set.


@dataclass(frozen=True)
class SubperiodFacts:
    """One filing's duration columns by ``(statement, concept) -> {column: filed value}`` and the
    company's fiscal-year end as ``(month, day)`` (from its 10-K period ends; ``None`` when the
    company has no stored 10-K), which gives each aggregate column's expected quarter count from
    the aggregate's own date."""

    fiscal_year_end_md: tuple[int, int] | None
    duration: Mapping[tuple[str, str], Mapping[str, float]]


@dataclass(frozen=True)
class SubperiodResult:
    """One complete contiguous sub-period set and its outcome. ``rules``/``column``/``ytd``/
    ``base_missing`` mirror :class:`IdentityResult` so a consumer can treat them alike."""

    statement: str
    concept: str
    agg_column: str
    quarter_columns: tuple[str, ...]
    values: tuple[float, ...]  # quarters (in ascending date) then the aggregate
    level: str
    diff: float
    rounding: float
    threshold: float
    base: float | None
    n_terms: int
    unit: float

    @property
    def rule_id(self) -> str:
        return "CON_06"

    @property
    def column(self) -> str:
        return self.agg_column

    @property
    def ytd(self) -> bool:
        return False

    @property
    def base_missing(self) -> bool:
        return self.base is None


def _next_fiscal_year_end(day: date, month: int, day_of_month: int) -> date:
    """The first date on/after *day* with the fiscal-year-end *(month, day_of_month)* (a month end
    stays a month end)."""
    for year in (day.year, day.year + 1):
        last = calendar.monthrange(year, month)[1]
        candidate = date(year, month, min(day_of_month, last))
        if candidate >= day:
            return candidate
    return date(day.year + 1, month, calendar.monthrange(day.year + 1, month)[1])


def _aggregate_expected(fye_md: tuple[int, int] | None, agg_date_iso: str) -> int:
    """The quarter count an aggregate column at *agg_date_iso* must span to be complete: the
    months from its fiscal-year start to the aggregate, over three (a 6-month YTD -> 2, 9-month
    -> 3, FY -> 4). The fiscal-year end comes from the company's 10-K period ends (*fye_md*),
    never from the filing's ``fiscal_period`` label (F1c-2)."""
    if fye_md is None:
        return 0
    agg = date.fromisoformat(agg_date_iso)
    fy_end = _next_fiscal_year_end(agg, fye_md[0], fye_md[1])
    start = fiscal.months_before(fy_end, 12)
    months = (agg.year - start.year) * 12 + (agg.month - start.month)
    return round(months / 3)


def _subperiod_chain(
    quarters: Mapping[str, str], agg_date_iso: str, expected: int
) -> tuple[list[str], int]:
    """The consecutive quarter columns chaining into an aggregate at *agg_date_iso* (closest
    first), at most *expected* of them (the aggregate's quarter length, so the chain never runs
    past the fiscal-year start), and the gap (days) of the first step. The last sub-period may end
    on the aggregate's own date (a 10-Q's Q3 and its 9-month YTD share the end date); every step
    but the first is one PER-08 quarter."""
    agg_date = date.fromisoformat(agg_date_iso)
    candidates = sorted(
        (
            (date.fromisoformat(d), k)
            for d, k in quarters.items()
            if 0 <= (agg_date - date.fromisoformat(d)).days <= _SUB_YEAR_WINDOW
        ),
        reverse=True,
    )
    accepted: list[str] = []
    previous = agg_date
    first_gap: int | None = None
    for d, key in candidates:
        gap = (previous - d).days
        valid = gap == 0 if first_gap is None else _SUB_QUARTER_GAP[0] <= gap <= _SUB_QUARTER_GAP[1]
        if not valid:
            break
        accepted.append(key)
        if first_gap is None:
            first_gap = gap
        previous = d
        if len(accepted) >= expected:
            break
    return accepted, (first_gap if first_gap is not None else -1)


def _split_duration_columns(
    columns: Mapping[str, float],
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """``({date: quarter key}, {date: (aggregate key, tag)})`` of one concept's columns."""
    quarters: dict[str, str] = {}
    aggregates: dict[str, tuple[str, str]] = {}
    for column in columns:
        parts = _column_parts(column)
        if parts is None or parts[1] == "INSTANT":
            continue
        if parts[1] in ("Q1", "Q2", "Q3", "Q4"):
            quarters.setdefault(parts[0], column)
        elif parts[1] in ("FY", "YTD"):
            aggregates.setdefault(parts[0], (column, parts[1]))
    return quarters, aggregates


def _subperiod_base(facts: SubperiodFacts, statement: str, agg_column: str) -> float | None:
    """DQ-01's base for a CON-06 set, from the aggregate column of the same statement (F1b):
    ``NetIncomeLoss`` on the income statement, the first filed change in cash on the cash flow."""
    if statement == "income_statement":
        return facts.duration.get((statement, _NET_INCOME), {}).get(agg_column)
    for change_concept in _CHANGE_IN_CASH:
        candidate = facts.duration.get((statement, change_concept), {}).get(agg_column)
        if candidate is not None:
            return candidate
    return None


def subperiod_results(
    facts: SubperiodFacts,
) -> tuple[list[SubperiodResult], list[dict[str, Any]], dict[str, int]]:
    """Every complete CON-06 set of one filing, the incomplete sets (a partial chain of >= 2
    quarters) and the two not-testable counters (``type_not_in_schema`` for a concept the 2026
    schema does not declare -- an extension or a pre-2026 element; ``not_additive`` for a declared
    concept that is not a monetary duration). One entry per (statement, concept, aggregate
    column)."""
    schema = schema_concepts()
    additive = additive_duration_concepts()
    results: list[SubperiodResult] = []
    incomplete: list[dict[str, Any]] = []
    not_tested = {"type_not_in_schema": 0, "not_additive": 0}
    for (statement, concept), columns in sorted(facts.duration.items()):
        quarters, aggregates = _split_duration_columns(columns)
        for agg_date_iso, (agg_column, _agg_tag) in sorted(aggregates.items()):
            expected = _aggregate_expected(facts.fiscal_year_end_md, agg_date_iso)
            if expected < _SUB_MIN_QUARTERS:
                continue
            chain, first_gap = _subperiod_chain(quarters, agg_date_iso, expected)
            if len(chain) < _SUB_MIN_QUARTERS:
                continue
            if concept not in schema:
                not_tested["type_not_in_schema"] += 1
                continue
            if concept not in additive:
                not_tested["not_additive"] += 1
                continue
            if len(chain) != expected or first_gap != 0:
                incomplete.append(
                    {
                        "statement": statement,
                        "concept": concept,
                        "agg_column": agg_column,
                        "quarter_columns": chain,
                        "expected": expected,
                    }
                )
                continue
            ordered = list(reversed(chain))  # ascending date order (closest-first -> oldest-first)
            quarter_values = [columns[k] for k in ordered]
            agg_value = columns[agg_column]
            terms = [*quarter_values, agg_value]
            diff = abs(agg_value - sum(quarter_values))
            base = _subperiod_base(facts, statement, agg_column)
            rounding = l10_rounding(terms)
            threshold = rounding if base is None else max(rounding, MATERIALITY * abs(base))
            results.append(
                SubperiodResult(
                    statement=statement,
                    concept=concept,
                    agg_column=agg_column,
                    quarter_columns=tuple(ordered),
                    values=tuple(terms),
                    level=dq01_level(diff, rounding, base),
                    diff=diff,
                    rounding=rounding,
                    threshold=threshold,
                    base=base,
                    n_terms=len(terms),
                    unit=l10_unit(terms),
                )
            )
    return results, incomplete, not_tested


def _subperiod_evidence(result: SubperiodResult) -> dict[str, Any]:
    return {
        "rule": "CON_06",
        "level": result.level,
        "statement": result.statement,
        "concept": result.concept,
        "agg_column": result.agg_column,
        "quarter_columns": list(result.quarter_columns),
        "values": list(result.values),
        "diff": result.diff,
        "rounding": result.rounding,
        "unit": result.unit,
        "n_terms": result.n_terms,
        "base": result.base,
        "base_missing": result.base_missing,
        "threshold": result.threshold,
    }


def con06_issues(results: Iterable[SubperiodResult], metrics: FilingMetrics) -> list[Issue]:
    """The CON_06 rows of one filing: one SOFT ``identity`` row per non-pass set (keyed by concept
    and aggregate column, since a filing can hold several), and the quarantined TTM-based metrics
    (:data:`_CON06_TTM_METRICS`) when the set is material."""
    out: list[Issue] = []
    for result in results:
        if result.level == "pass":
            continue
        evidence = _subperiod_evidence(result)
        metric = (IDENTITY_GROUP, f"CON_06 {result.concept} {result.agg_column}")
        out.append(Issue("CON_06", "SOFT", False, metric, result.diff, evidence))
        if result.level == "block":
            for key in _CON06_TTM_METRICS:
                stored = metrics.get(key)
                if stored is not None:
                    out.append(Issue("CON_06", "SOFT", True, key, stored.value, evidence))
    return out


def _fiscal_year_end_md(ends: Iterable[str]) -> tuple[int, int] | None:
    """The company's fiscal-year-end ``(month, day)``: the most common of its stored 10-K period
    ends (the latest such end breaks a tie). ``None`` when it has none (F1c-2)."""
    parsed = [date.fromisoformat(e) for e in ends if e]
    if not parsed:
        return None
    counts: dict[tuple[int, int], int] = {}
    latest: dict[tuple[int, int], date] = {}
    for end in parsed:
        md = (end.month, end.day)
        counts[md] = counts.get(md, 0) + 1
        latest[md] = max(latest.get(md, end), end)
    return max(counts, key=lambda md: (counts[md], latest[md]))


def load_subperiods(conn: Database, filing_ids: Iterable[int]) -> dict[int, SubperiodFacts]:
    """``filing_id -> SubperiodFacts``: every duration fact (income statement, cash flow) of each
    filing, in the filed sign, with the company's fiscal-year end (from its 10-K period ends)."""
    ids = sorted(set(int(f) for f in filing_ids))
    if not ids:
        return {}
    ids_json = json.dumps(ids)
    assets = {
        int(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT id, asset_id FROM sec_filings WHERE id IN (SELECT value FROM json_each(?))",
            (ids_json,),
        )
    }
    tenk: dict[int, list[str]] = {}
    if assets:
        for row in conn.execute(
            "SELECT asset_id, period_end FROM sec_filings WHERE form = '10-K' "
            "AND period_end IS NOT NULL AND asset_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(set(assets.values()))),),
        ):
            tenk.setdefault(int(row["asset_id"]), []).append(str(row["period_end"]))
    fye_by_asset = {asset: _fiscal_year_end_md(ends) for asset, ends in tenk.items()}
    sql = {
        True: (
            "SELECT filing_id, statement, concept, period_key, value, preferred_sign "
            "FROM financial_facts WHERE filing_id IN (SELECT value FROM json_each(?)) "
            "AND statement IN ('income_statement', 'cash_flow') ORDER BY filing_id, id"
        ),
        False: (
            "SELECT filing_id, statement, concept, period_key, value, NULL AS preferred_sign "
            "FROM financial_facts WHERE filing_id IN (SELECT value FROM json_each(?)) "
            "AND statement IN ('income_statement', 'cash_flow') ORDER BY filing_id, id"
        ),
    }
    by_filing: dict[int, dict[tuple[str, str], dict[str, float]]] = {}
    for row in conn.execute(sql[_has_preferred_sign(conn)], (ids_json,)):
        parts = _column_parts(str(row["period_key"]))
        displayed = row["value"]
        if parts is None or parts[1] == "INSTANT" or displayed is None:
            continue
        sign = row["preferred_sign"]
        value = float(displayed) * float(sign) if sign in (1, -1) else float(displayed)
        by_filing.setdefault(int(row["filing_id"]), {}).setdefault(
            (str(row["statement"]), str(row["concept"])), {}
        ).setdefault(str(row["period_key"]), value)
    return {
        fid: SubperiodFacts(
            fiscal_year_end_md=fye_by_asset.get(assets.get(fid, 0)),
            duration=duration,
        )
        for fid, duration in by_filing.items()
    }


# The DQ_* gates in evaluation order (the ``PLAN.md`` Work item 7 table). The identity rules
# (``CON_*``, T-150) are catalogued in :data:`CON_RULE_IDS` and both sets are in
# :data:`ALL_RULE_IDS`, which the CLI prints.
RULE_IDS: Final[tuple[str, ...]] = tuple(g[0] for g in GATES)
ALL_RULE_IDS: Final[tuple[str, ...]] = (*RULE_IDS, *CON_RULE_IDS)


def evaluate(fm: FilingMetrics) -> list[Issue]:
    """Every gate over one filing's stored metrics (one engine version)."""
    return [issue for _rule, _sev, _desc, gate in GATES for issue in gate(fm)]


# -- persistence ---------------------------------------------------------------------------


@dataclass
class GateReport:
    filings: int = 0
    inserted: int = 0
    # rule_id -> distinct filings hit / rule_id -> of which HARD
    filings_by_rule: dict[str, int] = field(default_factory=dict)
    hard_by_rule: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _load(
    conn: Database, versions: MetricVersions, filing_id: int | None
) -> dict[int, tuple[int, dict[MetricKey, StoredMetric]]]:
    """``filing_id -> (asset_id, metrics)`` for the resolved *versions* (every filing, or one)."""
    rows = conn.execute(
        """
        SELECT m.filing_id, f.asset_id, m.metric_group, m.metric_name, m.value, m.inputs_json
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        WHERE (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
          AND (? IS NULL OR m.filing_id = ?)
        ORDER BY m.filing_id
        """,
        (versions.json_param(), filing_id, filing_id),
    ).fetchall()
    out: dict[int, tuple[int, dict[MetricKey, StoredMetric]]] = {}
    for r in rows:
        try:
            inputs = json.loads(r["inputs_json"] or "{}")
        except (TypeError, ValueError):
            inputs = {}
        fid = int(r["filing_id"])
        _aid, metrics = out.setdefault(fid, (int(r["asset_id"]), {}))
        metrics[(str(r["metric_group"]), str(r["metric_name"]))] = StoredMetric(
            r["value"], inputs if isinstance(inputs, dict) else {}
        )
    return out


def _count_issues(conn: Database) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM data_quality_issue").fetchone()[0])


def gate_version(
    conn: Database,
    engine_version: str,
    *,
    filing_id: int | None = None,
    run_id: int | None = None,
) -> GateReport:
    """Gate the metrics stored under *engine_version* -- every filing, or just *filing_id* --
    and record the hits. Returns what was hit (whether or not it was already recorded)."""
    versions = MetricVersions(dict.fromkeys(METRIC_GROUPS, engine_version))
    filings = _load(conn, versions, filing_id)
    facts = load_facts(conn, filings)
    subperiods = load_subperiods(conn, filings)
    now = _now()
    report = GateReport(filings=len(filings))
    rows: list[tuple[Any, ...]] = []
    for fid, (aid, metrics) in filings.items():
        issues = evaluate(metrics)
        filing_facts = facts.get(fid)
        if filing_facts is not None:
            issues.extend(identity_issues(filing_facts, metrics))
        filing_subperiods = subperiods.get(fid)
        if filing_subperiods is not None:
            sub_results, _incomplete, _not_tested = subperiod_results(filing_subperiods)
            issues.extend(con06_issues(sub_results, metrics))
        for rule_id in {i.rule_id for i in issues}:
            report.filings_by_rule[rule_id] = report.filings_by_rule.get(rule_id, 0) + 1
        for rule_id in {i.rule_id for i in issues if i.severity == "HARD"}:
            report.hard_by_rule[rule_id] = report.hard_by_rule.get(rule_id, 0) + 1
        rows.extend(
            (
                fid,
                aid,
                i.metric[0],
                i.metric[1],
                engine_version,
                i.rule_id,
                i.severity,
                int(i.quarantined),
                i.value,
                json.dumps(dict(i.evidence), sort_keys=True),
                GATE_VERSION,
                now,
                run_id,
            )
            for i in issues
        )
    before = _count_issues(conn)
    conn.executemany(
        """
        INSERT OR IGNORE INTO data_quality_issue
            (filing_id, asset_id, metric_group, metric_name, metric_engine_version, rule_id,
             severity, quarantined, value, evidence_json, gate_version, created_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    report.inserted = _count_issues(conn) - before
    return report


def gate_all(
    conn: Database, *, engine_version: str | None = None, run_id: int | None = None
) -> dict[str, GateReport]:
    """The backfill: gate every stored filing under each metrics engine version (or just
    *engine_version*). Deterministic and LLM-free, so re-running it costs nothing."""
    present = sorted({v for vs in queries.metric_versions_present(conn).values() for v in vs})
    chosen = [v for v in present if engine_version is None or v == engine_version]
    return {v: gate_version(conn, v, run_id=run_id) for v in chosen}


# -- TTM cross-check (T-105 review; DQ-01 tolerance from T-150) ---------------------------------

TTM_CROSSCHECK_RULE: Final[str] = "DQ_TTM_CROSSCHECK"
# T-105's original relative gap. The gate below now uses DQ-01's tolerance; the constant is kept
# for `scripts/verify_t105.py`'s reproduction of that rule.
TTM_CROSSCHECK_TOLERANCE: Final[float] = 0.01


def record_ttm_crosscheck(  # noqa: PLR0913 - the filing's identity plus provenance
    conn: Database,
    filing_id: int,
    asset_id: int,
    flows: Mapping[str, TTMFlow],
    *,
    engine_version: str,
    run_id: int | None = None,
) -> int:
    """Where a flow's TTM was computable both ways -- the year-to-date identity (its value) and
    four recorded quarters (:attr:`TTMFlow.alt`) -- and they disagree, record one SOFT,
    **unquarantined** row under the pseudo-group ``ttm`` (never a metric group, so no consumer
    reads it as a metric). Nothing is quarantined or vetoed.

    Neither side is treated as the truth: the identity uses the filing's own *restated* comparative
    column, while the quarters are as-filed -- after AT&T's 2022 WarnerMedia spin-off its Q1 2022
    cogs were $10.351B as filed and $6.036B restated, and the four-quarter sum is the wrong side.
    A gap is still worth a look: it can equally mean a concept resolved differently in one filing.

    **This is not CON-06 as the checklist writes it.** DQC_0084 checks that a durational element's
    sub-periods aggregate "in the instance" -- within one filing -- whereas this check compares one
    filing's year-to-date identity with four quarters taken from *earlier* filings. It keeps T-105's
    "neither side is the truth"; it is a review flag, never a quarantine.

    **T-150** (F1, correcting C9): the level is DQ-01's -- ``diff = |identity - four_quarters|``;
    ``rounding = 0.5 x unit x n_terms`` with ``n_terms = :data:`_TTM_CROSSCHECK_TERMS``` (F1) and
    the unit inferred from the two constructed values (declared, since their operands are not
    available here); the base is the flow's own TTM net income when the item is an income-statement
    flow, absent for a cash-flow flow (no TTM change in cash is built), so a cash-flow not-pass is a
    WARN, never a BLOCK. The level and every DQ-01 term go in the evidence. Returns the rows
    inserted."""
    now = _now()
    ni_flow = flows.get("net_income")
    rows: list[tuple[Any, ...]] = []
    for item, flow in sorted(flows.items()):
        if flow.method != "ytd" or flow.alt is None:
            continue
        base = ni_flow.value if item in _INCOME_FLOW_ITEMS and ni_flow is not None else None
        terms = [flow.value, flow.alt]
        unit = l10_unit(terms)
        rounding = 0.5 * unit * _TTM_CROSSCHECK_TERMS
        diff = abs(flow.value - flow.alt)
        level = dq01_level(diff, rounding, base)
        if level == "pass":
            continue
        threshold = rounding if base is None else max(rounding, MATERIALITY * abs(base))
        evidence = {
            "identity": flow.value,
            "four_quarters": flow.alt,
            "diff": diff,
            "rounding": rounding,
            "unit": unit,
            "n_terms": _TTM_CROSSCHECK_TERMS,
            "base": base,
            "base_missing": base is None,
            "threshold": threshold,
            "level": level,
            "item": item,
        }
        rows.append(
            (
                filing_id,
                asset_id,
                "ttm",
                item,
                engine_version,
                TTM_CROSSCHECK_RULE,
                "SOFT",
                0,
                flow.value,
                json.dumps(evidence, sort_keys=True),
                GATE_VERSION,
                now,
                run_id,
            )
        )
    before = _count_issues(conn)
    conn.executemany(
        """
        INSERT OR IGNORE INTO data_quality_issue
            (filing_id, asset_id, metric_group, metric_name, metric_engine_version, rule_id,
             severity, quarantined, value, evidence_json, gate_version, created_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return _count_issues(conn) - before
