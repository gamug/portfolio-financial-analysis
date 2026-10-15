"""VALORIZATION v2 (T-071 part 2b): value only, priced on the analysis date.

The four measures on D1's rule input and the market cap on D; applicability from the matrix's cell
for the measure's own metric on the member's APP-00 profile; the data-quality gate's map; the
coverage rule; equal weights; DQ-02's winsorizing; and the components' audit trail.
"""

from __future__ import annotations

import pytest

from cycle.orchestrator import _value_applicability
from cycle.scores import valorization
from kg_schema.company_profile import CompanyProfile


def _profile(**over: object) -> CompanyProfile:
    base: dict[str, object] = {
        "cik": "0000000001",
        "ticker": "TST",
        "company_name": "Test Co",
        "type": "operating",
        "financial_firm": False,
        "reit": False,
        "multi_class": False,
        "regulated_utility": False,
        "sector": "Industrials",
        "sub_industry": "Machinery",
        "evidence_accession": None,
        "valid_from": "2020-01-01",
        "valid_to": "",
    }
    base.update(over)
    return CompanyProfile(**base)  # type: ignore[arg-type]


def _member(
    aid: int,
    *,
    profile: CompanyProfile | None = None,
    market_cap: float | None = 1000.0,
    quarantined: frozenset[str] = frozenset(),
    values: dict[str, float | None] | None = None,
) -> valorization.Member:
    return valorization.Member(
        asset_id=aid,
        market_cap=market_cap,
        values=dict(values or {}),
        applicable=_value_applicability(profile, False),
        quarantined=quarantined,
    )


_FULL: dict[str, float | None] = {
    "net_income_ttm": 100.0,
    "ebitda": 200.0,
    "md38_numerator": 80.0,
    "book_equity": 500.0,
    "total_debt": 100.0,
    "nci": 0.0,
    "cash": 50.0,
    "short_term_investments": 0.0,
}


def _statuses(scores: dict[int, valorization.RawScore], aid: int) -> dict[str, str]:
    measures = scores[aid].components["measures"]
    return {name: measures[name]["status"] for name in valorization.MEASURES}


# -- applicability by company profile (the matrix) -------------------------------------------------


def test_a_financial_firm_is_scored_on_earnings_and_book_only() -> None:
    """APP-02 and C2 p.27: "Firm value multiples such as Value to EBITDA or Value to EBIT cannot be
    easily adapted to value financial service firms ... the multiples ... are equity multiples".
    So EBITDA/EV and the FCF yield are *not applicable*, never "missing"."""
    scores = {
        s.asset_id: s
        for s in valorization.compute({1: _member(1, profile=_financial(), values=_FULL)})
    }
    statuses = _statuses(scores, 1)
    assert statuses["earnings_yield"] == valorization.PRESENT
    assert statuses["book_to_market"] == valorization.PRESENT
    assert statuses["ebitda_to_ev"] == valorization.NOT_APPLICABLE
    assert statuses["enterprise_fcf_yield"] == valorization.NOT_APPLICABLE
    assert scores[1].components["applicable"] == 2.0
    assert scores[1].raw_value is not None  # min(2, 2) present


def _financial() -> CompanyProfile:
    return _profile(type="article_9", financial_firm=True, sector="Financials")


def test_a_reit_keeps_earnings_yield_as_adj_and_drops_the_fcf_yield() -> None:
    """MD-38 (no classified balance sheet) leaves the FCF yield NA for a REIT; APP-04 makes its
    earnings yield ADJ -- the value is kept and the rule recorded."""
    scores = {
        s.asset_id: s
        for s in valorization.compute({1: _member(1, profile=_profile(reit=True), values=_FULL)})
    }
    measures = scores[1].components["measures"]
    assert measures["earnings_yield"]["status"] == valorization.PRESENT
    assert measures["earnings_yield"]["matrix"] == "ADJ"
    assert measures["earnings_yield"]["matrix_rule"] == "APP-04"
    assert measures["enterprise_fcf_yield"]["status"] == valorization.NOT_APPLICABLE


def test_a_regulated_utility_drops_the_fcf_yield() -> None:
    """The user's decision (D-13): a regulated utility keeps E/P, B/M and EBITDA/EV; its FCF yield
    is not applicable."""
    scores = {
        s.asset_id: s
        for s in valorization.compute(
            {1: _member(1, profile=_profile(regulated_utility=True), values=_FULL)}
        )
    }
    statuses = _statuses(scores, 1)
    assert statuses["enterprise_fcf_yield"] == valorization.NOT_APPLICABLE
    assert statuses["ebitda_to_ev"] == valorization.PRESENT
    assert scores[1].components["applicable"] == 3.0


