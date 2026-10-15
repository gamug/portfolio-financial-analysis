"""The regulated-utility cash-coverage rule input (D-13, T-071 part 2a).

The checklist's D-13 replaces ``NEGATIVE_FCF`` for a regulated utility with a HARD veto when the
**three-year average** of cash flow from operations **before working-capital changes** over total
debt is below 5% (C13 p.7's Ba band 5%-13%; C13 p.11, p.14, p.19, p.21). This module computes that
input from the stored facts of a 10-K and stores it as a *rule input* -- a ``fundamental_metrics``
row under the pseudo-group :data:`GROUP`, which is not one of ``kg_schema.versions.METRIC_GROUPS``,
so no metric consumer reads it, it is not a pillar member, it is not shown to the model, and it has
no applicability-matrix row.

**CFO before working capital.** ``NetCashProvidedByUsedInOperatingActivities`` (CON-08, via the
``operating_cash_flow`` resolver) minus the year's working-capital lines. Working capital is
**current items only**; regulatory assets and liabilities **stay in** the cash flow (C13 p.14,
checklist v1.3 D-13). Every ``IncreaseDecrease...`` element a regulated utility files is classified
**from its own source only** (reviewer A, 2026-10-14, rule 1): the B6 taxonomy definition for a
standard element -- a definition that states the period, or names a kind the checklist's rule 3
lists as current, settles it -- else the **filer's own caption** for that line
(``financial_facts.label``, the filing's own words), exactly as for a filer extension. An element
still silent after that is **left in** and marked *ambiguous*.

**Two bounds.** Because an ambiguous line's class is a judgement, the input carries two three-year
averages: :data:`AMBIGUOUS_IN` (the ambiguous lines left in the cash flow) and
:data:`AMBIGUOUS_AS_WC` (the same lines treated as working capital). ``UTILITY_CASH_COVERAGE`` is
HARD only when **both** bounds are below 5%; a pair that straddles 5%, or a missing bound, is
evaluated-not-hit and flagged (D-13's "missing inputs: no veto, flagged").

**Debt at each year end** is MET-07's (``Capital.debt``): the 10-K's balance sheet carries the two
year ends it shows, and the third year end comes from the previous 10-K. A year with no debt has no
ratio, and fewer than three ratios leave the average empty. Declared, not Moody's adjusted debt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from portfolio_common.db import Database

from fundamental_agent import db as agent_db
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements
from kg_schema.versions import (
    RULE_INPUT_GROUP,
    UTILITY_COVERAGE_AMBIGUOUS_FLAG,
    UTILITY_COVERAGE_BOUND_AS_WC,
    UTILITY_COVERAGE_BOUND_IN,
    UTILITY_COVERAGE_METRIC,
    UTILITY_COVERAGE_MISSING_BOUND_FLAG,
    UTILITY_COVERAGE_THRESHOLD,
)

GROUP: Final[str] = RULE_INPUT_GROUP
NAME: Final[str] = UTILITY_COVERAGE_METRIC
YEARS: Final[int] = 3
THRESHOLD: Final[float] = UTILITY_COVERAGE_THRESHOLD  # C13 p.7's Ba band lower bound
SENSITIVITY: Final[tuple[float, float]] = (0.05, 0.12)  # the checklist's "5%-12% same result" range

CURRENT: Final[str] = "current"
IN: Final[str] = "in"
# Why a line is left in the cash flow. ``ambiguous`` is the director's "still silent after the
# definition and the caption" fallback: the line the two bounds compare.
NONCURRENT: Final[str] = "noncurrent"
REGULATORY: Final[str] = "regulatory"
PENSION: Final[str] = "pension"
ASSET_RETIREMENT: Final[str] = "asset_retirement"
AMBIGUOUS: Final[str] = "ambiguous"

# The two bounds' names, in the stored inputs and the evidence.
AMBIGUOUS_IN: Final[str] = UTILITY_COVERAGE_BOUND_IN
AMBIGUOUS_AS_WC: Final[str] = UTILITY_COVERAGE_BOUND_AS_WC

# Caption markers (the director's rule 1): a standard element whose B6 definition is silent is
# classified from the filer's own caption for that line, as an extension is. A caption that mixes
# current and noncurrent items, or says neither, is ambiguous.
CURRENT_MARKERS: Final[tuple[str, ...]] = (
    "current",
    "receivable",
    "unbilled",
    "inventor",
    "payable",
    "accrued",
    "prepaid",
    "prepayment",
    "customer advance",
    "customer deposit",
    "income tax",
)
NONCURRENT_MARKERS: Final[tuple[str, ...]] = (
    "noncurrent",
    "non-current",
    "long-term",
    "long term",
    "deferred",
)
REGULATORY_MARKERS: Final[tuple[str, ...]] = ("regulatory",)
PENSION_MARKERS: Final[tuple[str, ...]] = ("pension", "postretirement", "opeb")
ARO_MARKERS: Final[tuple[str, ...]] = ("asset retirement", "aro")

# The standard elements whose B6 definition settles the class: it states the period (a current
# item), names a kind rule 3 lists as current, or says the line is noncurrent / regulatory /
# pension / asset-retirement. A definition that does none of those is silent -- the element is not
# here, and the filer's caption decides.
DEFINITION_CLASS: Final[dict[str, tuple[str, str]]] = {
    # a period stated by the definition
    "us-gaap_IncreaseDecreaseInDerivativeAssets": (CURRENT, "definition: within one year"),
    "us-gaap_IncreaseDecreaseInDerivativeAssetsAndLiabilities": (
        CURRENT,
        "definition: within one year",
    ),
    "us-gaap_IncreaseDecreaseInInsuranceSettlementsReceivable": (
        CURRENT,
        "definition: within one year",
    ),
    # a kind rule 3 lists as current
    "us-gaap_IncreaseDecreaseInReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsReceivable": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsAndNotesReceivable": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsAndOtherReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInOtherReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInIncomeTaxesReceivable": (CURRENT, "income taxes receivable"),
    "us-gaap_IncreaseDecreaseDueFromAffiliates": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInInventories": (CURRENT, "inventory"),
    "us-gaap_IncreaseDecreaseInFossilFuelInventories": (CURRENT, "fuel inventory"),
    "us-gaap_IncreaseDecreaseInFuelInventories": (CURRENT, "fuel inventory"),
    "us-gaap_IncreaseDecreaseInMaterialsAndSupplies": (CURRENT, "materials and supplies"),
    "us-gaap_IncreaseDecreaseInAccountsPayable": (CURRENT, "payable"),
    "us-gaap_IncreaseDecreaseInAccountsPayableTrade": (CURRENT, "payable"),
    "us-gaap_IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": (CURRENT, "payable, accrued"),
    "us-gaap_IncreaseDecreaseInOtherAccountsPayableAndAccruedLiabilities": (
        CURRENT,
        "payable, accrued",
    ),
    "us-gaap_IncreaseDecreaseInAccruedLiabilities": (CURRENT, "accrued liabilities"),
    "us-gaap_IncreaseDecreaseInOtherAccruedLiabilities": (CURRENT, "accrued liabilities"),
    "us-gaap_IncreaseDecreaseInEmployeeRelatedLiabilities": (CURRENT, "accrued liabilities"),
    "us-gaap_IncreaseDecreaseInInterestPayableNet": (CURRENT, "payable"),
    "us-gaap_IncreaseDecreaseInAccruedTaxesPayable": (CURRENT, "income taxes payable"),
    "us-gaap_IncreaseDecreaseInAccruedIncomeTaxesPayable": (CURRENT, "income taxes payable"),
    "us-gaap_IncreaseDecreaseInIncomeTaxesPayableNetOfIncomeTaxesReceivable": (
        CURRENT,
        "income taxes",
    ),
    "us-gaap_IncreaseDecreaseInPropertyAndOtherTaxesPayable": (CURRENT, "taxes payable"),
    "us-gaap_IncreaseDecreaseInPrepaidExpense": (CURRENT, "prepaid"),
    "us-gaap_IncreaseDecreaseInPrepaidTaxes": (CURRENT, "prepaid"),
    "us-gaap_IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": (CURRENT, "prepaid"),
    "us-gaap_IncreaseDecreaseInOtherCurrentAssets": (CURRENT, "other current assets"),
    "us-gaap_IncreaseDecreaseInOtherCurrentLiabilities": (CURRENT, "other current liabilities"),
    "us-gaap_IncreaseDecreaseInOtherCurrentAssetsAndLiabilitiesNet": (CURRENT, "other current"),
    # noncurrent
    "us-gaap_IncreaseDecreaseInOtherNoncurrentAssets": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInOtherNoncurrentLiabilities": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInOtherNoncurrentAssetsAndLiabilitiesNet": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredCharges": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredLiabilities": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredIncomeTaxes": (IN, NONCURRENT),
    # regulatory (stays in the cash flow, C13 p.14)
    "us-gaap_IncreaseDecreaseInRegulatoryAssetsAndLiabilities": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRegulatoryLiabilities": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInOtherRegulatoryAssets": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRegulatoryClauseRevenue": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInDeferredFuelCosts": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInOverEnergyRecovery": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInBalancingAccountUtility": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRecoverablePropertyDamageCosts": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInAllowanceForEquityFundsUsedDuringConstruction": (IN, REGULATORY),
    # pension / postretirement, asset retirement
    "us-gaap_IncreaseDecreaseInPensionAndPostretirementObligations": (IN, PENSION),
    "us-gaap_IncreaseDecreaseInPensionPlanObligations": (IN, PENSION),
    "us-gaap_IncreaseDecreaseInPostretirementObligations": (IN, PENSION),
    "us-gaap_IncreaseDecreaseInAssetRetirementObligations": (IN, ASSET_RETIREMENT),
}

# Filer extensions: no FASB definition exists, so the filer's own caption is the source. The
# decision per line is recorded here; the caption is quoted in docs/model_fixes.md.
EXTENSION_CLASS: Final[dict[str, tuple[str, str]]] = {
    "aep_IncreaseDecreaseInRegulatoryAssets": (IN, REGULATORY),
    "ato_IncreaseDecreaseInRegulatoryAssetsCurrent": (IN, REGULATORY),
    "ceg_IncreaseDecreaseInOptionPremiumsCollectedReceived": (IN, AMBIGUOUS),
    "ceg_OilAndGasNoncashAssetRetirementObligationIncreaseDecrease": (IN, ASSET_RETIREMENT),
    "d_IncreaseDecreaseInPrepaymentsAndDepositsNet": (CURRENT, "prepaid"),
    "dte_IncreaseDecreaseInPrepaidPostretirementCosts": (IN, PENSION),
    "eix_IncreaseDecreaseInLossContingencyAccrual": (IN, AMBIGUOUS),
    "etr_IncreaseDecreaseInCustomerAdvancesInAidOfConstruction": (CURRENT, "customer advances"),
    "evrg_IncreaseDecreaseInAccountsReceivablePledgedAsCollateral": (CURRENT, "receivable"),
    "evrg_IncreaseDecreaseInEnergyRelatedInventory": (CURRENT, "fuel inventory"),
    "fe_DefinedBenefitPlanPlanAssetsPeriodIncreaseDecreaseIncludingDiscontinuedOperations": (
        IN,
        PENSION,
    ),
    "fe_IncreaseDecreaseInCollateral": (IN, AMBIGUOUS),
    "fe_IncreaseDecreaseInOhioSettlementCustomerRestitutionAndRefunds": (IN, AMBIGUOUS),
    "nix_IncreaseDecreaseInDeferredChargesAndOtherNonCurrentAssets": (IN, NONCURRENT),
    "nix_IncreaseDecreaseInExchangeGasReceivablePayable": (IN, AMBIGUOUS),
    "nix_IncreaseDecreaseInOtherAccruals": (CURRENT, "accrued liabilities"),
    "nix_IncreaseDecreaseInPrepaymentsAndOtherCurrentAssets": (CURRENT, "current assets"),
    "pcg_IncreaseDecreaseInContributions": (IN, AMBIGUOUS),
    "pcg_IncreaseDecreaseToCatastrophesRelatedThirdPartyClaims": (IN, AMBIGUOUS),
    "peg_IncreaseDecreaseInObligationToReturnCashCollateral": (IN, AMBIGUOUS),
    "pnw_IncreaseDecreaseDeferredFuelAndPurchasedPowerAmortization": (IN, REGULATORY),
    "pnw_IncreaseDecreaseInLiabilityForUncertaintyInIncomeTaxes": (IN, AMBIGUOUS),
    "pnw_IncreaseDecreaseInOperatingLeaseRightOfUseAsset": (IN, AMBIGUOUS),
    "ppl_IncreaseDecreaseUnbilledRevenue": (CURRENT, "receivable"),
    "so_IncreaseDecreaseInCustomerRefunds": (IN, AMBIGUOUS),
    "so_IncreaseDecreaseInNaturalGasCostUnderRecoveryCurrentPortion": (IN, REGULATORY),
    "so_IncreaseDecreaseRetailFuelCostUnderRecoveryShortTerm": (IN, REGULATORY),
    "sre_IncreaseDecreaseInAssetRetirementObligationForInvestmentInPropertyPlantAndEquipment": (
        IN,
        ASSET_RETIREMENT,
    ),
    "sre_IncreaseDecreaseInBadDebtExpense": (IN, AMBIGUOUS),
    "vistra_IncreaseDecreaseInCommodityAndDerivativeAssetsAndLiabilities": (IN, AMBIGUOUS),
    "vistra_IncreaseDecreaseInMarginDepositsNet": (IN, AMBIGUOUS),
    "xel_IncreaseDecreaseInAccruedUnbilledRevenues": (CURRENT, "receivable"),
}

# The period's net change in cash -- the reconciliation total at the foot of the cash-flow
# statement, not a working-capital line. ``LIKE '%IncreaseDecrease%'`` catches it; it is skipped.
CASH_RECONCILIATION: Final[tuple[str, ...]] = (
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecrease",
    "CashAndCashEquivalentsPeriodIncreaseDecrease",
    "CashPeriodIncreaseDecrease",
)


def _is_cash_reconciliation(concept: str) -> bool:
    return any(marker in concept for marker in CASH_RECONCILIATION)


def _caption_class(label: str) -> tuple[str, str]:
    """``(class, reason)`` from the filer's own caption: a current item, a noncurrent one, a
    regulatory one, or -- when it says neither, or mixes the two -- left in and ambiguous."""
    text = (label or "").lower()
    if any(marker in text for marker in REGULATORY_MARKERS):
        return (IN, REGULATORY)
    if any(marker in text for marker in PENSION_MARKERS):
        return (IN, PENSION)
    if any(marker in text for marker in ARO_MARKERS):
        return (IN, ASSET_RETIREMENT)
    has_current = any(marker in text for marker in CURRENT_MARKERS)
    has_noncurrent = any(marker in text for marker in NONCURRENT_MARKERS)
    if has_current and not has_noncurrent:
        return (CURRENT, "caption")
    if has_noncurrent and not has_current:
        return (IN, NONCURRENT)
    return (IN, AMBIGUOUS)


def classification(concept: str, label: str | None = None) -> tuple[str, str]:
    """``(class, reason)`` of a working-capital line, from its source only (rule 1): the B6
    definition for a standard element, else the filer's own caption for that line; still silent ->
    left in, ``ambiguous``."""
    if concept in DEFINITION_CLASS:
        return DEFINITION_CLASS[concept]
    if concept in EXTENSION_CLASS:
        return EXTENSION_CLASS[concept]
    return _caption_class(label or "")


@dataclass(frozen=True)
class YearRatio:
    """One fiscal year's inputs and its two ratios."""

    period_end: str
    cfo: float | None
    working_capital: float | None
    ambiguous: float | None
    debt: float | None
    debt_source: str  # "10-K" or "previous 10-K"
    ratio_ambiguous_in: float | None  # (cfo - working_capital) / debt
    ratio_ambiguous_as_wc: float | None  # (cfo - working_capital - ambiguous) / debt


