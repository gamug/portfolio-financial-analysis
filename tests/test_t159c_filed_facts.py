"""T-159c: the filing's own ``filed_facts`` fill an item the statements leave empty.

Statements first, always (ID-02a): the fallback is reached only when the statements resolve the
item to nothing. The read column's own period decides (an instant; a quarter of 80-119 days; a year
of >= 351 days; a year-to-date column of 175-285 days; an untagged duration column of <= 371 days),
one distinct value is used, and two different values for the same concept and period leave the item
empty. The T-159a substitutes, sole-family and exact-sum rules apply to the filed facts too.
"""

from __future__ import annotations

from typing import Any

from fundamental_agent.statements import Statements

G = "us-gaap_"
FY = "2024-12-31 (FY)"
Q3 = "2024-09-30 (Q3)"
YTD = "2024-09-30 (YTD)"
INSTANT = "2024-12-31"


def _fact(
    concept: str,
    value: float,
    *,
    start: str | None = None,
    end: str,
    unit: str = "USD",
) -> dict[str, Any]:
    return {
        "concept": f"us-gaap:{concept}",
        "value": value,
        "unit": unit,
        "period_start": start,
        "period_end": end,
        "decimals": "-6",
    }


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


def _payload(
    *,
    income: list[dict[str, Any]] | None = None,
    balance: list[dict[str, Any]] | None = None,
    cash_flow: list[dict[str, Any]] | None = None,
    filed_facts: list[dict[str, Any]] | None = None,
    filed_facts_error: str | None = None,
) -> Statements:
    return Statements.from_payload(
        {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash_flow or [],
            "filed_facts": filed_facts or [],
            "filed_facts_error": filed_facts_error,
        }
    )


def test_an_instant_filed_fact_fills_a_missing_balance_item() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[
            _fact("LongTermDebt", 50.0, end=INSTANT),
            _fact("LongTermDebt", 50.0, end=INSTANT),  # the same fact filed twice
        ],
    )
    resolution = stmts.resolve("long_term_debt", FY)
    assert resolution.value == 50.0
    assert resolution.concept == G + "LongTermDebt"
    assert resolution.how == "filed_fact"
    assert "filed_fact" in resolution.flags


def test_a_quarter_filed_fact_is_read_and_a_ytd_fact_is_never_the_quarter() -> None:
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{Q3: 100.0, YTD: 300.0})],
        filed_facts=[
            _fact("InterestExpense", 10.0, start="2024-07-01", end="2024-09-30"),  # 92 days
            _fact("InterestExpense", 30.0, start="2024-01-01", end="2024-09-30"),  # 274 days
        ],
    )
    quarter = stmts.resolve("interest_expense", Q3)
    assert quarter.value == 10.0
    assert quarter.how == "filed_fact"
    assert stmts.resolve("interest_expense", YTD).value == 30.0


def test_a_ytd_only_fact_does_not_fill_the_quarter() -> None:
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{Q3: 100.0, YTD: 300.0})],
        filed_facts=[
            _fact("InterestExpense", 30.0, start="2024-01-01", end="2024-09-30"),
        ],
    )
    resolution = stmts.resolve("interest_expense", Q3)
    assert resolution.value is None


def test_a_10k_flow_reads_a_year_and_not_a_quarter() -> None:
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{FY: 1_000.0})],
        filed_facts=[
            _fact("InterestExpense", 40.0, start="2024-01-01", end="2024-12-31"),  # 365 days
            _fact("InterestExpense", 10.0, start="2024-10-01", end="2024-12-31"),  # 91 days
        ],
    )
    resolution = stmts.resolve("interest_expense", FY)
    assert resolution.value == 40.0
    assert resolution.how == "filed_fact"


def test_an_untagged_duration_column_uses_a_fact_up_to_371_days() -> None:
    untagged = "2024-09-28"
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{untagged: 100.0})],
        filed_facts=[
            _fact("InterestExpense", 10.0, start="2024-06-08", end=untagged),  # 112 days
            _fact("InterestExpense", 99.0, start="2023-08-01", end=untagged),  # 424 days
        ],
    )
    resolution = stmts.resolve("interest_expense", untagged)
    assert resolution.value == 10.0


