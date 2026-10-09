"""``scripts/sec_xcheck/resolver_measures.py`` and ``capex_measures.py``: one predicate per finding of
``code_comparison_v1.md`` §2 (N3 to N7, N13, N14, D2 to D4) and MET-08, each on a hand-built record (T-147)."""

from __future__ import annotations

from sec_xcheck import capex_measures as cx
from sec_xcheck import resolver_measures as rm
from sec_xcheck.features import extract
from sec_xcheck.records import count
from sec_xcheck_support import rec

from fundamental_agent.statements import Statements

G = "us-gaap_"


def test_n3_continuing_operations_wins_when_the_total_differs() -> None:
    ocf = {
        G + "NetCashProvidedByUsedInOperatingActivities": 100.0,
        G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations": 90.0,
    }
    assert rm.ocf_continuing_wins(rec(items={"operating_cash_flow": 90.0}, ocf=ocf))
    assert not rm.ocf_continuing_wins(
        rec(items={"operating_cash_flow": 100.0}, ocf=ocf)
    )  # the total won
    assert rm.ocf_both_differ(rec(ocf=ocf))


def test_n4_long_term_debt_that_includes_current_maturities_plus_the_current_line() -> None:
    both = rec(
        items={
            "long_term_debt": (500.0, G + "LongTermDebt"),
            "short_term_debt": (50.0, G + "LongTermDebtCurrent"),
        }
    )
    assert rm.debt_double_counts(both)
    assert rm.double_counted_amount(both) == 50.0
    noncurrent = rec(
        items={
            "long_term_debt": (500.0, G + "LongTermDebtNoncurrent"),
            "short_term_debt": (50.0, G + "LongTermDebtCurrent"),
        }
    )
    assert not rm.debt_double_counts(noncurrent)


def test_n5_cash_and_short_term_investments_counted_twice() -> None:
    assert rm.cash_double_counts(
        rec(
            items={
                "cash": (100.0, G + "CashCashEquivalentsAndShortTermInvestments"),
                "short_term_investments": 20.0,
            }
        )
    )
    assert not rm.cash_double_counts(
        rec(
            items={
                "cash": (100.0, G + "CashAndCashEquivalentsAtCarryingValue"),
                "short_term_investments": 20.0,
            }
        )
    )


def test_n6_a_balance_sheet_read_from_another_date() -> None:
    assert rm.instant_not_exact(rec(instant="2022-12-31", instant_exact=False))
    assert not rm.instant_not_exact(rec())


def test_n7_missing_debt_terms_and_the_zero_filled_enterprise_value() -> None:
    assert rm.lt_only(rec(items={"long_term_debt": 5.0}))
    assert rm.st_only(rec(items={"short_term_debt": 5.0}))
    assert rm.debt_absent(rec())
    assert rm.ev_zero_filled(rec(items={"long_term_debt": 5.0}))  # cash absent
    assert not rm.ev_zero_filled(rec(items={"long_term_debt": 5.0, "cash": 1.0}))


def test_d6d_short_term_debt_follows_the_disjoint_lines_with_debt_current_last() -> None:
    assert (
        rm.proposed_short_term_debt(
            {G + "LongTermDebtCurrent": 10.0, G + "ShortTermBorrowings": 5.0}
        )
        == 15.0
    )
    assert (
        rm.proposed_short_term_debt({G + "LongTermDebtCurrent": 10.0, G + "CommercialPaper": 4.0})
        == 14.0
    )  # no borrowings line
    assert (
        rm.proposed_short_term_debt({G + "ShortTermBorrowings": 5.0, G + "CommercialPaper": 4.0})
        == 5.0
    )  # CP is inside it
    assert (
        rm.proposed_short_term_debt({G + "DebtCurrent": 99.0}) == 99.0
    )  # includes leases: last, flagged
    assert rm.proposed_short_term_debt({}) is None
    today = rec(
        items={"short_term_debt": 10.0},
        st={G + "LongTermDebtCurrent": 10.0, G + "ShortTermBorrowings": 5.0},
    )
    assert rm.st_debt_differs(today) and rm.st_debt_gap(today) == 5.0


def test_d2_precedence_equity_and_net_income() -> None:
    assert rm.equity_not_narrowest(
        rec(items={"equity": 700.0}, equity={G + "StockholdersEquity": 500.0})
    )
    assert not rm.equity_not_narrowest(
        rec(items={"equity": 500.0}, equity={G + "StockholdersEquity": 500.0})
    )
    assert rm.ni_profitloss_over_parent(
        rec(items={"net_income": (110.0, G + "ProfitLoss")}, ni={G + "NetIncomeLoss": 100.0})
    )
    assert rm.ni_available_to_common(
        rec(items={"net_income": (1.0, G + "NetIncomeLossAvailableToCommonStockholdersBasic")})
    )