def test_a_multi_sector_holding_is_adj_on_every_measure() -> None:
    """APP-00: a multi-sector holding's every cell is ADJ."""
    scores = {
        s.asset_id: s
        for s in valorization.compute(
            {1: _member(1, profile=_profile(type="multi_sector_holding"), values=_FULL)}
        )
    }
    measures = scores[1].components["measures"]
    assert all(measures[name]["matrix"] == "ADJ" for name in valorization.MEASURES)
    assert all(measures[name]["status"] == valorization.PRESENT for name in valorization.MEASURES)


def test_an_unknown_profile_is_never_guessed() -> None:
    """T-071 part 1 (P4): a member with no profile is computed, not guessed -- every measure
    applies."""
    scores = {
        s.asset_id: s for s in valorization.compute({1: _member(1, profile=None, values=_FULL)})
    }
    assert _statuses(scores, 1) == {name: valorization.PRESENT for name in valorization.MEASURES}
    assert scores[1].components["applicable"] == 4.0


# -- the measures' edge cases ---------------------------------------------------------------------


def test_ev_zero_is_left_out_and_a_negative_ev_too() -> None:
    """MET-09: a denominator <= 0 inverts the ordering -- left out of the continuous score and
    flagged (EV = 0 is no ratio at all)."""
    zero = _member(1, market_cap=100.0, values={**_FULL, "total_debt": 0.0, "cash": 100.0})
    negative = _member(1, market_cap=10.0, values={**_FULL, "total_debt": 0.0, "cash": 100.0})
    for member, flag in ((zero, "ev_zero"), (negative, "ev_nonpositive")):
        scores = {s.asset_id: s for s in valorization.compute({1: member})}
        measure = scores[1].components["measures"]["ebitda_to_ev"]
        assert measure["status"] == valorization.EXCLUDED_EV
        assert measure["value"] is None and flag in measure["flags"]


def test_a_negative_ebitda_with_a_positive_ev_is_a_valid_low_value() -> None:
    """MET-09: with a positive denominator a negative value is valid -- lower still means worse."""
    scores = {
        s.asset_id: s
        for s in valorization.compute({1: _member(1, values={**_FULL, "ebitda": -50.0})})
    }
    measure = scores[1].components["measures"]["ebitda_to_ev"]
    assert measure["status"] == valorization.PRESENT
    assert measure["value"] < 0


def test_the_gate_drops_the_measure_whose_carrier_it_quarantines() -> None:
    """D2's map: B/M reads leverage.debt_to_equity, EBITDA/EV reads leverage.ebitda and
    valuation.enterprise_value, the FCF yield valuation.enterprise_fcf_yield."""
    members = {
        1: _member(1, quarantined=frozenset({"leverage.debt_to_equity"}), values=_FULL),
        2: _member(2, quarantined=frozenset({"valuation.enterprise_value"}), values=_FULL),
        3: _member(3, quarantined=frozenset({"valuation.enterprise_fcf_yield"}), values=_FULL),
        4: _member(4, quarantined=frozenset({"leverage.ebitda"}), values=_FULL),
    }
    scores = {s.asset_id: s for s in valorization.compute(members)}
    assert _statuses(scores, 1)["book_to_market"] == valorization.QUARANTINED
    assert _statuses(scores, 1)["earnings_yield"] == valorization.PRESENT
    assert _statuses(scores, 2)["ebitda_to_ev"] == valorization.QUARANTINED
    assert _statuses(scores, 2)["enterprise_fcf_yield"] == valorization.QUARANTINED
    assert _statuses(scores, 3)["enterprise_fcf_yield"] == valorization.QUARANTINED
    assert _statuses(scores, 3)["ebitda_to_ev"] == valorization.PRESENT
    assert _statuses(scores, 4)["ebitda_to_ev"] == valorization.QUARANTINED


# -- the coverage rule and the score ---------------------------------------------------------------


def test_the_coverage_rule_needs_min_two_of_the_applicable_measures() -> None:
    """A score needs at least min(2, applicable) present measures; otherwise the raw score is empty
    -- never a neutral 50 -- and the row is still written with its components."""
    # only the net income and the market cap: E/P present, the other three missing
    members = {
        1: _member(1, values={"net_income_ttm": 100.0}),
        2: _member(2, values={"net_income_ttm": 50.0}),
    }
    scores = {s.asset_id: s for s in valorization.compute(members)}
    assert scores[1].raw_value is None and scores[2].raw_value is None
    assert scores[1].components["present"] == 1.0
    assert scores[1].components["applicable"] == 4.0
    assert _statuses(scores, 1)["earnings_yield"] == valorization.PRESENT