def _cash_flow_years(conn: Database, filing_id: int) -> list[str]:
    """The 10-K's cash-flow duration columns (three fiscal years), oldest first."""
    keys = [
        str(r["period_key"])
        for r in conn.execute(
            "SELECT DISTINCT period_key FROM financial_facts WHERE filing_id = ? "
            "AND statement = 'cash_flow' ORDER BY period_key",
            (filing_id,),
        )
    ]
    return sorted(k for k in keys if k.endswith(" (FY)"))


def _working_capital(
    conn: Database, filing_id: int, period_key: str
) -> tuple[float, float, list[str]]:
    """``(current sum, ambiguous sum, ambiguous concepts)`` for one year, each line classified from
    its own source (the definition, else the filer's caption)."""
    current = ambiguous = 0.0
    ambiguous_concepts: list[str] = []
    for row in conn.execute(
        "SELECT concept, label, value FROM financial_facts WHERE filing_id = ? AND period_key = ? "
        "AND concept LIKE '%IncreaseDecrease%'",
        (filing_id, period_key),
    ):
        if row["value"] is None:
            continue
        concept = str(row["concept"])
        if _is_cash_reconciliation(concept):
            continue
        klass, reason = classification(concept, row["label"])
        if klass == CURRENT:
            current += float(row["value"])
        elif reason == AMBIGUOUS:
            ambiguous += float(row["value"])
            ambiguous_concepts.append(concept)
    return current, ambiguous, ambiguous_concepts


