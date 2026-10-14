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
in :data:`WORKING_CAPITAL` from its own source: the B6 taxonomy definition for a standard
``us-gaap`` element, or the filer's caption (``financial_facts.label``, the filing's own words) for a
filer extension. An element whose definition or caption does not say it is a current item is left in
the cash flow and marked *ambiguous*, so the measurement can show the in/out sensitivity.

**Debt at each year end** is MET-07's (``Capital.debt``): the 10-K's balance sheet carries the two
year ends it shows, and the third year end comes from the previous 10-K. A year with no debt has no
ratio, and fewer than three ratios leave the average empty (the rule then has no input and does not
veto). Declared, not Moody's adjusted debt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from portfolio_common.db import Database

from fundamental_agent import db as agent_db
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import Statements

GROUP: Final[str] = "rule_input"
NAME: Final[str] = "utility_cash_coverage_3y"
YEARS: Final[int] = 3
THRESHOLD: Final[float] = 0.05  # the Ba band's lower bound (C13 p.7), the rule's cutoff

CURRENT: Final[str] = "current"
IN: Final[str] = "in"
# Why a line is left in the cash flow; ``ambiguous`` is the director's "does not say it is current"
# fallback and is the one the sensitivity compares in vs out (T-071 part 2a, rule 4).
NONCURRENT: Final[str] = "noncurrent"
REGULATORY: Final[str] = "regulatory"
PENSION: Final[str] = "pension"
ASSET_RETIREMENT: Final[str] = "asset_retirement"
AMBIGUOUS: Final[str] = "ambiguous"
UNCLASSIFIED: Final[str] = "unclassified"