def test_a_bare_date_on_a_flow_statement_is_an_untagged_duration_not_an_instant() -> None:
    """T-159c/T-159b F0: the 16/24-week filers' bare-date income and cash-flow columns are
    duration columns; the balance sheet's bare date stays an instant."""
    untagged = "2024-09-28"
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{untagged: 100.0})],
        balance=[_row("Assets", "Assets", **{untagged: 200.0})],
    )
    assert [p.key for p in stmts.untagged_periods()] == [untagged]
    assert [p.key for p in stmts.instant_periods()] == [untagged]
    assert stmts.instant_periods()[0].tag == "INSTANT"
    assert stmts.untagged_periods()[0].tag == ""


def test_two_different_values_for_one_concept_and_period_are_ambiguous() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[
            _fact("LongTermDebt", 50.0, end=INSTANT),
            _fact("LongTermDebt", 60.0, end=INSTANT),
        ],
    )
    resolution = stmts.resolve("long_term_debt", FY)
    assert resolution.value is None
    assert "filed_fact_ambiguous" in resolution.flags


def test_the_statements_value_wins_over_a_filed_fact() -> None:
    stmts = _payload(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 100.0}),
            _row("LongTermDebt", "Long-term debt", **{INSTANT: 40.0}),
        ],
        filed_facts=[_fact("LongTermDebt", 50.0, end=INSTANT)],
    )
    resolution = stmts.resolve("long_term_debt", FY)
    assert resolution.value == 40.0
    assert resolution.how == "concept"


def test_a_statement_zero_is_not_replaced() -> None:
    stmts = _payload(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 100.0}),
            _row("LongTermDebt", "Long-term debt", **{INSTANT: 0.0}),
        ],
        filed_facts=[_fact("LongTermDebt", 50.0, end=INSTANT)],
    )
    resolution = stmts.resolve("long_term_debt", FY)
    assert resolution.value == 0.0
    assert resolution.how == "concept"


def test_a_filed_facts_read_failure_leaves_the_item_empty_and_flags_it() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[],
        filed_facts_error="KeyError: 'facts'",
    )
    resolution = stmts.resolve("long_term_debt", FY)
    assert resolution.value is None
    assert "filed_facts_unavailable" in resolution.flags


def test_a_substitute_total_is_read_from_the_filed_facts() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[_fact("AccountsReceivableNet", 70.0, end=INSTANT)],
    )
    resolution = stmts.resolve("receivables", FY)
    assert resolution.value == 70.0
    assert resolution.concept == G + "AccountsReceivableNet"
    assert resolution.how == "filed_fact"


def test_a_sole_family_member_is_read_from_the_filed_facts() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[_fact("InventoryFinishedGoods", 30.0, end=INSTANT)],
    )
    assert stmts.resolve("inventory", FY).value == 30.0


def test_two_inventory_families_stay_empty() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[
            _fact("EnergyRelatedInventory", 20.0, end=INSTANT),
            _fact("InventoryPartsAndComponentsNetOfReserves", 4.0, end=INSTANT),
        ],
    )
    assert stmts.resolve("inventory", FY).value is None


def test_the_disjoint_short_term_investment_classes_are_summed() -> None:
    stmts = _payload(
        balance=[_row("Assets", "Assets", **{INSTANT: 100.0})],
        filed_facts=[
            _fact("TradingSecurities", 10.0, end=INSTANT),
            _fact("HeldToMaturitySecuritiesCurrent", 5.0, end=INSTANT),
        ],
    )
    resolution = stmts.resolve("short_term_investments", FY)
    assert resolution.value == 15.0
    assert resolution.how == "filed_fact"


def test_the_exact_da_sum_reads_only_when_both_parts_are_filed() -> None:
    both = _payload(
        cash_flow=[_row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 100.0})],
        filed_facts=[
            _fact("Depreciation", 10.0, start="2024-01-01", end="2024-12-31"),
            _fact("AmortizationOfIntangibleAssets", 5.0, start="2024-01-01", end="2024-12-31"),
        ],
    )
    resolution = both.resolve("depreciation_amortization", FY)
    assert resolution.value == 15.0
    assert "da_sourced_map" in resolution.flags
    one = _payload(
        cash_flow=[_row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 100.0})],
        filed_facts=[_fact("Depreciation", 10.0, start="2024-01-01", end="2024-12-31")],
    )
    assert one.resolve("depreciation_amortization", FY).value is None


