"""T-159a: the registry substitutes -- a line item the filing reports under **another standard
concept**.

The brief's four groups, one test each:

* (a) the item's total under another standard concept -- a filing with only the substitute
  resolves to it (with its flag); a filing with the item's own concept **and** the substitute keeps
  the item's own concept (ID-02a precedence);
* (b) a component -- the sole filed member of one family is the total; two families leave the item
  empty; the short-term-investment classes (disjoint, ASC 320) are summed; ``Cash`` is read only
  when no cash-equivalents fact is filed;
* (c) the debt components -- they enter the MET-07 / ID-02b sum, once (N4);
* (d) the exact two-part sums -- both parts give the sum, one part alone gives nothing.

And the rejected list: none of those concepts may ever resolve.
"""

from __future__ import annotations

from typing import Any

import pytest

from fundamental_agent.metrics import capital as capital_metrics
from fundamental_agent.metrics import efficiency, profitability
from fundamental_agent.statements import REGISTRY, Statements

G = "us-gaap_"
KEY = "2024-12-31 (FY)"
INSTANT = "2024-12-31"

# The brief's rejected list: none of these may ever resolve (the brief, §2).
REJECTED = (
    "AccountsReceivableGrossCurrent",
    "InventoryGross",
    "EmployeeBenefitsAndShareBasedCompensationNoncash",
    "PaymentsToAcquireRealEstate",
    "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
    "OtherMinorityInterests",
    "NotesPayable",
    "InterestIncomeExpenseNet",
    "InterestIncomeExpenseNonoperatingNet",
    "InterestIncomeExpenseNonoperating",
)


def _row(concept: str, label: str, **periods: float) -> dict[str, Any]:
    return {
        "concept": G + concept,
        "label": label,
        "standard_concept": None,
        "abstract": False,
        "dimension": False,
        "preferred_sign": 1.0,
        **periods,
    }


def _balance(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row, **{INSTANT: row[INSTANT]}) for row in rows]


def _payload(
    *,
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash_flow or [],
        }
    )


# -- (a) the item's total under another standard concept -------------------------------------------


@pytest.mark.parametrize(
    ("item", "concept", "value", "statement"),
    [
        ("receivables", "AccountsNotesAndLoansReceivableNetCurrent", 100.0, "balance"),
        ("receivables", "AccountsAndOtherReceivablesNetCurrent", 100.0, "balance"),
        ("receivables", "AccountsReceivableNet", 100.0, "balance"),
        (
            "inventory",
            "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
            50.0,
            "balance",
        ),
        ("inventory", "RetailRelatedInventoryMerchandise", 50.0, "balance"),
        ("redeemable_nci", "RedeemableNoncontrollingInterestEquityFairValue", 20.0, "balance"),
        ("minority_interest", "MinorityInterestInOperatingPartnerships", 30.0, "balance"),
        ("interest_expense", "InterestExpenseOperating", 40.0, "income"),
        ("interest_expense", "InterestExpenseBorrowings", 40.0, "income"),
    ],
)
def test_a_filing_with_only_the_substitute_resolves_to_it(
    item: str, concept: str, value: float, statement: str
) -> None:
    """(a) The substitute is the item's total under another standard concept: the filing has no
    other concept for the item, so the item resolves to the substitute."""
    row = _row(concept, concept, **{KEY: value} if statement == "income" else {INSTANT: value})
    stmts = _payload(income=[row]) if statement == "income" else _payload(balance=[row])
    resolution = stmts.resolve(item, KEY)
    assert resolution.value == value
    assert resolution.concept == G + concept
    assert resolution.how == "substitute"


def test_the_item_s_own_concept_wins_over_the_substitute() -> None:
    """ID-02a: a filing with the item's own concept **and** the substitute keeps the item's own
    concept -- the substitute never displaces a value the registry already reads."""
    stmts = _payload(
        balance=[
            _row("AccountsReceivableNetCurrent", "Trade receivables", **{INSTANT: 100.0}),
            _row("AccountsReceivableNet", "Accounts receivable, net", **{INSTANT: 999.0}),
        ]
    )
    resolution = stmts.resolve("receivables", KEY)
    assert resolution.value == 100.0
    assert resolution.concept == G + "AccountsReceivableNetCurrent"
    assert resolution.how == "concept"