# concept -> (class, reason). The us-gaap elements are classified from their B6 definition (the
# section "Working-capital Increase (Decrease) elements added for T-071 part 2a (D-13), 2026-10-14");
# the filer extensions from the filing's own caption. The full table, with each source quote, is in
# docs/model_fixes.md "T-071 part 2a".
WORKING_CAPITAL: Final[dict[str, tuple[str, str]]] = {
    # -- receivables (current) -----------------------------------------------------------------
    "us-gaap_IncreaseDecreaseInReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsReceivable": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsAndNotesReceivable": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInAccountsAndOtherReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInOtherReceivables": (CURRENT, "receivable"),
    "us-gaap_IncreaseDecreaseInIncomeTaxesReceivable": (CURRENT, "income taxes receivable"),
    "us-gaap_IncreaseDecreaseInInsuranceSettlementsReceivable": (
        CURRENT,
        "receivable, within one year",
    ),
    "us-gaap_IncreaseDecreaseDueFromAffiliates": (CURRENT, "receivable"),
    "evrg_IncreaseDecreaseInAccountsReceivablePledgedAsCollateral": (CURRENT, "receivable"),
    "ppl_IncreaseDecreaseUnbilledRevenue": (CURRENT, "receivable"),
    "xel_IncreaseDecreaseInAccruedUnbilledRevenues": (CURRENT, "receivable"),
    # -- inventories (current) -----------------------------------------------------------------
    "us-gaap_IncreaseDecreaseInInventories": (CURRENT, "inventory"),
    "us-gaap_IncreaseDecreaseInFossilFuelInventories": (CURRENT, "fuel inventory"),
    "us-gaap_IncreaseDecreaseInFuelInventories": (CURRENT, "fuel inventory"),
    "us-gaap_IncreaseDecreaseInMaterialsAndSupplies": (CURRENT, "materials and supplies"),
    "evrg_IncreaseDecreaseInEnergyRelatedInventory": (CURRENT, "fuel inventory"),
    # -- payables, accrued liabilities, income taxes (current) ---------------------------------
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
    "nix_IncreaseDecreaseInOtherAccruals": (CURRENT, "accrued liabilities"),
    # -- prepaid, customer deposits/advances (current) -----------------------------------------
    "us-gaap_IncreaseDecreaseInPrepaidExpense": (CURRENT, "prepaid"),
    "us-gaap_IncreaseDecreaseInPrepaidTaxes": (CURRENT, "prepaid"),
    "us-gaap_IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": (CURRENT, "prepaid"),
    "d_IncreaseDecreaseInPrepaymentsAndDepositsNet": (CURRENT, "prepaid"),
    "etr_IncreaseDecreaseInCustomerAdvancesInAidOfConstruction": (CURRENT, "customer deposits"),
    # -- other current assets / liabilities (current) ------------------------------------------
    "us-gaap_IncreaseDecreaseInOtherCurrentAssets": (CURRENT, "other current assets"),
    "us-gaap_IncreaseDecreaseInOtherCurrentLiabilities": (CURRENT, "other current liabilities"),
    "us-gaap_IncreaseDecreaseInOtherCurrentAssetsAndLiabilitiesNet": (CURRENT, "other current"),
    "nix_IncreaseDecreaseInPrepaymentsAndOtherCurrentAssets": (CURRENT, "other current assets"),
    # -- noncurrent ----------------------------------------------------------------------------
    "us-gaap_IncreaseDecreaseInOtherNoncurrentAssets": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInOtherNoncurrentLiabilities": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInOtherNoncurrentAssetsAndLiabilitiesNet": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredCharges": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredLiabilities": (IN, NONCURRENT),
    "us-gaap_IncreaseDecreaseInDeferredIncomeTaxes": (IN, NONCURRENT),
    "nix_IncreaseDecreaseInDeferredChargesAndOtherNonCurrentAssets": (IN, NONCURRENT),
    # -- regulatory (stays in the cash flow, C13 p.14) -----------------------------------------
    "us-gaap_IncreaseDecreaseInRegulatoryAssetsAndLiabilities": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRegulatoryLiabilities": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInOtherRegulatoryAssets": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRegulatoryClauseRevenue": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInDeferredFuelCosts": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInOverEnergyRecovery": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInBalancingAccountUtility": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInRecoverablePropertyDamageCosts": (IN, REGULATORY),
    "us-gaap_IncreaseDecreaseInAllowanceForEquityFundsUsedDuringConstruction": (IN, REGULATORY),
    "aep_IncreaseDecreaseInRegulatoryAssets": (IN, REGULATORY),
    "ato_IncreaseDecreaseInRegulatoryAssetsCurrent": (IN, REGULATORY),
    "pnw_IncreaseDecreaseDeferredFuelAndPurchasedPowerAmortization": (IN, REGULATORY),
    "so_IncreaseDecreaseInNaturalGasCostUnderRecoveryCurrentPortion": (IN, REGULATORY),
    "so_IncreaseDecreaseRetailFuelCostUnderRecoveryShortTerm": (IN, REGULATORY),
    # -- pension / postretirement --------------------------------------------------------------
    "us-gaap_IncreaseDecreaseInPensionAndPostretirementObligations": (IN, PENSION),
    "us-gaap_IncreaseDecreaseInPensionPlanObligations": (IN, PENSION),
    "us-gaap_IncreaseDecreaseInPostretirementObligations": (IN, PENSION),
    "fe_DefinedBenefitPlanPlanAssetsPeriodIncreaseDecreaseIncludingDiscontinuedOperations": (
        IN,
        PENSION,
    ),
    "dte_IncreaseDecreaseInPrepaidPostretirementCosts": (IN, AMBIGUOUS),
    # -- asset retirement ----------------------------------------------------------------------
    "us-gaap_IncreaseDecreaseInAssetRetirementObligations": (IN, ASSET_RETIREMENT),
    "ceg_OilAndGasNoncashAssetRetirementObligationIncreaseDecrease": (IN, ASSET_RETIREMENT),
    "sre_IncreaseDecreaseInAssetRetirementObligationForInvestmentInPropertyPlantAndEquipment": (
        IN,
        ASSET_RETIREMENT,
    ),
    # -- definition/caption does not say it is a current item (left in; the sensitivity compares) --
    "us-gaap_IncreaseDecreaseCashCollateralFromCounterparties": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInCommodityContractAssetsAndLiabilities": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInContractWithCustomerAsset": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInDepositOtherAssets": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInDerivativeAssets": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInDerivativeAssetsAndLiabilities": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInMarginDepositsOutstanding": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInOperatingLeaseLiability": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInOtherOperatingAssets": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInOtherOperatingLiabilities": (IN, AMBIGUOUS),
    "us-gaap_IncreaseDecreaseInOtherOperatingCapitalNet": (IN, AMBIGUOUS),
    "ceg_IncreaseDecreaseInOptionPremiumsCollectedReceived": (IN, AMBIGUOUS),
    "eix_IncreaseDecreaseInLossContingencyAccrual": (IN, AMBIGUOUS),
    "fe_IncreaseDecreaseInCollateral": (IN, AMBIGUOUS),
    "fe_IncreaseDecreaseInOhioSettlementCustomerRestitutionAndRefunds": (IN, AMBIGUOUS),
    "nix_IncreaseDecreaseInExchangeGasReceivablePayable": (IN, AMBIGUOUS),
    "pcg_IncreaseDecreaseInContributions": (IN, AMBIGUOUS),
    "pcg_IncreaseDecreaseToCatastrophesRelatedThirdPartyClaims": (IN, AMBIGUOUS),
    "peg_IncreaseDecreaseInObligationToReturnCashCollateral": (IN, AMBIGUOUS),
    "pnw_IncreaseDecreaseInLiabilityForUncertaintyInIncomeTaxes": (IN, AMBIGUOUS),
    "pnw_IncreaseDecreaseInOperatingLeaseRightOfUseAsset": (IN, AMBIGUOUS),
    "so_IncreaseDecreaseInCustomerRefunds": (IN, AMBIGUOUS),
    "sre_IncreaseDecreaseInBadDebtExpense": (IN, AMBIGUOUS),
    "vistra_IncreaseDecreaseInCommodityAndDerivativeAssetsAndLiabilities": (IN, AMBIGUOUS),
    "vistra_IncreaseDecreaseInMarginDepositsNet": (IN, AMBIGUOUS),
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


def classification(concept: str) -> tuple[str, str]:
    """``(class, reason)`` of a working-capital concept: the table's, else left in, ``unclassified``
    (the director's fallback: a definition or caption that does not say it is a current item)."""
    return WORKING_CAPITAL.get(concept, (IN, UNCLASSIFIED))


@dataclass(frozen=True)
class YearRatio:
    """One fiscal year's inputs and ratio."""

    period_end: str
    cfo: float | None
    working_capital: float | None
    ambiguous: float | None
    debt: float | None
    debt_source: str  # "10-K" or "previous 10-K"
    ratio: float | None  # (cfo - working_capital) / debt
    ratio_excluding_ambiguous: float | None  # (cfo - working_capital - ambiguous) / debt


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
) -> tuple[float, float, list[str], list[str]]:
    """``(current sum, ambiguous sum, current concepts, unclassified concepts)`` for one year."""
    current = ambiguous = 0.0
    current_concepts: list[str] = []
    unclassified: list[str] = []
    for row in conn.execute(
        "SELECT concept, value FROM financial_facts WHERE filing_id = ? AND period_key = ? "
        "AND concept LIKE '%IncreaseDecrease%'",
        (filing_id, period_key),
    ):
        if row["value"] is None:
            continue
        concept = str(row["concept"])
        if _is_cash_reconciliation(concept):
            continue
        klass, reason = classification(concept)
        if klass == CURRENT:
            current += float(row["value"])
            current_concepts.append(concept)
        elif reason == AMBIGUOUS:
            ambiguous += float(row["value"])
        elif reason == UNCLASSIFIED:
            unclassified.append(concept)
    return current, ambiguous, current_concepts, unclassified


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
    """The 10-K's three-year average of CFO-before-working-capital / total debt (D-13)."""
    years: list[YearRatio] = []
    unclassified: list[str] = []
    filing_period_end = conn.execute(
        "SELECT period_end FROM sec_filings WHERE id = ?", (filing_id,)
    ).fetchone()
    before = str(filing_period_end["period_end"]) if filing_period_end else ""
    for key in _cash_flow_years(conn, filing_id):
        period_end = key[:10]
        cfo = stmts.resolve("operating_cash_flow", key).value
        current, ambiguous, _concepts, unknown = _working_capital(conn, filing_id, key)
        unclassified.extend(unknown)
        debt = stmts.capital_at(key).debt.value
        source = "10-K"
        if debt is None:
            debt = _previous_10k_debt(conn, asset_id, before, period_end)
            source = "previous 10-K"
        working = cfo - current if cfo is not None else None
        working_out = cfo - current - ambiguous if cfo is not None else None
        ratio = working / debt if working is not None and debt is not None and debt > 0 else None
        ratio_out = (
            working_out / debt
            if working_out is not None and debt is not None and debt > 0
            else None
        )
        years.append(
            YearRatio(
                period_end=period_end,
                cfo=cfo,
                working_capital=current,
                ambiguous=ambiguous,
                debt=debt,
                debt_source=source,
                ratio=ratio,
                ratio_excluding_ambiguous=ratio_out,
            )
        )
    ratios = [y.ratio for y in years if y.ratio is not None]
    ratios_out = [
        y.ratio_excluding_ambiguous for y in years if y.ratio_excluding_ambiguous is not None
    ]
    average = sum(ratios) / len(ratios) if len(ratios) == YEARS else None
    average_out = sum(ratios_out) / len(ratios_out) if len(ratios_out) == YEARS else None
    inputs: dict[str, Any] = {
        "years": [
            {
                "period_end": y.period_end,
                "cfo": y.cfo,
                "working_capital": y.working_capital,
                "ambiguous": y.ambiguous,
                "debt": y.debt,
                "debt_source": y.debt_source,
                "ratio": y.ratio,
                "ratio_excluding_ambiguous": y.ratio_excluding_ambiguous,
            }
            for y in years
        ],
        "average": average,
        "average_excluding_ambiguous": average_out,
        "ambiguous_concepts": sorted(
            {c for c in WORKING_CAPITAL if WORKING_CAPITAL[c][1] == AMBIGUOUS}
        ),
        "unclassified_concepts": sorted(set(unclassified)),
    }
    if average is None:
        inputs["flag_utility_cash_coverage_missing_years"] = 1.0
    return MetricResult(NAME, average, "ratio", inputs)


def write(conn: Database, filing_id: int, asset_id: int, stmts: Statements) -> int:
    """Compute and store the rule input for one 10-K (a no-op for any other form). Idempotent."""
    form = conn.execute("SELECT form FROM sec_filings WHERE id = ?", (filing_id,)).fetchone()
    if form is None or not str(form["form"]).startswith("10-K"):
        return 0
    result = compute(conn, filing_id, asset_id, stmts)
    agent_db.record_metrics(conn, filing_id, [(GROUP, result)])
    return 1
