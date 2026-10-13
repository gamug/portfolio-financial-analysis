"""Capital building blocks shared by the return, leverage and valuation metrics.

MD-02 (invested capital), MD-03 (net debt), MD-35 (enterprise value) and MD-38 (non-cash working
capital) all combine the same balance-sheet items -- debt (MET-07), parent equity (ID-02a),
noncontrolling interest, cash and short-term investments. :class:`~fundamental_agent.statements.Capital`
resolves those items at one instant column (the filing's own, or a stored earlier filing's); these
functions combine them under the rules of T-151 part 2 (D6, D8, D9) and keep each term's flags so a
metric can record provenance.

The ratio formulas stay in the metric modules; only the shared combination lives here.
"""

from __future__ import annotations

from fundamental_agent.statements import Capital, Resolution

__all__ = [
    "capital_flags",
    "invested_capital",
    "nci",
    "net_debt",
    "net_short_term_investments",
    "working_capital",
]


def _join(*resolutions: Resolution) -> tuple[str, ...]:
    flags: list[str] = []
    for res in resolutions:
        flags.extend(f for f in res.flags if f not in flags)
    return tuple(flags)


def _concept(*resolutions: Resolution) -> str | None:
    parts = [r.concept for r in resolutions if r.concept]
    return " + ".join(parts) if parts else None


def nci(cap: Capital, *, income_filed: bool) -> Resolution:
    """NCI (MD-02, MD-35; D6): the NCI in equity + the redeemable NCI; else CON-04's identity
    (equity including NCI - parent equity) when both are filed; else 0 only when no income
    attributable to NCI is filed; else empty, flagged ``nci_capture_failure``."""
    if cap.minority_interest.value is not None or cap.redeemable_nci.value is not None:
        value = (cap.minority_interest.value or 0.0) + (cap.redeemable_nci.value or 0.0)
        return Resolution(
            value,
            _concept(cap.minority_interest, cap.redeemable_nci),
            "map",
            _join(cap.minority_interest, cap.redeemable_nci),
        )
    if cap.equity_incl_nci.value is not None and cap.equity.value is not None:
        return Resolution(
            cap.equity_incl_nci.value - cap.equity.value,
            _concept(cap.equity_incl_nci, cap.equity),
            "map",
            ("nci_con04_identity",),
        )
    if not income_filed:
        return Resolution(0.0, None, "none_filed", ("nci_no_income_filed",))
    return Resolution(None, None, None, ("nci_capture_failure",))


def net_short_term_investments(cap: Capital) -> Resolution:
    """Short-term investments to net in MD-02/MD-03/MD-35 (D6): the filed value only when it is
    filed apart from cash. Never when the cash line already includes them -- the
    ``includes_short_term_investments`` flag of the (k)(1) cash read (B4, T-148). ``0`` when
    nothing is filed, so a caller can subtract it directly."""
    sti = cap.short_term_investments
    if sti.value is None:
        return Resolution(0.0, None, "none_filed", ())
    includes = cap.cash.concept == "us-gaap_CashCashEquivalentsAndShortTermInvestments" and (
        "cash_sti_tag_cash_caption" not in cap.cash.flags
    )
    if includes:
        return Resolution(0.0, None, "none_filed", ("short_term_investments_in_cash",))
    return sti


def net_debt(cap: Capital) -> Resolution:
    """Net debt (MD-03; D9): debt - cash - short-term investments. Empty when debt or cash is
    missing (MET-00)."""
    sti = net_short_term_investments(cap)
    flags = _join(cap.debt, cap.cash, sti)
    if cap.debt.value is None or cap.cash.value is None:
        return Resolution(None, None, None, (*flags, "net_debt_capture_failure"))
    return Resolution(
        cap.debt.value - cap.cash.value - (sti.value or 0.0),
        _concept(cap.debt, cap.cash),
        "map",
        flags,
    )


def invested_capital(cap: Capital, *, income_filed: bool) -> Resolution:
    """Invested capital (MD-02; D6): debt + parent equity + NCI - cash - short-term investments,
    at the end of the prior year (MET-02). Empty, flagged, when a term is missing (MET-00)."""
    sti = net_short_term_investments(cap)
    nci_res = nci(cap, income_filed=income_filed)
    flags = _join(cap.debt, cap.equity, cap.cash, sti, nci_res)
    debt, equity, cash, nci_value = cap.debt.value, cap.equity.value, cap.cash.value, nci_res.value
    if debt is None or equity is None or cash is None or nci_value is None:
        return Resolution(None, None, None, (*flags, "invested_capital_capture_failure"))
    value = debt + equity + nci_value - cash - (sti.value or 0.0)
    return Resolution(value, _concept(cap.debt, cap.equity, nci_res, cap.cash), "map", flags)


def working_capital(cap: Capital) -> Resolution:
    """Non-cash working capital (MD-38; D8): (current assets - cash - short-term investments) -
    (current liabilities - short-term debt). Empty when current assets, current liabilities or
    cash is missing -- the balance sheet is not classified (MET-00). A company that files no
    short-term debt line has none to subtract, so it is read as 0."""
    sti = net_short_term_investments(cap)
    flags = _join(cap.current_assets, cap.current_liabilities, cap.cash, sti, cap.short_term_debt)
    ca, cl, cash = cap.current_assets.value, cap.current_liabilities.value, cap.cash.value
    if ca is None or cl is None or cash is None:
        return Resolution(None, None, None, (*flags, "working_capital_capture_failure"))
    value = (ca - cash - (sti.value or 0.0)) - (cl - (cap.short_term_debt.value or 0.0))
    return Resolution(value, "noncash_working_capital", "map", flags)


def capital_flags(cap: Capital, prefix: str) -> dict[str, float]:
    """Every resolution flag of *cap* as metric inputs (D11), keyed ``flag_<prefix>_<flag>``."""
    out: dict[str, float] = {}
    for name in (
        "total_assets",
        "equity",
        "cash",
        "short_term_investments",
        "minority_interest",
        "redeemable_nci",
        "equity_incl_nci",
        "current_assets",
        "current_liabilities",
        "debt",
        "short_term_debt",
    ):
        res: Resolution = getattr(cap, name)
        for flag in res.flags:
            out[f"flag_{prefix}_{name}_{flag}"] = 1.0
    return out