def test_the_cost_of_sales_substitute_is_flagged_cogs_excludes_da() -> None:
    """(a) The cost of sales without the D&A the filer presents on its own line: the item resolves
    to it and the flag ``cogs_excludes_da`` says the D&A is not in it."""
    stmts = _payload(
        income=[
            _row(
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                "Cost of sales, excluding D&A",
                **{KEY: 500.0},
            )
        ]
    )
    resolution = stmts.resolve("cogs", KEY)
    assert resolution.value == 500.0
    assert (
        resolution.concept
        == G + "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"
    )
    assert resolution.flags == ("cogs_excludes_da",)


def test_the_cogs_flag_reaches_every_metric_that_reads_the_item() -> None:
    """(a) The flag is on the item **and** on every metric that reads it (the gross margin and the
    inventory turnover)."""
    stmts = _payload(
        income=[
            _row(
                "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                "Cost of sales, excluding D&A",
                **{KEY: 500.0},
            ),
            _row("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue", **{KEY: 1000.0}),
        ],
        balance=[
            _row("InventoryNet", "Inventory", **{INSTANT: 200.0}),
            _row("AccountsReceivableNetCurrent", "Receivables", **{INSTANT: 100.0}),
        ],
    )
    gross = {r.name: r for r in profitability.compute(stmts, KEY, KEY, None)}["gross_margin"]
    turnover = {r.name: r for r in efficiency.compute(stmts, KEY, KEY, None)}["inventory_turnover"]
    assert gross.inputs["flag_cogs_cogs_excludes_da"] == 1.0
    assert turnover.inputs["flag_cogs_cogs_excludes_da"] == 1.0


def test_the_net_income_substitute_is_flagged_net_income_to_common() -> None:
    """(a) "Net income available to common" is the parent's net income when no preferred dividend
    is filed for the period; the flag says the line was read."""
    stmts = _payload(
        income=[
            _row("NetIncomeLossAvailableToCommonStockholdersBasic", "Net income", **{KEY: 80.0})
        ]
    )
    resolution = stmts.resolve("net_income", KEY)
    assert resolution.value == 80.0
    assert resolution.concept == G + "NetIncomeLossAvailableToCommonStockholdersBasic"
    assert "net_income_to_common" in resolution.flags


def test_the_net_income_substitute_is_refused_when_a_preferred_dividend_is_filed() -> None:
    """(a) With a preferred dividend filed for the period, the "available to common" line is after
    it and is not the parent's earnings (N13)."""
    stmts = _payload(
        income=[
            _row("NetIncomeLossAvailableToCommonStockholdersBasic", "Net income", **{KEY: 80.0}),
            _row(
                "PreferredStockDividendsAndOtherAdjustments",
                "Preferred dividends",
                **{KEY: 5.0},
            ),
        ]
    )
    resolution = stmts.resolve("net_income", KEY)
    assert resolution.value is None
    assert "net_income_to_common_refused_preferred_dividends" in resolution.flags


def test_the_net_income_substitute_never_displaces_the_id_03_value() -> None:
    """(a) "Only when no ``NetIncomeLoss`` / ``ProfitLoss`` / ID-03 value": with a ``ProfitLoss``
    filed, the substitute is not read (the ID-03 path owns the item)."""
    stmts = _payload(
        income=[
            _row("ProfitLoss", "Net income incl. NCI", **{KEY: 90.0}),
            _row("NetIncomeLossAvailableToCommonStockholdersBasic", "Net income", **{KEY: 80.0}),
        ]
    )
    resolution = stmts.resolve("net_income", KEY)
    assert resolution.value == 90.0
    assert resolution.concept == G + "ProfitLoss"


# -- (b) a component ---------------------------------------------------------------------------------


