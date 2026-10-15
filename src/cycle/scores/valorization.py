"""VALORIZATION v2 (T-071 part 2b): value only, priced on the **analysis date**.

Four measures, each on the filing's own inputs (D1's rule input) and the market value **on the
cycle date** (MKT-01) -- never a stored period-end ratio:

====================  ================================  ==========================================
measure               formula                           source
====================  ================================  ==========================================
earnings yield (E/P)  ``max(net income, 0) / cap``      MD-39, MET-05 (C4 p.13, p.18): the positive
                                                        part, the negative-earnings dummy
                                                        recorded, never ranked
book-to-market (B/M)  ``book equity / cap``             C4 p.2, p.13 ("BE/ME, the ratio of the
                                                        book value of common equity to its market
                                                        value"); C4 p.15 leaves a non-positive BE
                                                        out
EBITDA/EV             ``EBITDA / EV``                   C10 p.46; EV is MD-35's, EBITDA MD-01's
enterprise FCF yield  ``MD-38 numerator / EV``          MD-38, C10's EBIT form
====================  ================================  ==========================================

The yields are the inverse of the multiples C4 and C10 use -- the same form C4 gives E/P and BE/ME.

**Applicability** is the matrix's cell for the measure's own metric (:data:`MATRIX_METRIC`) on the
member's APP-00 profile: NA leaves the measure *not applicable* (never "missing"), ADJ keeps the
value and records the rule. So a financial firm is scored on E/P and B/M only (APP-02, C2 p.27), a
REIT on E/P (ADJ, APP-04), B/M and EBITDA/EV (MD-38 leaves its FCF yield NA), and a regulated
utility on E/P, B/M and EBITDA/EV (the user's decision, D-13). B/M itself has no matrix row: it
reads ``market_capitalization``'s cell -- A for every company type, ADJ for a multi-class one
(MKT-03) -- a declared limitation, like ``ebitda``. A member with **no** profile is never guessed:
every measure applies.

**The data-quality gate.** A measure is dropped (``quarantined``) when the gate quarantines the
metric that carries its input (:data:`CARRIERS`). The market cap **on D** is not a metric -- it is
``cycle.data.market_cap_estimates`` (T-132) -- so no stored-metric quarantine applies to it; a
member without one is ``missing_input``.

**Coverage.** A score needs at least ``min(MIN_PRESENT, the applicable count)`` present measures;
otherwise ``raw_value`` is ``None`` (never a neutral 50). The row is still written, with its
components, so the coverage is visible, and the blend renormalizes over the components it has
(``_blended``).

**Weights.** No source gives weights between the four measures, so the raw score is 100 x the
equal-weight mean of the present measures' percentiles (the 1/N argument, as T-141 did for the
composite). Each measure is winsorized at DQ-02's 0.5% per tail (:func:`winsorize`) before it is
ranked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from cycle.scores.normalize import rank_pct, winsorize
from cycle.scores.technical import RawScore

VERSION = "valorization-v2"
SCORE_TYPE = "VALORIZATION"

#: A score needs at least ``min(MIN_PRESENT, the applicable count)`` present measures.
MIN_PRESENT: Final[int] = 2

EARNINGS_YIELD: Final[str] = "earnings_yield"
BOOK_TO_MARKET: Final[str] = "book_to_market"
EBITDA_TO_EV: Final[str] = "ebitda_to_ev"
ENTERPRISE_FCF_YIELD: Final[str] = "enterprise_fcf_yield"
MEASURES: Final[tuple[str, ...]] = (
    EARNINGS_YIELD,
    BOOK_TO_MARKET,
    EBITDA_TO_EV,
    ENTERPRISE_FCF_YIELD,
)

#: The matrix metric each measure's applicability cell is read from.
MATRIX_METRIC: Final[dict[str, str]] = {
    EARNINGS_YIELD: "earnings_yield",
    BOOK_TO_MARKET: "market_capitalization",
    EBITDA_TO_EV: "enterprise_value",
    ENTERPRISE_FCF_YIELD: "enterprise_fcf_yield",
}

#: The metric rows that carry each measure's inputs (the data-quality gate's map).
CARRIERS: Final[dict[str, tuple[str, ...]]] = {
    EARNINGS_YIELD: ("profitability.return_on_assets",),
    BOOK_TO_MARKET: ("leverage.debt_to_equity",),
    EBITDA_TO_EV: ("leverage.ebitda", "valuation.enterprise_value"),
    ENTERPRISE_FCF_YIELD: ("valuation.enterprise_fcf_yield", "valuation.enterprise_value"),
}

# The status vocabulary (D2).
PRESENT: Final[str] = "present"
NOT_APPLICABLE: Final[str] = "not_applicable"
MISSING_INPUT: Final[str] = "missing_input"
EXCLUDED_EV: Final[str] = "excluded_nonpositive_denominator"
EXCLUDED_EQUITY: Final[str] = "excluded_nonpositive_book_equity"
QUARANTINED: Final[str] = "quarantined"

# The D1 input keys each measure reads.
_NET_INCOME: Final[str] = "net_income_ttm"
_EBITDA: Final[str] = "ebitda"
_MD38: Final[str] = "md38_numerator"
_BOOK_EQUITY: Final[str] = "book_equity"
_TOTAL_DEBT: Final[str] = "total_debt"
_NCI: Final[str] = "nci"
_CASH: Final[str] = "cash"
_STI: Final[str] = "short_term_investments"


@dataclass(frozen=True)
class Member:
    """One universe member's VALORIZATION inputs on the cycle date.

    ``values`` is D1's rule input (its ``inputs_json``); ``applicable`` maps each measure to the
    matrix's ``(code, rule)``; ``quarantined`` is the gate's key set for the member's filing."""

    asset_id: int
    market_cap: float | None = None
    values: dict[str, Any] = field(default_factory=dict)
    applicable: dict[str, tuple[str, str]] = field(default_factory=dict)
    quarantined: frozenset[str] = frozenset()
    # the audit trail
    share_date: str | None = None
    close_date: str | None = None
    accession: str | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def enterprise_value(member: Member) -> float | None:
    """MD-35 on the cycle date: market cap **on D** + debt + NCI - cash - short-term investments.
    Empty when the market cap, debt, cash or NCI is missing (MET-00), never read as 0."""
    cap = member.market_cap
    debt = _number(member.values.get(_TOTAL_DEBT))
    nci = _number(member.values.get(_NCI))
    cash = _number(member.values.get(_CASH))
    if cap is None or debt is None or cash is None or nci is None:
        return None
    return cap + debt + nci - cash - (_number(member.values.get(_STI)) or 0.0)


