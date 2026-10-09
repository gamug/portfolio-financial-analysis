"""Level (b) measurements of the line-item rules: today's resolver over the stored ``financial_facts``.

Each ``m_*`` function takes the population (:class:`~sec_xcheck.records.Rec`) and returns :class:`Finding`s. They are
pure functions of the audit records, so each is tested on hand-built records (``tests/test_sec_xcheck_resolver.py``).
Findings N3-N7, N12-N14 and D2-D4 are those of ``code_comparison_v1.md`` §2 and the brief's list.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from sec_xcheck.findings import Finding
from sec_xcheck.records import Rec, amounts_note, count

G = "us-gaap_"
CMD = "uv run python scripts/audit_sec_checklist.py measure --only resolver"
OCF_LINES = (
    G + "NetCashProvidedByUsedInOperatingActivities",
    G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CURRENT_MATURITIES_INCLUDED = (
    G + "LongTermDebt",
    G + "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)
COMBINED_DEBT_LEASE = (
    G + "LongTermDebtAndCapitalLeaseObligations",
    G + "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
)


def concept_of(r: Rec, item: str) -> str | None:
    return r.f["items"][item]["concept"]


def how_of(r: Rec, item: str) -> str | None:
    return r.f["items"][item]["how"]


# ---------------------------------------------------------------- N3 / CON-08: operating cash flow
def ocf_continuing_wins(r: Rec) -> bool:
    ocf = r.f["ocf"]
    total, cont = (
        ocf.get(G + "NetCashProvidedByUsedInOperatingActivities"),
        ocf.get(G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    )
    return (
        total is not None
        and cont is not None
        and total != cont
        and r.value("operating_cash_flow") == cont
    )


def ocf_both_differ(r: Rec) -> bool:
    ocf = r.f["ocf"]
    vals = set(ocf.values())
    return len(ocf) == len(OCF_LINES) and len(vals) > 1


# ---------------------------------------------------------------- N4 / MET-07: debt
def debt_double_counts(r: Rec) -> bool:
    """A long-term line that already contains current maturities, plus ``LongTermDebtCurrent`` read as short-term debt."""
    return concept_of(r, "long_term_debt") in CURRENT_MATURITIES_INCLUDED and concept_of(
        r, "short_term_debt"
    ) == (G + "LongTermDebtCurrent")


def double_counted_amount(r: Rec) -> float:
    return abs(r.value("short_term_debt") or 0.0)


def lt_only(r: Rec) -> bool:
    return r.value("long_term_debt") is not None and r.value("short_term_debt") is None


def st_only(r: Rec) -> bool:
    return r.value("long_term_debt") is None and r.value("short_term_debt") is not None


def debt_absent(r: Rec) -> bool:
    return r.value("long_term_debt") is None and r.value("short_term_debt") is None


def ev_zero_filled(r: Rec) -> bool:
    """Enterprise value adds ``(total_debt or 0) - (cash or 0)``: a missing term is silently 0 (D10)."""
    return debt_absent(r) or r.value("cash") is None


# D-06(d): the short-term debt order T-148 should use: disjoint LongTermDebtCurrent + ShortTermBorrowings,
# CommercialPaper only without ShortTermBorrowings, DebtCurrent (it includes leases) last and flagged.
def proposed_short_term_debt(st: dict[str, float]) -> float | None:
    ltdc, stb = st.get(G + "LongTermDebtCurrent"), st.get(G + "ShortTermBorrowings")
    cp, dc = st.get(G + "CommercialPaper"), st.get(G + "DebtCurrent")
    parts = [p for p in (ltdc, stb if stb is not None else cp) if p is not None]
    if parts:
        return float(sum(parts))
    return dc


def st_debt_differs(r: Rec) -> bool:
    new, old = proposed_short_term_debt(r.f["st"]), r.value("short_term_debt")
    if new is None and old is None:
        return False
    return new is None or old is None or abs(new - old) > 1.0


def st_debt_gap(r: Rec) -> float:
    new, old = proposed_short_term_debt(r.f["st"]), r.value("short_term_debt")
    return abs((new or 0.0) - (old or 0.0))


# ---------------------------------------------------------------- N5: cash
def cash_double_counts(r: Rec) -> bool:
    return concept_of(r, "cash") == G + "CashCashEquivalentsAndShortTermInvestments" and r.value(
        "short_term_investments"
    ) not in (None, 0.0)


# ---------------------------------------------------------------- N6 / ID-05: balance sheet date
def instant_not_exact(r: Rec) -> bool:
    return r.f["instant"] is not None and not r.f["instant_exact"]


def instant_gap_days(r: Rec) -> str:
    gap = (dt.date.fromisoformat(r.f["key"][:10]) - dt.date.fromisoformat(r.f["instant"])).days
    return f"balance sheet read from {r.f['instant']}, {gap} days before the period end"


# ---------------------------------------------------------------- D-02: revenue as a sum of components
def revenue_is_component_sum(r: Rec) -> bool:
    """No filed total: the revenue is the sum of several co-reported streams (ASC 606 beside lease income, ...)."""
    return how_of(r, "revenue") == "sum"


# ---------------------------------------------------------------- N14: D&A
def da_depreciation_alone(r: Rec) -> bool:
    return concept_of(r, "depreciation_amortization") == G + "Depreciation"


# ---------------------------------------------------------------- D2 / ID-02a / ID-03: precedence
def equity_not_narrowest(r: Rec) -> bool:
    """``StockholdersEquity`` is filed, and the resolver read another row (a label match, or a wider concept)."""
    parent = r.f["equity"].get(G + "StockholdersEquity")
    return (
        parent is not None
        and r.value("equity") is not None
        and abs(r.value("equity") - parent) > 1.0
    )


def ni_profitloss_over_parent(r: Rec) -> bool:
    parent = r.f["ni"].get(G + "NetIncomeLoss")
    return (
        parent is not None
        and concept_of(r, "net_income") == G + "ProfitLoss"
        and abs((r.value("net_income") or 0.0) - parent) > 1.0
    )


def ni_available_to_common(r: Rec) -> bool:
    return concept_of(r, "net_income") == G + "NetIncomeLossAvailableToCommonStockholdersBasic"


def nci_present(r: Rec) -> bool:
    return abs(r.f["ni"].get(G + "NetIncomeLossAttributableToNoncontrollingInterest", 0.0)) > 0.0


# ---------------------------------------------------------------- D3 / D-06(c): income tax
def tax_component_only(r: Rec) -> bool:
    return (
        how_of(r, "income_tax") is not None
        and concept_of(r, "income_tax") != G + "IncomeTaxExpenseBenefit"
    )


def tax_current_deferred_without_total(r: Rec) -> bool:
    tax = r.f["tax"]
    return (
        G + "IncomeTaxExpenseBenefit" not in tax
        and G + "CurrentIncomeTaxExpenseBenefit" in tax
        and G + "DeferredIncomeTaxExpenseBenefit" in tax
    )


# ---------------------------------------------------------------- ID-01: extension / label matches
def not_exact_concept(item: str) -> Callable[[Rec], bool]:
    def pred(r: Rec) -> bool:
        return how_of(r, item) in ("standard", "label", "label_fallback")

    return pred


def custom_concept_used(item: str) -> Callable[[Rec], bool]:
    def pred(r: Rec) -> bool:
        c = concept_of(r, item)
        return c is not None and not c.startswith((G, "dei_"))

    return pred


# ---------------------------------------------------------------- N12 / ID-20: dead concepts
def dead_concept_filed(r: Rec) -> bool:
    return any(r.f["dead"][s] for s in r.f["dead"])


def m_line_items(recs: list[Rec]) -> list[Finding]:
    """The N3-N7, N12-N14, D2-D4, ID-01, MET-07 measurements at level (b)."""
    f: list[Finding] = []
    f.append(
        count(
            "N3.ocf_continuing_resolved",
            ["CON-08", "ID-02b"],
            "Operating cash flow resolved to the continuing-operations line although the total is filed and differs",
            recs,
            ocf_continuing_wins,
            command=CMD,
        )
    )
    f.append(
        count(
            "N3.ocf_total_and_continuing_differ",
            ["CON-08"],
            "Both OCF lines filed with different values",
            recs,
            ocf_both_differ,
            command=CMD,
        )
    )
    dd = [r for r in recs if debt_double_counts(r)]
    f.append(
        count(
            "N4.debt_double_count",
            ["MET-07", "ID-02b"],
            "Long-term debt incl. current maturities plus LongTermDebtCurrent: current maturities counted twice",
            recs,
            debt_double_counts,
            command=CMD,
            note=amounts_note([double_counted_amount(r) for r in dd]),
        )
    )
    f.append(
        count(
            "N5.cash_double_count",
            ["ID-02a", "ID-02b"],
            "Cash resolved to the cash-and-short-term-investments line, then short-term investments added again",
            recs,
            cash_double_counts,
            command=CMD,
        )
    )
    f.append(
        count(
            "N6.balance_from_other_date",
            ["ID-05"],
            "Balance-sheet items read from an instant that is not the period end",
            recs,
            instant_not_exact,
            detail=instant_gap_days,
            command=CMD,
        )
    )
    f.append(
        count(
            "N7.lt_debt_only",
            ["MET-00", "ID-02b"],
            "Debt = long-term only (no short-term line)",
            recs,
            lt_only,
            command=CMD,
        )
    )
    f.append(
        count(
            "N7.st_debt_only",
            ["MET-00", "ID-02b"],
            "Debt = short-term only (no long-term line): a partial debt",
            recs,
            st_only,
            command=CMD,
        )
    )
    f.append(
        count(
            "N7.debt_absent",
            ["MET-00"],
            "No debt line at all (ROIC, EV read it as 0)",
            recs,
            debt_absent,
            command=CMD,
        )
    )
    f.append(
        count(
            "D10.ev_debt_or_cash_zero_filled",
            ["MET-00"],
            "Enterprise value would fill a missing debt or cash with 0",
            recs,
            ev_zero_filled,
            command=CMD,
        )
    )
    f.append(
        count(
            "D02.revenue_component_sum",
            ["ID-02b", "CON-11"],
            "Revenue is the sum of several components because no total is filed (D-02: allowed only when they are the only revenue lines)",
            recs,
            revenue_is_component_sum,
            command=CMD,
        )
    )
    f.append(
        count(
            "N14.da_depreciation_alone",
            ["ID-02b"],
            "D&A resolved to Depreciation alone (a component of D&A)",
            recs,
            da_depreciation_alone,
            command=CMD,
        )
    )
    f.append(
        count(
            "D2.equity_not_narrowest",
            ["ID-02a"],
            "Equity read from a row other than StockholdersEquity although that is filed",
            recs,
            equity_not_narrowest,
            command=CMD,
        )
    )
    f.append(
        count(
            "D2.net_income_profitloss_over_parent",
            ["ID-03", "ID-02a"],
            "Net income read as ProfitLoss (incl. NCI) although NetIncomeLoss (parent) is filed and differs",
            recs,
            ni_profitloss_over_parent,
            command=CMD,
        )
    )
    f.append(
        count(
            "N13.net_income_available_to_common",
            ["ID-03"],
            "Net income resolved to the available-to-common (after preferred dividends) line",
            recs,
            ni_available_to_common,
            command=CMD,
        )
    )
    f.append(
        count(
            "D3.income_tax_component",
            ["ID-02b"],
            "Income tax resolved to a component or a standard-concept match, not IncomeTaxExpenseBenefit",
            recs,
            tax_component_only,
            command=CMD,
        )
    )
    f.append(
        count(
            "D6c.tax_current_deferred_without_total",
            ["ID-02b"],
            "Current and deferred tax both filed and no total (the D-06c map would apply)",
            recs,
            tax_current_deferred_without_total,
            command=CMD,
        )
    )
    f.append(
        count(
            "D4.short_term_debt_order_differs",
            ["ID-02b", "MET-07"],
            "Short-term debt under D-06(d)'s order differs from today's first match",
            recs,
            st_debt_differs,
            command=CMD,
            note=amounts_note([st_debt_gap(r) for r in recs if st_debt_differs(r)]),
        )
    )
    f.append(
        count(
            "N12.dead_concepts_filed",
            ["ID-20"],
            "A filing carries one of the four dead registry concepts",
            recs,
            dead_concept_filed,
            command=CMD,
        )
    )
    f.append(
        count(
            "MET07.combined_debt_lease_element",
            ["MET-07"],
            "Long-term debt resolved to a combined debt-and-lease element",
            recs,
            lambda r: concept_of(r, "long_term_debt") in COMBINED_DEBT_LEASE,
            command=CMD,
            note="checklist MET-07 reports 1,329 of 5,075 production filings with the combined element as the only long-term debt line",
        )
    )
    for item in (
        "equity",
        "net_income",
        "income_tax",
        "cash",
        "long_term_debt",
        "short_term_debt",
        "capital_expenditure",
        "revenue",
    ):
        f.append(
            count(
                f"ID01.{item}_label_or_standard_match",
                ["ID-01", "ID-02a"],
                f"{item} resolved by a label or standard-concept match, not an exact concept",
                recs,
                not_exact_concept(item),
                command=CMD,
            )
        )
        f.append(
            count(
                f"ID01.{item}_custom_concept",
                ["ID-01"],
                f"{item} resolved to an issuer-extension concept",
                recs,
                custom_concept_used(item),
                command=CMD,
            )
        )
    return f


def sign_profile(recs: list[Rec], item: str) -> dict[str, int]:
    """Counts of the displayed sign of a resolved signed item (ID-11: ``abs()`` hides which it was)."""
    out = {"negative": 0, "zero": 0, "positive": 0, "absent": 0}
    for r in recs:
        v = r.value(item)
        out[
            "absent" if v is None else "negative" if v < 0 else "zero" if v == 0 else "positive"
        ] += 1
    return out


def m_signs(recs: list[Rec]) -> list[Finding]:
    f: list[Finding] = []
    for item in ("capital_expenditure", "interest_expense", "stock_based_compensation"):
        prof = sign_profile(recs, item)
        f.append(
            count(
                f"ID11.{item}_abs_changes_value",
                ["ID-11"],
                f"{item}: stored with a negative displayed sign, so abs() changes the value used",
                recs,
                lambda r, i=item: (r.value(i) or 0.0) < 0,
                population=lambda r, i=item: r.value(i) is not None,
                command=CMD,
                note=f"sign profile {prof}",
            )
        )
    return f


def m_income_tax_sign(recs: list[Rec]) -> list[Finding]:
    """ID-12 / D1 at level (b): a negative displayed income tax (the edgartools-flipped sign reaches ``effective_tax_rate``)."""
    return [
        count(
            "D1.income_tax_negative",
            ["ID-12", "ID-11"],
            "Income tax resolved negative (displayed sign; the stored rows carry no preferred_sign)",
            recs,
            lambda r: (r.value("income_tax") or 0.0) < 0,
            population=lambda r: r.value("income_tax") is not None,
            command=CMD,
            note="a negative IncomeTaxExpenseBenefit is valid for a tax benefit; level (a) separates the flipped ones",
        )
    ]


def all_measures(recs: list[Rec]) -> list[Finding]:
    return [*m_line_items(recs), *m_signs(recs), *m_income_tax_sign(recs)]


Metadata = dict[str, Any]