def test_a_sole_component_of_one_family_is_the_inventory() -> None:
    """(b) A component is accepted only when it is the sole filed member of one family: then it
    is the whole of the item (ID-02b)."""
    stmts = _payload(balance=[_row("InventoryFinishedGoods", "Finished goods", **{INSTANT: 300.0})])
    resolution = stmts.resolve("inventory", KEY)
    assert resolution.value == 300.0
    assert resolution.concept == G + "InventoryFinishedGoods"
    assert resolution.how == "substitute"


def test_two_component_families_leave_the_inventory_empty() -> None:
    """(b) Two families are filed: neither member is the total, so the item stays empty (XOM's
    ``EnergyRelatedInventory`` beside ``InventoryPartsAndComponentsNetOfReserves``)."""
    stmts = _payload(
        balance=[
            _row("EnergyRelatedInventory", "Inventories", **{INSTANT: 20_078_000_000.0}),
            _row(
                "InventoryPartsAndComponentsNetOfReserves",
                "Parts and components",
                **{INSTANT: 4_018_000_000.0},
            ),
        ]
    )
    resolution = stmts.resolve("inventory", KEY)
    assert resolution.value is None


def test_two_members_of_one_family_leave_the_inventory_empty() -> None:
    """(b) Two members of the **same** family are filed: the family is not the total either."""
    stmts = _payload(
        balance=[
            _row("InventoryFinishedGoods", "Finished goods", **{INSTANT: 300.0}),
            _row("InventoryFinishedGoodsNetOfReserves", "Finished goods, net", **{INSTANT: 250.0}),
        ]
    )
    assert stmts.resolve("inventory", KEY).value is None


def test_the_inventory_total_wins_over_its_own_components() -> None:
    """(a) The total under another standard concept is read before the component families."""
    stmts = _payload(
        balance=[
            _row("InventoryNet", "Inventories", **{INSTANT: 400.0}),
            _row("InventoryFinishedGoods", "Finished goods", **{INSTANT: 300.0}),
        ]
    )
    resolution = stmts.resolve("inventory", KEY)
    assert resolution.value == 400.0
    assert resolution.concept == G + "InventoryNet"


def test_the_short_term_investment_classes_are_summed() -> None:
    """(b) The ASC 320 classes are **disjoint** (trading, available for sale, held to maturity,
    other), so several filed classes are summed -- each class counts once."""
    stmts = _payload(
        balance=[
            _row(
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                "Available-for-sale securities",
                **{INSTANT: 100.0},
            ),
            _row("HeldToMaturitySecuritiesCurrent", "Held-to-maturity", **{INSTANT: 40.0}),
            _row("OtherShortTermInvestments", "Other short-term investments", **{INSTANT: 25.0}),
        ]
    )
    resolution = stmts.resolve("short_term_investments", KEY)
    assert resolution.value == 165.0
    assert resolution.how == "map"


def test_a_short_term_investment_class_counts_once() -> None:
    """(b) Within a class, its own total wins over its components (the trading class: the total
    ``TradingSecurities`` covers ``TradingSecuritiesDebt`` / ``TradingSecuritiesEquity``)."""
    stmts = _payload(
        balance=[
            _row("TradingSecurities", "Trading securities", **{INSTANT: 200.0}),
            _row("TradingSecuritiesDebt", "Trading securities (debt)", **{INSTANT: 150.0}),
            _row("OtherShortTermInvestments", "Other short-term investments", **{INSTANT: 25.0}),
        ]
    )
    resolution = stmts.resolve("short_term_investments", KEY)
    assert resolution.value == 225.0
    assert "substitute_class_multiple" in resolution.flags


def test_the_filed_short_term_investments_are_no_longer_read_as_zero() -> None:
    """(b) The short-term investments the filing reports replace the defaulted 0 -- the only
    defaulted input (MD-02 / MD-03 / MD-35)."""
    stmts = _payload(
        balance=[
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash", **{INSTANT: 500.0}),
            _row(
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                "Available-for-sale securities",
                **{INSTANT: 1_126_400_000.0},
            ),
        ]
    )
    cap = stmts.capital_at(INSTANT)
    net = capital_metrics.net_short_term_investments(cap)
    assert net.value == 1_126_400_000.0
    assert net.how == "map"