def test_d3_and_d6c_income_tax() -> None:
    assert rm.tax_component_only(
        rec(items={"income_tax": (5.0, G + "DeferredIncomeTaxExpenseBenefit")})
    )
    comp = {G + "CurrentIncomeTaxExpenseBenefit": 3.0, G + "DeferredIncomeTaxExpenseBenefit": 2.0}
    assert rm.tax_current_deferred_without_total(rec(tax=comp))
    assert not rm.tax_current_deferred_without_total(
        rec(tax={**comp, G + "IncomeTaxExpenseBenefit": 5.0})
    )


def test_id11_signs_and_the_findings_wrapper() -> None:
    recs = [
        rec(items={"capital_expenditure": -10.0}, ticker="A"),
        rec(items={"capital_expenditure": 10.0}, ticker="B"),
    ]
    (f,) = [x for x in rm.m_signs(recs) if "capital_expenditure" in x.key]
    assert (f.count, f.total, f.companies) == (1, 2, 1)
    assert f.examples == [("A", "acc-A", "10-K FY2023")]


def test_count_counts_within_a_population() -> None:
    recs = [
        rec(ticker="A", sector="Utilities"),
        rec(ticker="B"),
        rec(ticker="C", sector="Utilities"),
    ]
    f = count(
        "k",
        ["r"],
        "t",
        recs,
        lambda r: r.ticker != "C",
        population=lambda r: r.sector == "Utilities",
    )
    assert (f.count, f.total) == (1, 2)


# -- MET-08 -------------------------------------------------------------------------------------
def test_met08_generic_company_uses_ppe_then_productive_assets_flagged() -> None:
    assert cx.target_capex({cx.PPE: -100.0, cx.PROD: -150.0}) == (100.0, [])
    assert cx.target_capex({cx.PROD: -150.0}) == (150.0, ["productive_assets_fallback"])
    assert cx.target_capex({}) == (None, [])


def test_met08_oil_and_gas_tie_break_adds_separate_face_lines() -> None:
    value, flags = cx.target_capex({cx.OG_ED: -2870.0, cx.OG_EQUIP: -400.0})
    assert value == 3270.0 and flags == ["tie_break_c_added"]
    assert cx.target_capex({cx.OG_PPE: -500.0}) == (500.0, ["includes_acquisitions_contrary_to_a"])
    # (a): mineral-interest purchases and business acquisitions are never capex
    assert cx.target_capex({cx.OG_PROP: -8920.0, cx.BUSINESSES: -100.0}) == (None, [])


def test_met08_reit_adds_acquisitions_and_never_double_counts_capital_improvements() -> None:
    value, flags = cx.target_capex(
        {cx.PPE: -10.0, cx.REAL_ESTATE[0]: -300.0, cx.CAPIMP: -50.0}, reit=True
    )
    assert value == 310.0 and "capital_improvements_overlap_not_added" in flags
    assert cx.target_capex({cx.CAPIMP: -50.0}, reit=True)[0] == 50.0


def test_cause_names_why_the_resolver_and_the_rule_differ() -> None:
    fang = rec(
        items={"capital_expenditure": -2870.0}, capex={cx.OG_ED: -2870.0, cx.OG_EQUIP: -400.0}
    )
    assert cx.cause(fang) == "tie_break_c_added" and cx.amount_left_out(fang) == 400.0
    agree = rec(items={"capital_expenditure": -100.0}, capex={cx.PPE: -100.0})
    assert cx.cause(agree) is None
    reit = rec(sector="Real Estate", sub_industry="Retail REITs", capex={cx.REAL_ESTATE[0]: -300.0})
    assert cx.is_reit(reit) and cx.cause(reit) == "resolver_empty_reit_components"


def test_features_extract_runs_on_a_real_filing(aapl_10k: Statements) -> None:
    period = aapl_10k.latest_fy()
    assert period is not None
    record = extract(aapl_10k, period.key)
    assert record["items"]["revenue"]["value"] == aapl_10k.get("revenue", period.key)
    assert record["items"]["equity"]["concept"] == "us-gaap_StockholdersEquity"
    assert record["metrics"]["net_margin"]["group"] == "profitability"
    assert record["instant_exact"] is True