def _previous_10k_debt(conn: Database, asset_id: int, before: str, target: str) -> float | None:
    """MET-07's debt at *target* (a prior year end) from the company's previous 10-K."""
    row = conn.execute(
        "SELECT id FROM sec_filings WHERE asset_id = ? AND form = '10-K' AND period_end < ? "
        "ORDER BY period_end DESC LIMIT 1",
        (asset_id, before),
    ).fetchone()
    if row is None:
        return None
    capital = agent_db.statements_from_facts(conn, int(row["id"])).capital_at(f"{target} (FY)")
    return capital.debt.value


def compute(conn: Database, filing_id: int, asset_id: int, stmts: Statements) -> MetricResult:
    """The 10-K's two three-year bounds of CFO-before-working-capital / total debt (D-13)."""
    years: list[YearRatio] = []
    ambiguous_concepts: list[str] = []
    filing = conn.execute(
        "SELECT period_end FROM sec_filings WHERE id = ?", (filing_id,)
    ).fetchone()
    before = str(filing["period_end"]) if filing else ""
    for key in _cash_flow_years(conn, filing_id):
        period_end = key[:10]
        cfo = stmts.resolve("operating_cash_flow", key).value
        current, ambiguous, concepts = _working_capital(conn, filing_id, key)
        ambiguous_concepts.extend(concepts)
        debt = stmts.capital_at(key).debt.value
        source = "10-K"
        if debt is None:
            debt = _previous_10k_debt(conn, asset_id, before, period_end)
            source = "previous 10-K"
        working = cfo - current if cfo is not None else None
        working_as_wc = cfo - current - ambiguous if cfo is not None else None
        ratio_in = ratio_wc = None
        if debt is not None and debt > 0:
            ratio_in = working / debt if working is not None else None
            ratio_wc = working_as_wc / debt if working_as_wc is not None else None
        years.append(
            YearRatio(
                period_end=period_end,
                cfo=cfo,
                working_capital=current,
                ambiguous=ambiguous,
                debt=debt,
                debt_source=source,
                ratio_ambiguous_in=ratio_in,
                ratio_ambiguous_as_wc=ratio_wc,
            )
        )
    ratios_in = [y.ratio_ambiguous_in for y in years if y.ratio_ambiguous_in is not None]
    ratios_wc = [y.ratio_ambiguous_as_wc for y in years if y.ratio_ambiguous_as_wc is not None]
    average_in = sum(ratios_in) / len(ratios_in) if len(ratios_in) == YEARS else None
    average_wc = sum(ratios_wc) / len(ratios_wc) if len(ratios_wc) == YEARS else None
    inputs: dict[str, Any] = {
        "years": [
            {
                "period_end": y.period_end,
                "cfo": y.cfo,
                "working_capital": y.working_capital,
                "ambiguous": y.ambiguous,
                "debt": y.debt,
                "debt_source": y.debt_source,
                "ratio_ambiguous_in": y.ratio_ambiguous_in,
                "ratio_ambiguous_as_wc": y.ratio_ambiguous_as_wc,
            }
            for y in years
        ],
        AMBIGUOUS_IN: average_in,
        AMBIGUOUS_AS_WC: average_wc,
        "ambiguous_concepts": sorted(set(ambiguous_concepts)),
        "threshold": THRESHOLD,
        "sensitivity_range": list(SENSITIVITY),
    }
    if average_in is None or average_wc is None:
        inputs[UTILITY_COVERAGE_MISSING_BOUND_FLAG] = 1.0
    if (
        average_in is not None
        and average_wc is not None
        and (average_in < THRESHOLD) != (average_wc < THRESHOLD)
    ):
        inputs[UTILITY_COVERAGE_AMBIGUOUS_FLAG] = 1.0
    # The stored value is the primary bound, D-13's wording ("before working-capital changes": the
    # current items removed, an ambiguous line left in). The rule reads both bounds from the inputs.
    return MetricResult(NAME, average_in, "ratio", inputs)


def write(conn: Database, filing_id: int, asset_id: int, stmts: Statements) -> int:
    """Compute and store the rule input for one 10-K (a no-op for any other form). Idempotent."""
    form = conn.execute("SELECT form FROM sec_filings WHERE id = ?", (filing_id,)).fetchone()
    if form is None or not str(form["form"]).startswith("10-K"):
        return 0
    agent_db.record_metrics(conn, filing_id, [(GROUP, compute(conn, filing_id, asset_id, stmts))])
    return 1