def test_the_bare_cash_line_is_not_read_when_a_cash_equivalents_fact_is_filed() -> None:
    """(b) ``Cash`` is read only when no ``CashEquivalentsAtCarryingValue`` is filed: with one,
    the cash and cash equivalents are the map's own read (the ES map), never the bare line."""
    stmts = _payload(
        balance=[
            _row("Cash", "Cash", **{INSTANT: 500.0}),
            _row("CashEquivalentsAtCarryingValue", "Cash equivalents", **{INSTANT: 20.0}),
        ]
    )
    resolution = stmts.resolve("cash", KEY)
    assert resolution.value == 520.0
    assert resolution.concept == f"{G}Cash + {G}CashEquivalentsAtCarryingValue"


# -- (c) the debt components -------------------------------------------------------------------------


def test_a_debt_component_enters_the_long_term_debt_sum_once() -> None:
    """(c) The components enter the MET-07 / ID-02b sum -- never a free alternative -- and each
    enters it once (N4: no double counting)."""
    stmts = _payload(
        balance=[
            _row("SeniorNotes", "Senior notes", **{INSTANT: 1_000.0}),
            _row("ConvertibleLongTermNotesPayable", "Convertible notes", **{INSTANT: 500.0}),
        ]
    )
    resolution = stmts.resolve("long_term_debt", KEY)
    assert resolution.value == 1_500.0
    assert resolution.how == "map"
    assert resolution.concept == (f"{G}ConvertibleLongTermNotesPayable + {G}SeniorNotes")


def test_a_current_maturity_is_never_added_to_a_long_term_component() -> None:
    """(c) N4: the current maturities are a short-term term; they are never added to a long-term
    line that does not already include them."""
    stmts = _payload(
        balance=[
            _row("SeniorNotes", "Senior notes", **{INSTANT: 1_000.0}),
            _row("LongTermDebtCurrent", "Current maturities of long-term debt", **{INSTANT: 100.0}),
        ]
    )
    long_term = stmts.resolve("long_term_debt", KEY)
    short_term = stmts.resolve("short_term_debt", KEY)
    assert long_term.value == 1_000.0
    assert short_term.value == 100.0


# -- (d) the exact two-part sums ----------------------------------------------------------------------


def test_the_da_exact_sum_is_read_when_both_parts_are_filed() -> None:
    """(d) D&A = ``Depreciation`` + ``AmortizationOfIntangibleAssets``: both parts filed for the
    period give the sum, with the provenance ``how`` ``sourced_map``.

    The exact sum is the **last** tier: the existing sourced sum (the four D&A-nature children of
    the FASB 2026 arc, refused when another D&A-named line is on the face) is read first, so the
    exact sum takes over only where that sum is refused."""
    stmts = _payload(
        cash_flow=[
            _row("Depreciation", "Depreciation", **{KEY: 304.0}),
            _row("AmortizationOfIntangibleAssets", "Amortization of intangibles", **{KEY: 63.0}),
            _row("AmortizationOfDeferredCharges", "Amortization of deferred charges", **{KEY: 9.0}),
        ]
    )
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 367.0
    assert resolution.how == "sourced_map"
    assert resolution.concept == f"{G}Depreciation + {G}AmortizationOfIntangibleAssets"
    assert "da_sourced_map" in resolution.flags
    assert "da_sum_refused_other_lines" in resolution.flags


def test_the_da_exact_sum_needs_both_parts() -> None:
    """(d) One part alone is not D&A (N14/D7): the item stays empty."""
    stmts = _payload(cash_flow=[_row("Depreciation", "Depreciation", **{KEY: 304.0})])
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value is None
    assert "depreciation_only_not_da" in resolution.flags


