"""T-148 part 1 (d): debt, cash and interest expense -- MET-07, ID-02b, D-01, D-06(d), MET-00,
MET-09, ID-05 and the N4-N6 / N23 defects, on the real gateway captures in ``tests/fixtures``.

Captured for this task (2026-10-11, the gateway's ``/financials`` with the filing's accession):
a REIT (UDR), Consolidated Edison, a company with no debt (Align) and Philip Morris, shapes no
earlier capture in the repository has.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from fundamental_agent.metrics import leverage, liquidity, roic, valuation
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import Statements
from kg_schema.market_cap import MarketCap

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())["data"]
    return data


def _stmts(name: str) -> Statements:
    return Statements.from_payload(_payload(name))


def _key(stmts: Statements, *, quarter: bool = False) -> str:
    return (stmts.quarter_periods() if quarter else stmts.fy_periods())[-1].key


def _drop(payload: dict[str, Any], statement: str, *concepts: str) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out[statement] = [r for r in out[statement] if r["concept"] not in concepts]
    return out


def _add(payload: dict[str, Any], statement: str, template: dict[str, Any], **changes: Any) -> None:
    """A copy of *template* (a real row) under another concept / with other values."""
    payload[statement].append({**template, **changes})


def _row(payload: dict[str, Any], statement: str, concept: str) -> dict[str, Any]:
    return next(
        r
        for r in payload[statement]
        if r["concept"] == concept and not r.get("dimension") and not r.get("abstract")
    )


G = "us-gaap_"
STZ = "financials_STZ_10-Q_2023_acc-0000016918-23-000190.json"
AAPL = "financials_AAPL_10-K_2023.json"
DUKE = "financials_DUK_10-K_2025_acc-0001326160-25-000072.json"
XEL = "financials_XEL_10-K_2026_acc-0000072903-26-000009.json"
NVDA = "financials_NVDA_10-K_2024.json"
EXE = "financials_EXE_10-K_2022_acc-0000895126-22-000029.json"
WAT = "financials_WAT_10-Q_2023_acc-0001193125-23-271909.json"
JNJ = "financials_JNJ_10-K_2024_acc-0000200406-24-000013.json"
MSFT = "financials_MSFT_10-Q_2024.json"
UDR = "financials_UDR_10-K_2026_acc-0000074208-26-000013.json"
ED = "financials_ED_10-K_2026_acc-0001047862-26-000031.json"
ALGN = "financials_ALGN_10-K_2023_acc-0001097149-23-000013.json"
PM = "financials_PM_10-K_2026_acc-0001628280-26-005939.json"
GM = "financials_GM_10-K_2026_acc-0001467858-26-000013.json"


# -- short-term debt: D-06(d), MET-07 -----------------------------------------------------------


def test_short_term_debt_is_current_maturities_plus_short_term_borrowings() -> None:
    """Constellation Brands' 10-Q files ``LongTermDebtCurrent`` and ``ShortTermBorrowings``, disjoint
    by definition (B6): both are short-term debt. Read as one first match, only the first was."""
    stmts = _stmts(STZ)
    key = _key(stmts, quarter=True)
    payload = _payload(STZ)
    instant = stmts.resolve_column("short_term_debt", key)
    assert instant is not None
    ltdc = _row(payload, "balance_sheet", G + "LongTermDebtCurrent")[instant]
    stb = _row(payload, "balance_sheet", G + "ShortTermBorrowings")[instant]
    resolution = stmts.resolve("short_term_debt", key)
    assert resolution.value == ltdc + stb == 1_025_700_000.0
    assert resolution.how == "map"
    assert resolution.concept == f"{G}LongTermDebtCurrent + {G}ShortTermBorrowings"


def test_commercial_paper_is_short_term_debt_only_without_short_term_borrowings() -> None:
    """Apple files ``LongTermDebtCurrent`` (the term debt due) and ``CommercialPaper``. Commercial
    paper is "short-term borrowings using unsecured obligations": a kind of ``ShortTermBorrowings``,
    so with that filed as well it is not added again."""
    stmts = _stmts(AAPL)
    key = _key(stmts)
    assert stmts.get("short_term_debt", key) == 15_807_000_000.0  # 9,822M term debt + 5,985M paper

    payload = copy.deepcopy(_payload(AAPL))
    paper = _row(payload, "balance_sheet", G + "CommercialPaper")
    _add(payload, "balance_sheet", paper, concept=G + "ShortTermBorrowings")
    both = Statements.from_payload(payload)
    assert both.get("short_term_debt", key) == 15_807_000_000.0  # the paper is not counted twice


def test_debt_current_is_the_last_resort_and_says_it_includes_leases() -> None:
    """NVIDIA files only ``DebtCurrent`` ("debt and lease obligation, classified as current"): used
    because nothing narrower is filed, flagged."""
    stmts = _stmts(NVDA)
    resolution = stmts.resolve("short_term_debt", _key(stmts))
    assert resolution.value == 1_250_000_000.0
    assert resolution.concept == G + "DebtCurrent" and resolution.flags == ("includes_leases",)


def test_debt_current_never_displaces_the_narrower_lines() -> None:
    payload = copy.deepcopy(_payload(STZ))
    template = _row(payload, "balance_sheet", G + "ShortTermBorrowings")
    _add(payload, "balance_sheet", template, concept=G + "DebtCurrent")
    stmts = Statements.from_payload(payload)
    assert stmts.get("short_term_debt", _key(stmts, quarter=True)) == 1_025_700_000.0


# -- N4: current maturities counted twice -------------------------------------------------------


def test_n4_a_long_term_line_that_includes_current_maturities_gets_none_added() -> None:
    """Xcel Energy files ``LongTermDebt`` (31,832M; B6: no classification, so it includes the part
    due within a year) and ``ShortTermBorrowings``. Were it to file ``LongTermDebtCurrent`` too,
    that would be inside the first: 25 production filings (7 companies) added it."""
    stmts = _stmts(XEL)
    key = _key(stmts)
    assert stmts.total_debt(key).value == 31_832_000_000.0 + 1_550_000_000.0

    payload = copy.deepcopy(_payload(XEL))
    template = _row(payload, "balance_sheet", G + "ShortTermBorrowings")
    _add(payload, "balance_sheet", template, concept=G + "LongTermDebtCurrent")
    for column in [c for c in template if c[:2] == "20"]:
        payload["balance_sheet"][-1][column] = 999.0
    with_current = Statements.from_payload(payload)
    total = with_current.total_debt(key)
    assert total.value == 31_832_000_000.0 + 1_550_000_000.0
    assert "current_maturities_in_long_term" in total.flags
    assert G + "LongTermDebtCurrent" not in (total.concept or "")


def test_c2_the_reit_pair_is_the_sourced_long_term_debt_sum() -> None:
    """(**Replaced**: ``test_n23_the_reit_pair_is_not_a_map_and_the_debt_is_a_capture_failure``,
    T-151 part 1 C2.) UDR files no long-term debt line: its balance sheet is "Secured debt, net"
    (961.18M) and "Unsecured debt, net" (4,860.19M). The FASB 2026 ``sfp-dbo`` calculation arc makes
    ``LongTermDebt = SecuredDebt + UnsecuredDebt`` -- the loaded source ID-02b asks for -- so the
    pair is the sourced long-term debt: summed, flagged ``debt_sourced_sum`` and
    ``includes_current_maturities`` (B6 defines each "including the current and noncurrent
    portions"), and no current-maturity term is added to the total (N4)."""
    stmts = _stmts(UDR)
    key = _key(stmts)
    resolution = stmts.resolve("long_term_debt", key)
    assert resolution.value == 5_821_369_000.0
    assert resolution.concept == (f"{G}SecuredDebt + {G}UnsecuredDebt")
    assert resolution.flags == ("debt_sourced_sum", "includes_current_maturities")
    total = stmts.total_debt(key)
    assert total.value == 5_821_369_000.0  # no short-term term is filed, so it is not added
    assert "debt_sourced_sum" in total.flags and "includes_current_maturities" in total.flags
    assert (
        stmts.get("interest_expense", key) == 196_619_000.0
    )  # InterestExpenseDebt (N23) still read


def test_the_combined_debt_and_lease_element_is_used_as_the_only_long_term_line_and_flagged() -> (
    None
):
    """MET-07: Philip Morris files ``LongTermDebtAndCapitalLeaseObligations`` and nothing narrower."""
    stmts = _stmts(PM)
    resolution = stmts.resolve("long_term_debt", _key(stmts))
    assert resolution.value == 45_134_000_000.0
    assert resolution.flags == ("includes_finance_leases",)


def test_the_combined_element_never_outranks_a_narrower_long_term_line() -> None:
    payload = copy.deepcopy(_payload(PM))
    template = _row(payload, "balance_sheet", G + "LongTermDebtAndCapitalLeaseObligations")
    _add(payload, "balance_sheet", template, concept=G + "LongTermDebtNoncurrent")
    for column in [c for c in template if c[:2] == "20"]:
        payload["balance_sheet"][-1][column] = 40_000_000_000.0
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("long_term_debt", _key(stmts))
    assert resolution.value == 40_000_000_000.0 and resolution.flags == ()


def test_a_secured_debt_line_beside_the_companys_own_long_term_line_is_not_read() -> None:
    """MetLife files "Long-term debt" (14.5B, the combined element) and a "Collateral financing
    arrangement" (352M, ``SecuredDebt``): the first is the long-term debt, the second is not read."""
    payload = copy.deepcopy(_payload(PM))
    template = _row(payload, "balance_sheet", G + "LongTermDebtAndCapitalLeaseObligations")
    _add(payload, "balance_sheet", template, concept=G + "SecuredDebt")
    for column in [c for c in template if c[:2] == "20"]:
        payload["balance_sheet"][-1][column] = 352_000_000.0
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("long_term_debt", _key(stmts))
    assert resolution.value == 45_134_000_000.0
    assert resolution.concept == G + "LongTermDebtAndCapitalLeaseObligations"


# -- N23: debt concepts the resolver did not read -------------------------------------------------


def test_n23_other_long_term_debt_current_and_notes_payable_current_are_short_term_debt() -> None:
    """Consolidated Edison files its "long-term debt due within one year" (250M) as
    ``OtherLongTermDebtCurrent`` and its "notes payable" (1,575M) as ``NotesPayableCurrent``:
    neither was read, so its short-term debt was empty."""
    stmts = _stmts(ED)
    resolution = stmts.resolve("short_term_debt", _key(stmts))
    assert resolution.value == 250_000_000.0 + 1_575_000_000.0
    assert resolution.how == "map"
    assert resolution.concept == f"{G}OtherLongTermDebtCurrent + {G}NotesPayableCurrent"


def test_wat_notes_payable_current_is_short_term_debt() -> None:
    stmts = _stmts(WAT)
    assert stmts.get("short_term_debt", _key(stmts, quarter=True)) == 50_000_000.0


def test_other_long_term_debt_current_is_a_kind_of_current_maturity_never_added_to_it() -> None:
    payload = copy.deepcopy(_payload(STZ))
    template = _row(payload, "balance_sheet", G + "LongTermDebtCurrent")
    _add(payload, "balance_sheet", template, concept=G + "OtherLongTermDebtCurrent")
    stmts = Statements.from_payload(payload)
    assert stmts.get("short_term_debt", _key(stmts, quarter=True)) == 1_025_700_000.0


# -- D-01: a map with a term missing ---------------------------------------------------------------


def test_a_partial_map_is_accepted_flagged_when_long_term_debt_and_the_balance_sheet_are_there() -> (
    None
):
    """Expand Energy (FY2021): long-term debt 0 and current maturities 1,929M, no short-term
    borrowings line. The filed terms are the debt; the missing one is flagged."""
    stmts = _stmts(EXE)
    total = stmts.total_debt(_key(stmts))
    assert total.value == 1_929_000_000.0
    assert total.flags == ("partial_map",)


def test_a_partial_map_is_refused_when_the_balance_sheet_did_not_resolve() -> None:
    """D-01 (ii): without the rest of the balance sheet (total assets, equity) a missing term is as
    likely a capture failure. EXE with its equity line gone: no debt, flagged."""
    stmts = Statements.from_payload(
        _drop(
            _payload(EXE),
            "balance_sheet",
            G + "StockholdersEquity",
            G + "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        )
    )
    total = stmts.total_debt(_key(stmts))
    assert total.value is None
    assert set(total.flags) == {"partial_map_rejected", "capture_failure"}


def test_a_missing_cash_empties_the_enterprise_value_but_not_the_debt() -> None:
    """Cash never qualifies (D-01): a cash the registry did not read leaves the enterprise value and
    the ROIC empty (MET-00). The debt itself is accepted on the rest of the balance sheet -- Edison,
    CF Industries and 60 other companies file a cash concept the registry does not read, and their
    debt ratios do not need it."""
    stmts = Statements.from_payload(
        _drop(_payload(EXE), "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    )
    total = stmts.total_debt(_key(stmts))
    assert total.value == 1_929_000_000.0 and total.flags == ("partial_map",)
    assert stmts.get("cash", _key(stmts)) is None


def test_short_term_debt_alone_leaves_debt_empty() -> None:
    """D-01 (i): Waters files notes payable (50M) and no long-term debt concept the registry
    knows, which suggests an unrecognized one -- not a company with 50M of debt. Edison likewise."""
    for name, quarter in ((WAT, True), (ED, False)):
        stmts = _stmts(name)
        total = stmts.total_debt(_key(stmts, quarter=quarter))
        assert total.value is None
        assert total.flags == ("short_term_debt_only", "capture_failure")


def test_every_term_filed_is_a_complete_map_with_no_flag() -> None:
    stmts = _stmts(STZ)
    total = stmts.total_debt(_key(stmts, quarter=True))
    assert total.value == 10_680_800_000.0 + 1_025_700_000.0
    assert total.flags == ()


# -- ID-02b: no debt line at all ---------------------------------------------------------------------


def test_no_debt_line_is_zero_on_a_complete_balance_sheet_without_interest_expense() -> None:
    """Align Technology FY2022: no debt line, no interest-expense concept, and assets, equity and
    cash resolve. Debt is 0, flagged ``no_debt_lines``."""
    stmts = _stmts(ALGN)
    total = stmts.total_debt(_key(stmts))
    assert total.value == 0.0
    assert total.flags == ("no_debt_lines",)


def test_gm_a_debt_the_gateway_emptied_is_a_capture_failure_not_zero() -> None:
    """General Motors' FY2025 10-K files its debt only with dimensions (Automotive, GM Financial): the
    gateway's T-042 empties the non-dimensional row of ``DebtCurrent`` (35.7B) and
    ``LongTermDebtAndCapitalLeaseObligations`` (94.6B) -- see ``corrections`` -- and its interest expense.
    The concepts are on the statement, so "no debt line" is false: debt is missing, not 0. (A first
    reading checked the column's value and made it 0: a 109B debt, found by rebuilding the 503
    companies, as 0.)"""
    payload = _payload(GM)
    stmts = Statements.from_payload(payload)
    key = _key(stmts)
    assert stmts.get("long_term_debt", key) is None and stmts.get("short_term_debt", key) is None
    total = stmts.total_debt(key)
    assert total.value is None and total.flags == ("capture_failure",)
    corrected = {
        c["concept"]
        for c in json.loads((FIXTURES / GM).read_text())["data"]["corrections"]
        if c["statement"] == "balance_sheet"
    }
    assert {G + "DebtCurrent", G + "LongTermDebtAndCapitalLeaseObligations"} <= corrected


def test_a_debt_row_valued_only_in_another_period_is_still_a_debt_line() -> None:
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    row = {**template, "concept": G + "LongTermDebtNoncurrent"}
    for column in [c for c in row if c[:2] == "20"]:
        row[column] = None
    payload["balance_sheet"].append(row)
    stmts = Statements.from_payload(payload)
    assert stmts.total_debt(_key(stmts)).flags == ("capture_failure",)


def test_an_unread_interest_concept_makes_a_missing_debt_a_capture_failure() -> None:
    """ID-02b names ``InterestExpenseDebt`` among the variants: with one filed, a company with no
    debt line has debt the resolver did not find, not none. (UDR's REIT debt is exactly that.)"""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "income_statement", G + "IncomeTaxExpenseBenefit")
    _add(payload, "income_statement", template, concept="x_InterestExpenseOnBorrowings")
    stmts = Statements.from_payload(payload)
    total = stmts.total_debt(_key(stmts))
    assert total.value is None and total.flags == ("capture_failure",)


def test_an_unread_debt_line_makes_a_missing_debt_a_capture_failure() -> None:
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + "ConvertibleNotesPayableNoncurrent")
    stmts = Statements.from_payload(payload)
    assert stmts.total_debt(_key(stmts)).flags == ("capture_failure",)


@pytest.mark.parametrize(
    "concept",
    [
        "MortgageLoansPayable",
    ],
)
def test_a_debt_concept_by_any_of_its_names_outside_the_trees_is_still_a_debt_line(
    concept: str,
) -> None:
    """(**Replaced**: ``test_a_debt_concept_by_any_of_its_names_is_a_debt_line``, whose ``LineOfCredit``
    and ``ConvertibleDebtNoncurrent`` cases are now sourced children, T-151 part 1 C2.) The pattern
    for "a debt line is filed" is case-insensitive and reads the plural names too: a filer whose
    only debt is a line of credit has debt the resolver did not find, not none. This name is no
    child of either sourced tree and no T-159a component, so with no tree child filed the sum is
    not attempted and the debt stays a capture failure."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + concept)
    stmts = Statements.from_payload(payload)
    assert stmts.total_debt(_key(stmts)).flags == ("capture_failure",)


@pytest.mark.parametrize(
    "concept",
    [
        "SeniorNotes",
        "ConvertibleLongTermNotesPayable",
        "LongTermNotesPayable",
        "OtherLongTermDebtNoncurrent",
    ],
)
def test_a_t159a_long_term_debt_component_is_read_by_the_sum(concept: str) -> None:
    """T-159a: the long-term debt components enter the MET-07 / ID-02b sum (never a free
    alternative): a filer whose only long-term debt line is its senior notes has long-term debt.
    The sum is refused when another debt-named line is on the balance sheet (the "only lines"
    condition), so the value is read alone here."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + concept)
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("long_term_debt", _key(stmts))
    assert resolution.value is not None
    assert resolution.concept == G + concept
    assert "debt_sourced_sum" in resolution.flags


@pytest.mark.parametrize(
    "concept",
    [
        "OtherShortTermBorrowings",
        "ConvertibleDebtCurrent",
        "ConvertibleNotesPayableCurrent",
        "LinesOfCreditCurrent",
    ],
)
def test_a_t159a_short_term_debt_line_is_read_by_the_map(concept: str) -> None:
    """T-159a: the short-term debt lines a filer presents instead of a "short-term borrowings"
    line are read by the MET-07 map -- only when the map's own ``borrowings`` term is not filed."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + concept)
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("short_term_debt", _key(stmts))
    assert resolution.value is not None
    assert resolution.concept == G + concept


def test_a_t159a_short_term_debt_line_never_displaces_the_maps_own_borrowings_term() -> None:
    """MET-07: "CommercialPaper only when ShortTermBorrowings is absent". A filer that reports
    short-term borrowings under one of the map's names keeps that value as the term's own line;
    the filed children are added beside it (T-159a rule 3), never in place of it."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + "LinesOfCreditCurrent")
    _add(payload, "balance_sheet", template, concept=G + "ShortTermBorrowings")
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("short_term_debt", _key(stmts))
    assert resolution.concept == f"{G}ShortTermBorrowings + {G}LinesOfCreditCurrent"


@pytest.mark.parametrize("concept", ["LineOfCredit", "ConvertibleDebtNoncurrent"])
def test_a_sourced_debt_child_is_read_by_the_sum_when_no_total_is_filed(concept: str) -> None:
    """(**Replaced**: the ``LineOfCredit`` / ``ConvertibleDebtNoncurrent`` cases of
    ``test_a_debt_concept_by_any_of_its_names_is_a_debt_line``, T-151 part 1 C2.) ``LineOfCredit``
    is a child of ``LongTermDebt`` (sfp-dbo) and ``ConvertibleDebtNoncurrent`` a child of
    ``LongTermDebtNoncurrent`` (sfp-cls): with no total filed, each is the sourced sum's only term,
    and it is read."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + concept)
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("long_term_debt", _key(stmts))
    assert resolution.value is not None and resolution.concept == G + concept
    assert resolution.flags == (
        ("debt_sourced_sum", "includes_current_maturities")
        if concept == "LineOfCredit"
        else ("debt_sourced_sum",)
    )


def test_a_convertible_preferred_stock_is_not_a_debt_line() -> None:
    """Equity named "convertible" is not an obligation: a company with no debt keeps its zero."""
    payload = copy.deepcopy(_payload(ALGN))
    template = _row(payload, "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    _add(payload, "balance_sheet", template, concept=G + "ConvertiblePreferredStockNonredeemable")
    stmts = Statements.from_payload(payload)
    assert stmts.total_debt(_key(stmts)).value == 0.0


def test_an_incomplete_balance_sheet_makes_a_missing_debt_a_capture_failure() -> None:
    stmts = Statements.from_payload(
        _drop(_payload(ALGN), "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    )
    total = stmts.total_debt(_key(stmts))
    assert total.value is None and total.flags == ("capture_failure",)


def test_a_filing_with_interest_expense_and_no_debt_line_is_a_capture_failure() -> None:
    """J&J's FY2023 balance sheet as captured has no debt line, but the income statement files
    interest expense (772M): debt is not 0, it is missing."""
    stmts = _stmts(JNJ)
    total = stmts.total_debt(_key(stmts))
    assert total.value is None and total.flags == ("capture_failure",)


# -- MET-00: EV and ROIC are not filled with 0 ----------------------------------------------------


def _duke_without_debt() -> Statements:
    payload = _drop(
        _payload(DUKE),
        "balance_sheet",
        G + "LongTermDebtNoncurrent",
        G + "LongTermDebtCurrent",
        G + "ShortTermBorrowings",
    )
    return Statements.from_payload(payload)


def _duke_mcap(close: float = 100.0) -> MarketCap:
    shares = 776_461_008.0
    return MarketCap(
        asset_id=1,
        value=shares * close,
        shares=shares,
        share_count=shares,
        share_as_of="2024-12-31",
        share_age_days=0,
        filing_id=1,
        close=close,
        close_date="2024-12-31",
        n_classes=0,
        split_factor=1.0,
    )


def test_enterprise_value_is_empty_when_debt_is_missing_not_market_cap_less_cash() -> None:
    price = ClosePrice(date="2024-12-31", close=100.0)
    mcap = _duke_mcap(100.0)
    complete = {
        r.name: r
        for r in valuation.compute(_stmts(DUKE), "2024-12-31 (FY)", price, market_cap=mcap)
    }
    # MD-35 (D7, T-151 part 2): debt 84,273M + market cap 77,646M + NCI 1,129M - cash 314M
    assert complete["enterprise_value"].value == pytest.approx(
        776_461_008.0 * 100.0 + 84_273_000_000.0 + 1_129_000_000.0 - 314_000_000.0
    )

    broken = {
        r.name: r
        for r in valuation.compute(_duke_without_debt(), "2024-12-31 (FY)", price, market_cap=mcap)
    }
    assert broken["market_capitalization"].value is not None
    assert broken["enterprise_value"].value is None
    assert broken["enterprise_fcf_yield"].value is None
    assert broken["enterprise_value"].inputs["flag_total_debt_capture_failure"] == 1.0


def test_enterprise_value_is_empty_when_cash_is_missing() -> None:
    payload = _drop(_payload(DUKE), "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    price = ClosePrice(date="2024-12-31", close=100.0)
    out = {
        r.name: r
        for r in valuation.compute(Statements.from_payload(payload), "2024-12-31 (FY)", price)
    }
    assert out["enterprise_value"].value is None


def test_roic_is_empty_when_debt_or_cash_is_missing_not_computed_on_equity_alone() -> None:
    full = {r.name: r for r in roic.compute(_stmts(DUKE), "2024-12-31 (FY)")}
    assert full["return_on_invested_capital"].value is not None
    no_debt = {r.name: r for r in roic.compute(_duke_without_debt(), "2024-12-31 (FY)")}
    assert no_debt["return_on_invested_capital"].value is None
    assert no_debt["nopat"].value is not None  # the operating profit is still there
    no_cash = {
        r.name: r
        for r in roic.compute(
            Statements.from_payload(
                _drop(_payload(DUKE), "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
            ),
            "2024-12-31 (FY)",
        )
    }
    assert no_cash["return_on_invested_capital"].value is None


def test_a_company_with_no_debt_has_an_enterprise_value_of_market_cap_less_cash() -> None:
    """ID-02b's zero is a debt, not a gap: Align's enterprise value exists."""
    stmts = _stmts(ALGN)
    out = {
        r.name: r
        for r in valuation.compute(stmts, _key(stmts), ClosePrice(date="2022-12-31", close=200.0))
    }
    market_cap = out["market_capitalization"].value
    if market_cap is None:  # the slice has no share count: the debt is what is tested
        assert out["enterprise_value"].value is None
        return
    assert out["enterprise_value"].value == pytest.approx(market_cap - 942_050_000.0)


# -- MET-09: interest coverage on the filed sign -------------------------------------------------


def _coverage(stmts: Statements, key: str) -> Any:
    return next(r for r in leverage.compute(stmts, key) if r.name == "interest_coverage")


def test_interest_coverage_divides_operating_income_by_the_filed_interest_expense() -> None:
    stmts = _stmts(DUKE)
    key = _key(stmts)
    result = _coverage(stmts, key)
    operating = stmts.get("operating_income", key)
    assert operating is not None
    assert result.value == pytest.approx(operating / 3_384_000_000.0)
    assert not any(k.startswith("flag_interest") for k in result.inputs)


def test_wat_interest_coverage_is_positive_now_that_the_expense_is_on_its_filed_sign() -> None:
    """Waters displays the expense negated: 202 of 319 production interest values were negative,
    and ``abs()`` was what made the coverage right. Filed, it needs no repair."""
    stmts = _stmts(WAT)
    key = _key(stmts, quarter=True)
    result = _coverage(stmts, key)
    operating = stmts.get("operating_income", key)
    assert operating is not None
    assert result.value == pytest.approx(operating / 30_442_000.0)


def test_a_negative_filed_interest_expense_leaves_the_coverage_empty_and_flagged() -> None:
    """MET-09: a negative denominator is left out of the score and flagged -- never made positive
    with ``abs()`` (ID-11). Duke's expense with the sign hint reversed is filed as -3,384M."""
    payload = copy.deepcopy(_payload(DUKE))
    _row(payload, "income_statement", G + "InterestExpense")["preferred_sign"] = -1.0
    stmts = Statements.from_payload(payload)
    key = _key(stmts)
    assert stmts.get("interest_expense", key) == -3_384_000_000.0
    result = _coverage(stmts, key)
    assert result.value is None
    assert result.inputs["flag_interest_expense_negative"] == 1.0


def test_a_negative_filed_interest_expense_empties_the_unlevered_fcf_input_not_the_metric() -> None:
    """MD-38 (D8, T-151 part 2) is C10's EBIT form, so the enterprise FCF yield no longer reads
    interest expense. The unlevered-FCF *input* still does: a negative filed expense is neither
    added back (it would be subtracted) nor made positive -- the input is empty (MET-09, ID-11) --
    while the metric itself is unchanged."""
    price = ClosePrice(date="2024-12-31", close=100.0)
    mcap = _duke_mcap(100.0)
    ok = {
        r.name: r
        for r in valuation.compute(_stmts(DUKE), "2024-12-31 (FY)", price, market_cap=mcap)
    }
    assert ok["enterprise_fcf_yield"].value is not None
    assert ok["free_cash_flow_yield"].inputs["free_cash_flow_to_firm"] is not None
    payload = copy.deepcopy(_payload(DUKE))
    _row(payload, "income_statement", G + "InterestExpense")["preferred_sign"] = -1.0
    negative = {
        r.name: r
        for r in valuation.compute(
            Statements.from_payload(payload), "2024-12-31 (FY)", price, market_cap=mcap
        )
    }
    assert negative["free_cash_flow_yield"].value is not None  # the equity yield needs no interest
    assert "free_cash_flow_to_firm" not in negative["free_cash_flow_yield"].inputs
    assert negative["enterprise_fcf_yield"].value == pytest.approx(ok["enterprise_fcf_yield"].value)


def test_an_interest_expense_rejected_for_its_sign_is_not_a_missing_one() -> None:
    """``InterestAndDebtExpense`` filed negative is rejected (ID-13) and the item is empty -- but an
    empty interest expense must not read as "no interest": the unlevered-FCF input is empty, not the
    levered one. The MD-38 metric (D8) no longer reads interest at all."""
    price = ClosePrice(date="2024-12-31", close=100.0)
    mcap = _duke_mcap(100.0)
    payload = copy.deepcopy(_payload(DUKE))
    interest = _row(payload, "income_statement", G + "InterestExpense")
    interest["concept"] = G + "InterestAndDebtExpense"  # Duke's expense under the listed concept
    ok = {
        r.name: r
        for r in valuation.compute(
            Statements.from_payload(payload), "2024-12-31 (FY)", price, market_cap=mcap
        )
    }
    assert ok["free_cash_flow_yield"].inputs["free_cash_flow_to_firm"] is not None
    interest["preferred_sign"] = -1.0  # filed negative
    rejected = {
        r.name: r
        for r in valuation.compute(
            Statements.from_payload(payload), "2024-12-31 (FY)", price, market_cap=mcap
        )
    }
    assert rejected["free_cash_flow_yield"].value is not None
    assert "free_cash_flow_to_firm" not in rejected["free_cash_flow_yield"].inputs
    assert rejected["enterprise_fcf_yield"].value == pytest.approx(ok["enterprise_fcf_yield"].value)


def test_a_zero_interest_expense_leaves_the_coverage_empty() -> None:
    payload = copy.deepcopy(_payload(DUKE))
    row = _row(payload, "income_statement", G + "InterestExpense")
    for column in [c for c in row if c[:2] == "20"]:
        row[column] = 0.0
    stmts = Statements.from_payload(payload)
    result = _coverage(stmts, _key(stmts))
    assert result.value is None and "flag_interest_expense_negative" not in result.inputs


def test_a_negative_interest_and_debt_expense_is_a_wrong_sign_and_rejected() -> None:
    """``InterestAndDebtExpense`` is on DQC_0015 (ID-13): filed negative it is rejected."""
    # an XEL filing with only this concept: the plain InterestExpense row dropped
    payload = _drop(_payload(XEL), "income_statement", G + "InterestExpense")
    row = _row(payload, "income_statement", G + "InterestAndDebtExpense")
    row["preferred_sign"] = -1.0 * (row.get("preferred_sign") or 1.0)
    stmts = Statements.from_payload(payload)
    resolution = stmts.resolve("interest_expense", _key(stmts))
    assert resolution.value is None and "wrong_sign_rejected" in resolution.flags


# -- N5: cash counted twice --------------------------------------------------------------------------


def test_n5_the_cash_ratio_does_not_add_investments_to_a_cash_line_that_contains_them() -> None:
    """Microsoft files ``CashAndCashEquivalentsAtCarryingValue`` (20,840M) and the combined line
    (78,428M, which includes the 57,588M of short-term investments). When only the combined line is
    the cash, adding the investments again counted them twice (CMI: 4 production filings)."""
    stmts = _stmts(MSFT)
    key = _key(stmts, quarter=True)
    ratios = {r.name: r for r in liquidity.compute(stmts, key)}
    assert ratios["cash_ratio"].value == pytest.approx(
        (20_840 + 57_588) / 115_200
    )  # distinct lines: summed

    payload = _drop(_payload(MSFT), "balance_sheet", G + "CashAndCashEquivalentsAtCarryingValue")
    only_combined = Statements.from_payload(payload)
    resolution = only_combined.resolve("cash", key)
    assert resolution.value == 78_428_000_000.0
    assert resolution.concept == G + "CashCashEquivalentsAndShortTermInvestments"
    ratios = {r.name: r for r in liquidity.compute(only_combined, key)}
    assert ratios["cash_ratio"].value == pytest.approx(78_428 / 115_200)
    assert ratios["cash_ratio"].inputs["flag_cash_includes_short_term_investments"] == 1.0


# -- N6 / ID-05: a balance sheet is read at its period end ---------------------------------------------


def test_n6_a_balance_sheet_is_never_read_from_an_earlier_date() -> None:
    """Constellation's 10-Q carries the balance sheet at the quarter end and at the last fiscal year
    end. With the quarter's own column missing, the nearest earlier instant -- the year end, months
    before -- was read as this period's balance sheet (8 production filings)."""
    stmts = _stmts(STZ)
    key = _key(stmts, quarter=True)
    assert stmts.get("equity", key) == 9_390_800_000.0

    payload = copy.deepcopy(_payload(STZ))
    own = key[:10]
    for row in payload["balance_sheet"]:
        row.pop(own, None)
    without = Statements.from_payload(payload)
    assert without.instant_periods()  # the earlier columns are still there
    assert without.get("equity", key) is None
    assert without.total_debt(key).value is None


def test_a_balance_sheet_dated_days_from_the_month_end_is_the_period_s_own() -> None:
    """PER-08: a 52/53-week filer's balance sheet is dated the Saturday nearest the month end; it
    rounds to the same month end as the duration column and is the period's instant."""
    payload = {
        "income_statement": [
            {
                "concept": G + "Revenues",
                "abstract": False,
                "dimension": False,
                "2023-07-01 (Q2)": 5.0,
            }
        ],
        "balance_sheet": [
            {
                "concept": G + "Assets",
                "abstract": False,
                "dimension": False,
                "2023-06-30": 9.0,
                "2022-12-31": 7.0,
            }
        ],
    }
    stmts = Statements.from_payload(payload)
    assert stmts.get("total_assets", "2023-07-01 (Q2)") == 9.0