def _earnings_yield(member: Member, cap: float) -> tuple[float | None, str, list[str]]:
    net_income = _number(member.values.get(_NET_INCOME))
    if net_income is None:
        return None, MISSING_INPUT, []
    # MET-05 (Fama and French): E(+)/P -- the positive part. A loss contributes 0, the lowest
    # earnings yield, never a negative one; the dummy is counted by the caller.
    return max(net_income, 0.0) / cap, PRESENT, []


def _book_to_market(member: Member, cap: float) -> tuple[float | None, str, list[str]]:
    equity = _number(member.values.get(_BOOK_EQUITY))
    if equity is None:
        return None, MISSING_INPUT, []
    if equity <= 0.0:
        # C4 p.15: negative-BE firms "we do not include them in the tests". Never abs() (ID-11).
        return None, EXCLUDED_EQUITY, ["book_equity_nonpositive"]
    return equity / cap, PRESENT, []


def _over_ev(member: Member, name: str) -> tuple[float | None, str, list[str]]:
    ev = enterprise_value(member)
    if ev is None:
        return None, MISSING_INPUT, []
    if ev <= 0.0:
        # MET-09: a denominator <= 0 inverts the ordering -- left out of the continuous score and
        # flagged (a zero denominator is no ratio at all).
        return None, EXCLUDED_EV, ["ev_nonpositive" if ev < 0.0 else "ev_zero"]
    numerator = _number(member.values.get(_EBITDA if name == EBITDA_TO_EV else _MD38))
    if numerator is None:
        return None, MISSING_INPUT, []
    # A negative numerator with a positive denominator is a valid low value (MET-09).
    return numerator / ev, PRESENT, []


def _measure(member: Member, name: str) -> tuple[float | None, str, list[str]]:
    """``(value on D, status, flags)`` for one measure of one member."""
    code, rule = member.applicable.get(name, ("A", ""))
    if code == "NA":
        return None, NOT_APPLICABLE, [rule] if rule else []
    if any(key in member.quarantined for key in CARRIERS[name]):
        return None, QUARANTINED, []
    cap = member.market_cap
    if cap is None:
        return None, MISSING_INPUT, ["market_cap_missing"]
    if name == EARNINGS_YIELD:
        return _earnings_yield(member, cap)
    if name == BOOK_TO_MARKET:
        return _book_to_market(member, cap)
    return _over_ev(member, name)


def _clipped(values: list[float | None]) -> list[float | None]:
    """*values* with the present ones winsorized (DQ-02: 0.5% per tail), order preserved."""
    present = [v for v in values if v is not None]
    trimmed = iter(winsorize(present))
    return [next(trimmed) if v is not None else None for v in values]


def compute(members: dict[int, Member]) -> list[RawScore]:
    """One :class:`RawScore` per member, in *members*' order.

    Every member gets a row -- with its components and coverage -- even when the score is empty
    (``raw_value`` is ``None``); the caller writes them all, so the coverage is visible."""
    asset_ids = list(members)
    if not asset_ids:
        return []
    per_measure = {name: [_measure(members[aid], name) for aid in asset_ids] for name in MEASURES}
    # Rank each measure across every member that has it, after DQ-02's winsorizing (D3).
    percentiles = {name: rank_pct(_clipped([m[0] for m in per_measure[name]])) for name in MEASURES}
    out: list[RawScore] = []
    for i, aid in enumerate(asset_ids):
        member = members[aid]
        applicable = sum(
            1 for name in MEASURES if member.applicable.get(name, ("A", ""))[0] != "NA"
        )
        present = [name for name in MEASURES if per_measure[name][i][1] == PRESENT]
        measures: dict[str, Any] = {}
        for name in MEASURES:
            value, status, flags = per_measure[name][i]
            code, rule = member.applicable.get(name, ("A", ""))
            measures[name] = {
                "value": value,
                "percentile": percentiles[name][i] if status == PRESENT else None,
                "status": status,
                "flags": flags,
                "matrix": code,
                "matrix_rule": rule,
            }
        need = min(MIN_PRESENT, applicable)
        ranked: list[float] = []
        for name in present:
            percentile = percentiles[name][i]
            if percentile is None:  # a present measure always has one; never assume it
                ranked = []
                break
            ranked.append(float(percentile))
        raw = (
            100.0 * sum(ranked) / len(ranked)
            if ranked and len(ranked) >= need and len(ranked) == len(present)
            else None
        )
        components: dict[str, Any] = {
            "measures": measures,
            "applicable": float(applicable),
            "present": float(len(present)),
            "market_cap": member.market_cap,
            "enterprise_value": enterprise_value(member),
            "share_date": member.share_date,
            "close_date": member.close_date,
            "accession": member.accession,
        }
        out.append(RawScore(asset_id=aid, raw_value=raw, components=components))
    return out
