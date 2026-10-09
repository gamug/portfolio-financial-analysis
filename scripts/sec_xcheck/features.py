"""One record per stored filing: what the resolver read, and what else the filing carried (level b).

``extract(stmts, key)`` re-runs today's ``Statements`` resolution at the filing's own period and records, for each
line item the audit measures, **which concept it resolved from and how** (``concept``, ``standard``, ``label``,
``fallback`` or ``label_fallback``), its value, and the values of the *other* candidate concepts the same column carried.
The measurements in :mod:`sec_xcheck.resolver_measures` are pure functions over these records, so each can be
tested on a hand-built record and re-run without the database.

Everything here reads non-dimensional, non-abstract stored rows only (``financial_facts`` keeps no others), so a rule
that depends on a dimensional fact is outside level (b) and is reported ``source prevalence incomplete``.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fundamental_agent.metrics import COMPUTERS
from fundamental_agent.statements import REGISTRY, Statements
from fundamental_agent.statements import _matches as row_matches
from fundamental_agent.statements import _numeric as numeric

GAAP = "us-gaap_"
MIN_SUMMED = 2  # a sum needs at least two components

OCF_TOTAL = GAAP + "NetCashProvidedByUsedInOperatingActivities"
OCF_CONT = GAAP + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"
LT_CONCEPTS = tuple(REGISTRY["long_term_debt"].concepts)
ST_CONCEPTS = tuple(REGISTRY["short_term_debt"].concepts)
CASH_CONCEPTS = tuple(REGISTRY["cash"].concepts)
STI_CONCEPTS = tuple(REGISTRY["short_term_investments"].concepts)
DA_CONCEPTS = tuple(REGISTRY["depreciation_amortization"].concepts)
EQUITY_CONCEPTS = tuple(REGISTRY["equity"].concepts)
NI_CONCEPTS = (
    GAAP + "NetIncomeLoss",
    GAAP + "ProfitLoss",
    GAAP + "NetIncomeLossAvailableToCommonStockholdersBasic",
    GAAP + "NetIncomeLossAttributableToNoncontrollingInterest",
)
TAX_CONCEPTS = (
    GAAP + "IncomeTaxExpenseBenefit",
    GAAP + "CurrentIncomeTaxExpenseBenefit",
    GAAP + "DeferredIncomeTaxExpenseBenefit",
)
CAPEX_CONCEPTS = tuple(
    GAAP + name
    for name in (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToExploreAndDevelopOilAndGasProperties",
        "PaymentsToAcquireOilAndGasPropertyAndEquipment",
        "PaymentsToAcquireOilAndGasProperty",
        "PaymentsToAcquireOilAndGasEquipment",
        "PaymentsForProceedsFromProductiveAssets",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsToAcquireMachineryAndEquipment",
        "PaymentsToAcquireOtherProductiveAssets",
        "PaymentsToAcquireFurnitureAndFixtures",
        "PaymentsForConstructionInProcess",
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireRealEstate",
        "PaymentsToAcquireRealEstateHeldForInvestment",
        "PaymentsToAcquireAndDevelopRealEstate",
    )
)
DEAD_CONCEPTS = tuple(
    GAAP + name
    for name in (
        "CostOfGoodsSold",
        "CostOfServices",
        "ShareBasedCompensationExpense",
        "AvailableForSaleSecuritiesCurrent",
    )
)
SIGNED_ITEMS = (
    "capital_expenditure",
    "interest_expense",
    "stock_based_compensation",
    "income_tax",
    "cogs",
)


IDENTITY_BALANCE = tuple(
    GAAP + n
    for n in (
        "Assets", "LiabilitiesAndStockholdersEquity", "AssetsCurrent", "AssetsNoncurrent", "Liabilities",
        "LiabilitiesCurrent", "LiabilitiesNoncurrent", "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "MinorityInterest",
    )
)  # fmt: skip
IDENTITY_FLOW = tuple(
    GAAP + n
    for n in (
        "ProfitLoss", "NetIncomeLoss", "NetIncomeLossAttributableToNoncontrollingInterest",
        "NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInFinancingActivities",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations",
    )
)  # fmt: skip


def any_statement(
    stmts: Statements, concepts: tuple[str, ...], column: str | None
) -> dict[str, float]:
    """``{concept: value}`` of the first non-dimensional row of each concept in *column*, on any of the statements."""
    out: dict[str, float] = {}
    for statement in ("income_statement", "balance_sheet", "cash_flow"):
        for concept, value in concept_values(stmts, statement, concepts, column).items():
            out.setdefault(concept, value)
    return out


def capex_captions(stmts: Statements, column: str | None) -> dict[str, float]:
    """``{concept: value}`` of the cash-flow rows the resolver's capex caption fallback would read: a label that reads
    "capital expenditure(s)" without an accrual word (``REGISTRY["capital_expenditure"].label_fallback`` and its
    exclusion), whatever the concept. The resolver uses the caption only when exactly one such concept exists."""
    spec = REGISTRY["capital_expenditure"]
    wanted = re.compile(spec.label_fallback, re.IGNORECASE)
    unwanted = re.compile(spec.label_fallback_exclude or r"(?!)", re.IGNORECASE)
    out: dict[str, float] = {}
    if column is None:
        return out
    for row in stmts.raw.get("cash_flow", []):
        label = str(row.get("label") or "")
        if row.get("abstract") or row.get("dimension") or not wanted.search(label):
            continue
        value = numeric(row.get(column)) if not unwanted.search(label) else None
        if value is not None:
            out.setdefault(str(row.get("concept")), value)
    return out


def column_of(stmts: Statements, item: str, key: str) -> str | None:
    """The column a line item reads at *key*: the period itself, or a balance sheet's instant (``_instant_for``)."""
    return stmts.resolve_column(item, key)