def test_a_negative_filed_fact_of_a_nonnegative_concept_is_rejected() -> None:
    stmts = _payload(
        income=[_row("Revenues", "Revenue", **{FY: 1_000.0})],
        filed_facts=[_fact("InterestExpenseDebt", -5.0, start="2024-01-01", end="2024-12-31")],
    )
    resolution = stmts.resolve("interest_expense", FY)
    assert resolution.value is None
    assert "wrong_sign_rejected" in resolution.flags


def test_n4_current_maturities_are_not_added_to_long_term_debt() -> None:
    stmts = _payload(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 100.0}),
            _row("LongTermDebt", "Long-term debt", **{INSTANT: 100.0}),
        ],
        filed_facts=[_fact("LongTermDebtCurrent", 20.0, end=INSTANT)],
    )
    assert stmts.resolve("long_term_debt", FY).value == 100.0
    resolution = stmts.resolve("short_term_debt", FY)
    assert resolution.value is None
    assert "current_maturities_in_long_term" in resolution.flags


def test_apa_profit_loss_with_nci_parts_uses_the_available_to_common_fact() -> None:
    """F4: the ProfitLoss refusal stands (only NCI parts are filed); T-159a's "available to
    common" substitute is the same quantity when no preferred dividend is filed."""
    stmts = _payload(
        income=[
            _row("ProfitLoss", "Net income including NCI", **{FY: 4_082.0}),
            _row(
                "NetIncomeLossAttributableToRedeemableNoncontrollingInterest",
                "NCI, preferred units",
                **{FY: -70.0},
            ),
            _row(
                "NetIncomeLossAttributableToNonredeemableNoncontrollingInterest",
                "NCI, Egypt",
                **{FY: 464.0},
            ),
            _row(
                "NetIncomeLossAvailableToCommonStockholdersBasic",
                "Net income attributable to common stock",
                **{FY: 3_674.0},
            ),
        ],
        filed_facts=[
            _fact(
                "NetIncomeLossAvailableToCommonStockholdersBasic",
                3_674.0,
                start="2024-01-01",
                end="2024-12-31",
            )
        ],
    )
    resolution = stmts.resolve("net_income", FY)
    assert resolution.value == 3_674.0
    assert resolution.concept == G + "NetIncomeLossAvailableToCommonStockholdersBasic"
    assert "net_income_to_common" in resolution.flags
    assert "profit_loss_nci_income_not_total" in resolution.flags


def test_total_debt_uses_a_filed_fact_long_term_debt() -> None:
    """T-159c: total_debt reads the same fallback (it calls the long-term resolver directly)."""
    stmts = _payload(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 100.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 50.0}),
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash", **{INSTANT: 10.0}),
        ],
        filed_facts=[_fact("LongTermDebt", 50.0, end=INSTANT)],
    )
    resolution = stmts.total_debt(FY)
    assert resolution.value == 50.0
    assert "partial_map" in resolution.flags


def test_a_short_term_fact_alone_is_not_total_debt() -> None:
    stmts = _payload(
        balance=[
            _row("Assets", "Assets", **{INSTANT: 100.0}),
            _row("StockholdersEquity", "Equity", **{INSTANT: 50.0}),
            _row("CashAndCashEquivalentsAtCarryingValue", "Cash", **{INSTANT: 10.0}),
        ],
        filed_facts=[_fact("CommercialPaper", 10.0, end=INSTANT)],
    )
    short = stmts.resolve("short_term_debt", FY)
    assert short.value == 10.0
    assert short.how == "filed_fact"
    assert short.concept == G + "CommercialPaper"
    resolution = stmts.total_debt(FY)
    assert resolution.value is None
    assert "short_term_debt_only" in resolution.flags


def test_the_net_proceeds_element_never_fills_capex() -> None:
    stmts = _payload(
        cash_flow=[_row("NetCashProvidedByUsedInOperatingActivities", "OCF", **{FY: 100.0})],
        filed_facts=[
            _fact(
                "PaymentsForProceedsFromProductiveAssets",
                800_000.0,
                start="2024-01-01",
                end="2024-12-31",
            )
        ],
    )
    assert stmts.resolve("capital_expenditure", FY).value is None