def test_the_pretax_exact_sum_is_read_when_both_parts_are_filed() -> None:
    """(d) Pre-tax income = the domestic + the foreign pre-tax income of a filer that splits them."""
    stmts = _payload(
        income=[
            _row(
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
                "Pre-tax income (domestic)",
                **{KEY: 700.0},
            ),
            _row(
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
                "Pre-tax income (foreign)",
                **{KEY: 300.0},
            ),
        ]
    )
    resolution = stmts.resolve("pretax_income", KEY)
    assert resolution.value == 1_000.0
    assert resolution.how == "sourced_map"


def test_the_pretax_exact_sum_needs_both_parts() -> None:
    """(d) One part alone is not the pre-tax income: the item stays empty."""
    stmts = _payload(
        income=[
            _row(
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
                "Pre-tax income (domestic)",
                **{KEY: 700.0},
            )
        ]
    )
    assert stmts.resolve("pretax_income", KEY).value is None


# -- the addendum (T-159a, reviewer B, 2026-10-22): total debt and D&A stored too low ---------------


def test_rule_1_the_current_maturities_filed_as_the_debt_and_lease_element_are_read() -> None:
    """Rule 1: after ``LongTermDebtCurrent`` and ``OtherLongTermDebtCurrent``, the current
    maturities a filer files as ``LongTermDebtAndCapitalLeaseObligationsCurrent`` are read, flagged
    ``includes_finance_leases`` (the flag its non-current partner already carries)."""
    stmts = _payload(
        balance=[
            _row(
                "LongTermDebtAndCapitalLeaseObligationsCurrent",
                "Current maturities of long-term debt",
                **{INSTANT: 15.0},
            ),
            _row("LongTermDebtAndCapitalLeaseObligations", "Long-term debt", **{INSTANT: 13_862.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 15.0
    assert resolution.concept == G + "LongTermDebtAndCapitalLeaseObligationsCurrent"
    assert "includes_finance_leases" in resolution.flags


def test_rule_1_n4_is_unchanged_a_long_term_line_with_the_current_maturities_excludes_them() -> (
    None
):
    """Rule 1: "N4 unchanged: not added when the long-term line already includes current
    maturities"."""
    stmts = _payload(
        balance=[
            _row(
                "LongTermDebtAndCapitalLeaseObligationsCurrent",
                "Current maturities of long-term debt",
                **{INSTANT: 15.0},
            ),
            _row("LongTermDebt", "Long-term debt", **{INSTANT: 100.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value is None
    assert "includes_finance_leases" not in resolution.flags


def test_rule_2_a_term_filed_only_as_its_components_is_their_sum() -> None:
    """Rule 2: when no concept the term reads today is filed, the term is the sum of its filed
    calculation children (each family once), flagged ``debt_components_sum``."""
    stmts = _payload(
        balance=[
            _row("SecuredDebtCurrent", "Current secured debt", **{INSTANT: 40.0}),
            _row("UnsecuredDebtCurrent", "Current unsecured debt", **{INSTANT: 60.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 100.0
    assert "debt_components_sum" in resolution.flags
    assert G + "SecuredDebtCurrent" in str(resolution.concept)
    assert G + "UnsecuredDebtCurrent" in str(resolution.concept)


def test_rule_2_the_briefs_short_term_components_land_in_the_tree_they_belong_to() -> None:
    """Rule 2: "Every component goes into the term where the tree puts it": the other short-term
    borrowings go to the borrowings term, the convertible debt and the lines of credit to the
    current maturities."""
    stmts = _payload(
        balance=[
            _row("OtherShortTermBorrowings", "Other short-term borrowings", **{INSTANT: 608.0}),
            _row("LinesOfCreditCurrent", "Revolving credit facility", **{INSTANT: 25.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 633.0
    assert G + "OtherShortTermBorrowings" in str(resolution.concept)
    assert G + "LinesOfCreditCurrent" in str(resolution.concept)


def test_rule_3_the_siblings_of_a_component_already_read_are_added() -> None:
    """Rule 3: when the borrowings term is read from ``CommercialPaper`` (a child of
    ``ShortTermBorrowings``) and ``ShortTermBorrowings`` is not filed, the other filed children are
    added (NEE: the commercial paper 1,955M plus the other short-term debt 608M)."""
    stmts = _payload(
        balance=[
            _row("CommercialPaper", "Commercial paper", **{INSTANT: 1_955.0}),
            _row("OtherShortTermBorrowings", "Other short-term debt", **{INSTANT: 608.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 2_563.0
    assert "debt_components_sum" in resolution.flags


def test_a_child_is_never_added_beside_its_parent() -> None:
    """Rules 2 and 3: "a child is never added beside its parent". The short-term borrowings that
    are filed are inside the filed ``ShortTermBorrowings``: the children are not added beside it."""
    stmts = _payload(
        balance=[
            _row("ShortTermBorrowings", "Short-term debt", **{INSTANT: 135.0}),
            _row("OtherShortTermBorrowings", "Other short-term borrowings", **{INSTANT: 608.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 135.0
    assert resolution.concept == G + "ShortTermBorrowings"
    assert "debt_components_sum" not in resolution.flags


def test_a_notes_payable_line_is_never_counted_twice() -> None:
    """Rules 2 and 3: a child is never read beside its parent. The notes payable (100) are inside
    the filed "notes and loans payable" (250), so the total is the parent alone."""
    stmts = _payload(
        balance=[
            _row("NotesPayableCurrent", "Notes payable", **{INSTANT: 100.0}),
            _row("NotesAndLoansPayableCurrent", "Notes and loans payable", **{INSTANT: 250.0}),
        ]
    )
    resolution = stmts.resolve("short_term_debt", KEY)
    assert resolution.value == 250.0
    assert G + "NotesPayableCurrent" not in str(resolution.concept)


def test_rule_4_the_da_of_two_face_lines_is_their_sum() -> None:
    """Rule 4: when the D&A resolves to the filed aggregate and ``AmortizationOfIntangibleAssets``
    is a separate row of the served cash-flow statement in the same column, the D&A is their sum
    (BEN: 125,300,000 + 406,500,000 = 531,800,000), flagged ``da_plus_intangibles_row``."""
    stmts = _payload(
        cash_flow=[
            _row(
                "DepreciationDepletionAndAmortization",
                "Depreciation and other amortization",
                **{KEY: 125_300_000.0},
            ),
            _row(
                "AmortizationOfIntangibleAssets",
                "Amortization of intangible assets",
                **{KEY: 406_500_000.0},
            ),
        ]
    )
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 531_800_000.0
    assert resolution.how == "sourced_map"
    assert resolution.concept == (
        f"{G}DepreciationDepletionAndAmortization + {G}AmortizationOfIntangibleAssets"
    )
    assert "da_plus_intangibles_row" in resolution.flags


def test_an_of_which_da_row_is_not_added() -> None:
    """Rule 4: a row that is a child of the D&A row -- an "of which" line, already inside it -- is
    not added."""
    da_row = _row(
        "DepreciationDepletionAndAmortization", "Depreciation and amortization", **{KEY: 500.0}
    )
    da_row["parent_concept"] = G + "NetCashProvidedByUsedInOperatingActivities"
    of_which = _row(
        "AmortizationOfIntangibleAssets", "Amortization of intangible assets", **{KEY: 200.0}
    )
    of_which["parent_concept"] = G + "DepreciationDepletionAndAmortization"
    stmts = _payload(cash_flow=[da_row, of_which])
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 500.0
    assert resolution.concept == G + "DepreciationDepletionAndAmortization"
    assert "da_plus_intangibles_row" not in resolution.flags


def test_a_dimensional_of_which_row_is_not_added() -> None:
    """Rule 4: an "of which" line is a dimensional member of the line it qualifies: the resolver
    reads no dimensional row, so it is never added."""
    da_row = _row(
        "DepreciationDepletionAndAmortization", "Depreciation and amortization", **{KEY: 500.0}
    )
    of_which = {
        **_row(
            "AmortizationOfIntangibleAssets", "Amortization of intangible assets", **{KEY: 200.0}
        ),
        "dimension": True,
    }
    stmts = _payload(cash_flow=[da_row, of_which])
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 500.0
    assert "da_plus_intangibles_row" not in resolution.flags


def test_rule_4_does_not_apply_when_the_income_statement_shows_the_da_as_the_full_expense() -> None:
    """Rule 4's HLT exception (reviewer B, 2026-10-22): the income statement of the same column
    carries the cash-flow D&A concept at the same value and no ``AmortizationOfIntangibleAssets``
    line -- the D&A row is then the whole D&A expense and the other row lies outside it (HLT's
    "Amortization of contract acquisition costs"), so adding it would overstate the D&A."""
    stmts = _payload(
        income=[
            _row(
                "DepreciationAndAmortization",
                "Depreciation and amortization expenses",
                **{KEY: 99.0},
            ),
        ],
        cash_flow=[
            _row(
                "DepreciationAndAmortization",
                "Depreciation and amortization expenses",
                **{KEY: 99.0},
            ),
            _row(
                "AmortizationOfIntangibleAssets",
                "Amortization of contract acquisition costs",
                **{KEY: 32.0},
            ),
        ],
    )
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 99.0
    assert resolution.concept == G + "DepreciationAndAmortization"
    assert "da_plus_intangibles_row" not in resolution.flags


def test_rule_4_applies_when_the_income_statement_carries_both_lines() -> None:
    """Rule 4: CME's shape -- the income statement carries the intangible amortization as its own
    line, so the cash-flow D&A row is not the whole expense and the two are summed."""
    stmts = _payload(
        income=[
            _row("DepreciationAndAmortization", "Depreciation and amortization", **{KEY: 107.5}),
            _row(
                "AmortizationOfIntangibleAssets",
                "Amortization of purchased intangibles",
                **{KEY: 223.4},
            ),
        ],
        cash_flow=[
            _row("DepreciationAndAmortization", "Depreciation and amortization", **{KEY: 107.5}),
            _row(
                "AmortizationOfIntangibleAssets",
                "Amortization of purchased intangibles",
                **{KEY: 223.4},
            ),
        ],
    )
    resolution = stmts.resolve("depreciation_amortization", KEY)
    assert resolution.value == 330.9
    assert resolution.how == "sourced_map"
    assert "da_plus_intangibles_row" in resolution.flags


def test_a_long_term_debt_component_is_never_added_beside_its_parent() -> None:
    """T-159a: the long-term debt components are read widest first, so a child is never added
    beside its parent: EA's filing tags its senior notes under both ``SeniorNotes`` (the total) and
    ``SeniorLongTermNotes`` (the noncurrent portion) -- the total is the value."""
    stmts = _payload(
        balance=[
            _row("SeniorNotes", "Senior notes, net", **{INSTANT: 1_882.0}),
            _row("SeniorLongTermNotes", "Senior notes, net", **{INSTANT: 1_882.0}),
        ]
    )
    resolution = stmts.resolve("long_term_debt", KEY)
    assert resolution.value == 1_882.0
    assert resolution.concept == G + "SeniorNotes"


# -- the rejected list -------------------------------------------------------------------------------


@pytest.mark.parametrize("concept", REJECTED)
def test_a_rejected_concept_never_resolves(concept: str) -> None:
    """The brief's rejected list: none of these concepts may ever resolve, for any item."""
    for statement, columns in (
        ("income", {KEY: 100.0}),
        ("balance", {INSTANT: 100.0}),
    ):
        row = _row(concept, concept, **columns)
        stmts = _payload(income=[row]) if statement == "income" else _payload(balance=[row])
        for item in REGISTRY:
            resolution = stmts.resolve(item, KEY)
            assert resolution.concept != G + concept, (item, concept, statement)
            assert resolution.value != 100.0 or item not in ("short_term_investments",)