def concept_values(
    stmts: Statements, statement: str, concepts: tuple[str, ...], column: str | None
) -> dict[str, float]:
    """``{concept: value}`` of the first non-dimensional row of each concept with a value in *column*."""
    out: dict[str, float] = {}
    if column is None:
        return out
    for row in stmts.raw.get(statement, []):
        concept = row.get("concept")
        if row.get("abstract") or row.get("dimension") or concept not in concepts or concept in out:
            continue
        value = numeric(row.get(column))
        if value is not None:
            out[str(concept)] = value
    return out


def match_kind(row: dict[str, Any], spec: Any) -> str:
    """How a row matched a line item: ``concept`` (exact), ``standard`` (the gateway's standard concept) or ``label``."""
    if row.get("concept") in spec.concepts:
        return "concept"
    return "standard" if spec.standard and row.get("standard_concept") in spec.standard else "label"


def sum_resolution(
    stmts: Statements, spec: Any, column: str, value: float
) -> dict[str, Any] | None:
    """Several co-reported streams and no filed total: the value is the sum of the components (named)."""
    if not spec.sum_components:
        return None
    parts = stmts._matching_component_values(spec, column)
    if len(parts) < MIN_SUMMED:
        return None
    return {"value": value, "concept": " + ".join(sorted(parts)), "how": "sum"}


def resolution(stmts: Statements, item: str, key: str) -> dict[str, Any]:
    """Which row ``stmts.get`` read for *item*: ``{value, concept, how}``; ``how`` names the tier that supplied it."""
    spec = REGISTRY[item]
    column = column_of(stmts, item, key)
    value = stmts.get(item, key)
    if value is None or column is None:
        return {"value": None, "concept": None, "how": None}
    if spec.total_concepts:
        for row in stmts._rows_for(spec):
            if row.get("concept") in spec.total_concepts and numeric(row.get(column)) is not None:
                return {"value": value, "concept": str(row.get("concept")), "how": "total"}
    summed = sum_resolution(stmts, spec, column, value)
    if summed:
        return summed
    for row in stmts._rows_for(spec):
        if row_matches(row, spec) and numeric(row.get(column)) is not None:
            return {
                "value": value,
                "concept": str(row.get("concept")),
                "how": match_kind(row, spec),
            }
    for tier, concept in enumerate(spec.fallback_concepts):
        if stmts._concept_value(spec, column, concept) is not None:
            return {"value": value, "concept": concept, "how": f"fallback{tier + 1}"}
    how = "label_fallback" if spec.label_fallback else "derived"  # derived: a sum or a rebuild
    return {"value": value, "concept": None, "how": how}