def test_the_raw_score_is_the_equal_weight_mean_of_the_present_percentiles() -> None:
    """No source gives weights between the measures, so the score is 100 x the equal-weight mean of
    the present measures' percentiles (as T-141 did for the composite)."""
    members = {
        1: _member(1, values=_FULL),
        2: _member(
            2,
            values={
                **_FULL,
                "net_income_ttm": 50.0,
                "ebitda": 100.0,
                "md38_numerator": 40.0,
                "book_equity": 250.0,
            },
        ),
        3: _member(
            3,
            values={
                **_FULL,
                "net_income_ttm": 10.0,
                "ebitda": 20.0,
                "md38_numerator": 8.0,
                "book_equity": 50.0,
            },
        ),
    }
    scores = {s.asset_id: s for s in valorization.compute(members)}
    # member 1 is best on all four: every percentile is 1.0 -> 100; member 3 worst -> 0
    assert scores[1].raw_value == pytest.approx(100.0)
    assert scores[3].raw_value == pytest.approx(0.0)
    measures = scores[1].components["measures"]
    assert all(measures[name]["percentile"] == pytest.approx(1.0) for name in valorization.MEASURES)


def test_dq02_winsorizes_a_measure_before_it_is_ranked() -> None:
    """DQ-02 (D3): each measure is winsorized at 0.5% per tail before it is ranked -- at 200+
    members that ties the extremes, which the percentile then averages."""
    # 200 members with an E/P of 1.0 plus one extreme at 1e6: the top value is clamped, so the
    # extreme's percentile ties with the 198th
    members = {i: _member(i, values={**_FULL, "net_income_ttm": 100.0}) for i in range(1, 201)}
    members[201] = _member(201, values={**_FULL, "net_income_ttm": 1e9})
    scores = {s.asset_id: s for s in valorization.compute(members)}
    pct = {
        aid: scores[aid].components["measures"]["earnings_yield"]["percentile"] for aid in scores
    }
    assert pct[201] == pytest.approx(0.5)  # clamped onto the cohort, so it ties with it
    assert max(p for p in pct.values() if p is not None) < 1.0  # it would take 1.0 unwinsorized


# -- the components' audit trail -------------------------------------------------------------------


def test_the_components_carry_the_audit_trail() -> None:
    member = _member(1, values=_FULL)
    member = valorization.Member(
        asset_id=1,
        market_cap=member.market_cap,
        values=member.values,
        applicable=member.applicable,
        share_date="2026-06-20",
        close_date="2026-06-30",
        accession="0000000000-26-000001",
    )
    scores = {s.asset_id: s for s in valorization.compute({1: member})}
    components = scores[1].components
    assert components["market_cap"] == 1000.0
    assert components["enterprise_value"] == pytest.approx(1050.0)  # 1000 + 100 + 0 - 50 - 0
    assert components["share_date"] == "2026-06-20"
    assert components["close_date"] == "2026-06-30"
    assert components["accession"] == "0000000000-26-000001"
    measure = components["measures"]["earnings_yield"]
    assert set(measure) == {"value", "percentile", "status", "flags", "matrix", "matrix_rule"}


def test_an_empty_cohort_is_no_scores() -> None:
    assert valorization.compute({}) == []


# -- F1 (fix round 1): B/M tests the book-equity sign before the gate ------------------------------


def test_book_to_market_tests_the_equity_sign_before_the_gate() -> None:
    """F1: DQ_NEG_EQUITY quarantines ``leverage.debt_to_equity`` for the *sign* itself, which D-04
    made a record. A non-positive book equity is therefore ``excluded_nonpositive_book_equity``
    (C4 p.15), never ``quarantined`` -- while a gate that quarantines the equity *value* (CON_01's
    or CON_04's identity, which also blocks D/E) still drops B/M."""
    negative = _member(
        1,
        quarantined=frozenset({"leverage.debt_to_equity"}),
        values={**_FULL, "book_equity": -500.0},
    )
    value_wrong = _member(2, quarantined=frozenset({"leverage.debt_to_equity"}), values=_FULL)
    scores = {s.asset_id: s for s in valorization.compute({1: negative, 2: value_wrong})}
    bm1 = scores[1].components["measures"]["book_to_market"]
    assert bm1["status"] == valorization.EXCLUDED_EQUITY
    assert bm1["flags"] == ["book_equity_nonpositive"]
    bm2 = scores[2].components["measures"]["book_to_market"]
    assert bm2["status"] == valorization.QUARANTINED  # equity > 0, but its value is quarantined