def revenue_path(stmts: Statements, key: str) -> dict[str, Any]:
    """How revenue was derived at *key* (L-09): a filed total, T-117's label correction, T-140's rebuild, or a sum."""
    spec = REGISTRY["revenue"]
    total = stmts._first_total_match(spec, key)
    out: dict[str, Any] = {
        "filed_total": total,
        "components": stmts._matching_component_values(spec, key),
    }
    if total is None:
        rebuild = stmts.rebuild_total(spec, key)
        out["rebuild"] = (
            None
            if rebuild is None
            else {
                "anchor_label": rebuild.anchor_label,
                "anchor_value": rebuild.anchor_value,
                "between": list(rebuild.between),
                "value": rebuild.value,
                "refusal": rebuild.refusal,
                "corroborated": rebuild.corroborated,
            }
        )
        return out
    corrected, contradicted = stmts._label_total_correction(spec, key, total)
    out["label_correction"] = {"corrected": corrected, "contradicted": contradicted}
    return out


def period_info(stmts: Statements, key: str) -> dict[str, Any]:
    """The columns around the filing's own period: the prior column the code pairs it with, and the FY columns."""
    period = next(p for p in stmts.periods if p.key == key)
    prior = stmts.prior_of(period)
    gap = None
    if prior is not None:
        gap = (dt.date.fromisoformat(period.date) - dt.date.fromisoformat(prior.date)).days
    return {
        "tag": period.tag,
        "prior_key": prior.key if prior else None,
        "prior_gap_days": gap,
        "fy_dates": [p.date for p in stmts.fy_periods()],
    }


def replay_metrics(stmts: Statements, key: str) -> dict[str, dict[str, Any]]:
    """Today's metric groups at *key*, without TTM: ``{name: {"value": v, "inputs": {...}}}``.

    The conditions the metric rules test (a denominator <= 0, a growth base <= 0, the 21% default tax rate) depend on
    line items only, so they hold with or without the 10-Q TTM substitution; the values themselves do not, and
    are not used for the 10-Q TTM metrics.
    """
    period = next((p for p in stmts.periods if p.key == key), None)
    prior = stmts.prior_of(period) if period else None
    out: dict[str, dict[str, Any]] = {}
    for group, compute in COMPUTERS.items():
        for result in compute(stmts, key, prior.key if prior else None, None):
            out[result.name] = {
                "group": group,
                "value": result.value,
                "inputs": dict(result.inputs),
            }
    return out


def extract(stmts: Statements, key: str, *, metrics: bool = True) -> dict[str, Any]:
    """The audit record of one filing at its own column *key*."""
    inst = stmts._instant_for(key)
    record: dict[str, Any] = {
        "key": key,
        "instant": inst,
        "instant_exact": inst is not None and inst[:10] == key[:10],
        "items": {item: resolution(stmts, item, key) for item in REGISTRY},
    }
    record["identity"] = {
        **any_statement(stmts, IDENTITY_BALANCE, inst),
        **any_statement(stmts, IDENTITY_FLOW, key),
    }
    record["periods"] = period_info(stmts, key)
    record["metrics"] = replay_metrics(stmts, key) if metrics else {}
    record["revenue_path"] = revenue_path(stmts, key)
    record["ocf"] = concept_values(stmts, "cash_flow", (OCF_TOTAL, OCF_CONT), key)
    record["lt"] = concept_values(stmts, "balance_sheet", LT_CONCEPTS, inst)
    record["st"] = concept_values(stmts, "balance_sheet", ST_CONCEPTS, inst)
    record["cash"] = concept_values(stmts, "balance_sheet", CASH_CONCEPTS, inst)
    record["sti"] = concept_values(stmts, "balance_sheet", STI_CONCEPTS, inst)
    record["da"] = concept_values(stmts, "cash_flow", DA_CONCEPTS, key)
    record["equity"] = concept_values(stmts, "balance_sheet", EQUITY_CONCEPTS, inst)
    record["ni"] = concept_values(stmts, "income_statement", NI_CONCEPTS, key)
    record["tax"] = concept_values(stmts, "income_statement", TAX_CONCEPTS, key)
    record["capex"] = concept_values(stmts, "cash_flow", CAPEX_CONCEPTS, key)
    record["capex_captions"] = capex_captions(stmts, key)
    record["dead"] = {
        s: concept_values(stmts, s, DEAD_CONCEPTS, key if s != "balance_sheet" else inst)
        for s in ("income_statement", "balance_sheet", "cash_flow")
    }
    return record
