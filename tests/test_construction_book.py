"""T-135: ``cycle.construction.build_book`` -- the pure N-ticker book (T-134 decisions, FR-007).

Hermetic and DB-free: plain candidates in, a ``BookResult`` out.
"""

from __future__ import annotations

import ast
import itertools
import random
from pathlib import Path

import pytest

from cycle.construction import (
    BookCandidate,
    BookResult,
    VetoStatus,
    _feasible_sector_cap,
    _project_book,
    build_book,
)

TOL = 1e-9
SECTORS = [
    "Financials",
    "Energy",
    "Tech",
    "Health",
    "Utilities",
    "Industrials",
    "Materials",
    "Staples",
]


def cand(
    i: int,
    sector: str | None,
    score: float | None = None,
    veto: VetoStatus = "none",
    vol: float | None = 0.2,
) -> BookCandidate:
    return BookCandidate(
        asset_id=i,
        ticker=f"T{i:03d}",
        blended_score=100.0 - i if score is None else score,
        sector=sector,
        realized_vol_90d=vol,
        veto=veto,
    )


def spread(count: int, sectors: int = 8) -> list[BookCandidate]:
    """*count* names ranked 0..count-1, sectors dealt round-robin."""
    return [cand(i, SECTORS[i % sectors]) for i in range(count)]


def sector_sums(res: BookResult, cands: list[BookCandidate]) -> dict[str | None, float]:
    sector = {c.asset_id: c.sector for c in cands}
    out: dict[str | None, float] = {}
    for aid, w in res.weights.items():
        out[sector[aid]] = out.get(sector[aid], 0.0) + w
    return out


def assert_feasible(
    res: BookResult,
    cands: list[BookCandidate],
    *,
    name_cap: float | None = None,
    sector_cap: float = 0.30,
) -> None:
    """The acceptance: sums to 1, band and both caps hold within 1e-9 -- or the relaxation is recorded."""
    if not res.weights:
        return
    assert abs(sum(res.weights.values()) - 1.0) < TOL
    floor = 0.5 / res.n_held if res.scheme == "score_tilt" else 0.0
    assert all(floor - TOL <= w <= res.max_name_weight + TOL for w in res.weights.values())
    assert all(s <= res.max_sector_weight + TOL for s in sector_sums(res, cands).values())
    relaxed = {r.cap: r for r in res.relaxations}
    if res.max_sector_weight > sector_cap + TOL:
        assert relaxed["sector"].requested == pytest.approx(sector_cap)
        assert relaxed["sector"].effective == pytest.approx(res.max_sector_weight)
    else:
        assert "sector" not in relaxed
    if name_cap is not None and name_cap < 1.0 / res.n_held - TOL:
        assert relaxed["name"].effective == pytest.approx(1.0 / res.n_held)
        assert res.max_name_weight == pytest.approx(1.0 / res.n_held)


# -- N sweep ---------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 3, 5, 10, 20, 30])
def test_every_n_gives_a_feasible_full_book(n: int) -> None:
    cands = spread(60)
    res = build_book(cands, n=n)
    assert res.n_held == n and res.shortfall == 0 and len(res.weights) == n
    assert_feasible(res, cands)
    # the default name cap is 1.5/N, no 0.10 floor (T-134 decision 2)
    assert res.max_name_weight == pytest.approx(min(1.0, 1.5 / n))


def test_n30_caps_a_name_at_five_percent_and_floors_it() -> None:
    res = build_book(spread(60), n=30)
    assert max(res.weights.values()) <= 0.05 + TOL
    assert min(res.weights.values()) >= 0.5 / 30 - TOL  # no near-zero positions


def test_n1_holds_one_name_at_weight_one() -> None:
    res = build_book(spread(5), n=1)
    assert res.weights == {0: pytest.approx(1.0)}
    # one name cannot satisfy a 0.30 sector cap: relaxed to 1.0 and said so
    assert res.max_sector_weight == pytest.approx(1.0)
    assert [r.cap for r in res.relaxations] == ["sector"]


# -- sector-aware fill -----------------------------------------------------------------------


@pytest.mark.parametrize(("n", "full_at"), [(30, 9), (10, 3), (5, 1), (3, 1)])
def test_a_sector_is_full_at_max_one_floor_cap_times_n(n: int, full_at: int) -> None:
    # Financials hold the top 20 ranks; eight other sectors follow
    cands = [cand(i, "Financials") for i in range(20)] + [
        cand(20 + i, SECTORS[1 + i % 7]) for i in range(40)
    ]
    res = build_book(cands, n=n)
    in_fin = sum(1 for c in cands if c.asset_id in res.weights and c.sector == "Financials")
    assert in_fin == full_at
    assert len(res.weights) == n
    assert_feasible(res, cands)


def test_n3_relaxes_the_sector_cap_to_one_third_and_records_it() -> None:
    cands = spread(20)
    res = build_book(cands, n=3)
    assert len(sector_sums(res, cands)) == 3  # one name per sector
    assert res.max_sector_weight == pytest.approx(1 / 3)
    (r,) = res.relaxations
    assert (r.cap, r.requested, r.effective) == ("sector", 0.30, pytest.approx(1 / 3))
    assert r.reason
    assert all(w == pytest.approx(1 / 3) for w in res.weights.values())


def test_n5_holds_one_per_sector_without_any_relaxation() -> None:
    cands = spread(20)
    res = build_book(cands, n=5)
    assert all(
        sum(1 for c in cands if c.asset_id in res.weights and c.sector == s) == 1
        for s in SECTORS[:5]
    )
    assert res.relaxations == ()


def test_production_shape_skips_the_fourth_financial_and_holds_both_caps() -> None:
    """cycle_run 1: 10 names, 4 Financials, 3 Energy held six names at 0.1167 and Energy at 0.35."""
    sectors = ["Financials"] * 4 + ["Energy"] * 3 + ["Tech", "Health", "Utilities"]
    top10 = [cand(i, s) for i, s in enumerate(sectors)]
    # ranks 10.. : the next-ranked name from another sector is the one that gets taken
    cands = [*top10, cand(10, "Materials"), cand(11, "Staples"), cand(12, "Financials")]
    res = build_book(cands, n=10)
    assert 3 not in res.weights  # the fourth Financial is skipped...
    assert 10 in res.weights  # ...and the next-ranked name from another sector is taken
    assert 12 not in res.weights
    assert len(res.weights) == 10
    assert res.relaxations == ()
    sums = sector_sums(res, cands)
    assert sums["Financials"] <= 0.30 + TOL and sums["Energy"] <= 0.30 + TOL
    assert max(res.weights.values()) <= 0.15 + TOL  # not the 0.1167 of the live book
    assert_feasible(res, cands)


def test_single_sector_universe_relaxes_the_sector_cap_to_one() -> None:
    cands = [cand(i, "Energy") for i in range(15)]
    res = build_book(cands, n=10)
    assert len(res.weights) == 10
    assert res.max_sector_weight == pytest.approx(1.0)
    (r,) = res.relaxations
    assert r.cap == "sector" and r.effective == pytest.approx(1.0)
    assert res.overflow_tickers  # fullness was ignored to reach 10 names, and said so
    assert_feasible(res, cands)


def test_two_sector_universe_relaxes_to_one_half() -> None:
    cands = spread(30, sectors=2)
    res = build_book(cands, n=10)
    assert res.max_sector_weight == pytest.approx(0.5)
    assert_feasible(res, cands)


def test_three_sector_universe_at_n30_relaxes_to_the_smallest_feasible_cap() -> None:
    cands = spread(60, sectors=3)
    res = build_book(cands, n=30)
    assert res.max_sector_weight == pytest.approx(1 / 3, abs=1e-9)
    assert_feasible(res, cands)


def test_only_sectors_with_one_sector_gives_sector_cap_one() -> None:
    cands = spread(40)
    res = build_book(cands, n=10, only_sectors=["Tech"])
    assert {c.sector for c in cands if c.asset_id in res.weights} == {"Tech"}
    assert res.max_sector_weight == pytest.approx(1.0)
    assert any(r.cap == "sector" for r in res.relaxations)
    assert_feasible(res, cands)


def test_only_sectors_with_two_sectors_relaxes_to_feasibility() -> None:
    cands = spread(40)
    res = build_book(cands, n=10, only_sectors=["tech", " HEALTH "])  # case/space-insensitive
    assert {c.sector for c in cands if c.asset_id in res.weights} == {"Tech", "Health"}
    assert res.max_sector_weight == pytest.approx(0.5)
    assert_feasible(res, cands)


# -- pins, vetoes, exclusions ----------------------------------------------------------------


def test_a_pin_is_held_first_counts_toward_n_and_obeys_the_caps() -> None:
    cands = spread(40)
    res = build_book(cands, n=10, pins=["T039"])  # the worst-ranked name in the list
    assert 39 in res.weights and len(res.weights) == 10
    assert next(iter(res.weights)) == 39  # pins first
    assert_feasible(res, cands)


def test_many_pins_in_one_sector_are_held_and_the_sector_cap_still_holds() -> None:
    cands = spread(40)
    fin = [c.ticker for c in cands if c.sector == "Financials"][:4]
    res = build_book(
        cands, n=10, pins=fin
    )  # 4 > the fullness threshold of 3: pins are never skipped
    assert all(c.asset_id in res.weights for c in cands if c.ticker in fin)
    assert sector_sums(res, cands)["Financials"] <= 0.30 + TOL
    assert_feasible(res, cands)


def test_a_hard_vetoed_pin_is_refused_with_its_reason_and_never_held() -> None:
    cands = [cand(0, "Tech", veto="HARD")] + [cand(i, SECTORS[i % 8]) for i in range(1, 30)]
    res = build_book(cands, n=10, pins=["T000"])
    assert 0 not in res.weights and len(res.weights) == 10
    (refused,) = res.refused_pins
    assert refused.ticker == "T000" and "HARD" in refused.reason
    assert res.flagged_pins == ()


def test_a_soft_vetoed_pin_is_held_and_flagged() -> None:
    cands = [cand(i, SECTORS[i % 8], veto="SOFT" if i == 25 else "none") for i in range(30)]
    res = build_book(cands, n=10, pins=["T025"])
    assert 25 in res.weights
    (flag,) = res.flagged_pins
    assert flag.ticker == "T025" and "SOFT" in flag.reason
    assert res.refused_pins == ()


def test_a_soft_vetoed_name_that_is_not_pinned_is_an_ordinary_candidate() -> None:
    cands = [cand(i, SECTORS[i % 8], veto="SOFT" if i == 0 else "none") for i in range(30)]
    res = build_book(cands, n=10)
    assert 0 in res.weights and res.flagged_pins == ()


def test_hard_vetoed_candidates_are_never_held() -> None:
    cands = [cand(i, SECTORS[i % 8], veto="HARD" if i < 5 else "none") for i in range(30)]
    res = build_book(cands, n=10)
    assert not set(res.weights) & set(range(5))


def test_an_unknown_pin_is_refused_not_dropped_silently() -> None:
    res = build_book(spread(30), n=10, pins=["NOPE"])
    (refused,) = res.refused_pins
    assert refused.ticker == "nope" and "not among" in refused.reason
    assert len(res.weights) == 10


def test_pins_count_toward_the_fullness_of_their_sector() -> None:
    cands = [cand(i, "Financials") for i in range(10)] + [
        cand(10 + i, SECTORS[1 + i % 7]) for i in range(30)
    ]
    res = build_book(cands, n=10, pins=["T007", "T008", "T009"])  # three low-ranked Financials
    held_fin = [c for c in cands if c.asset_id in res.weights and c.sector == "Financials"]
    assert {c.asset_id for c in held_fin} == {7, 8, 9}  # the threshold of 3 is used up by the pins
    assert len(res.weights) == 10
    assert_feasible(res, cands)


def test_excluded_tickers_and_sectors_are_dropped() -> None:
    cands = spread(40)
    res = build_book(cands, n=10, exclude=["T000", "t001"], exclude_sectors=["energy"])
    held = [c for c in cands if c.asset_id in res.weights]
    assert not {c.ticker for c in held} & {"T000", "T001"}
    assert all(c.sector != "Energy" for c in held)
    assert len(held) == 10
    assert_feasible(res, cands)


def test_excluding_a_pinned_ticker_is_an_error() -> None:
    with pytest.raises(ValueError, match="pin and exclude"):
        build_book(spread(20), n=5, pins=["T003"], exclude=["t003"])


def test_pinning_a_name_in_an_excluded_sector_is_an_error() -> None:
    with pytest.raises(ValueError, match="sector"):
        build_book(spread(20), n=5, pins=["T001"], exclude_sectors=["Energy"])  # T001 is Energy
    with pytest.raises(ValueError, match="sector"):
        build_book(spread(20), n=5, pins=["T001"], only_sectors=["Tech"])


def test_more_pins_than_n_is_an_error() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        build_book(spread(20), n=2, pins=["T001", "T002", "T003"])


# -- explicit overrides, shortfall, degenerate inputs ----------------------------------------


def test_an_explicit_name_cap_wins() -> None:
    cands = spread(60)
    res = build_book(cands, n=20, max_name_weight=0.055)  # default would be 0.075
    assert res.max_name_weight == pytest.approx(0.055)
    assert max(res.weights.values()) <= 0.055 + TOL
    assert res.relaxations == ()
    assert_feasible(res, cands)


def test_an_explicit_sector_cap_wins() -> None:
    cands = spread(60)
    res = build_book(cands, n=20, max_sector_weight=0.20)
    assert max(sector_sums(res, cands).values()) <= 0.20 + TOL
    assert_feasible(res, cands, sector_cap=0.20)


def test_an_explicit_name_cap_below_one_over_n_is_relaxed_and_recorded() -> None:
    cands = spread(60)
    res = build_book(cands, n=10, max_name_weight=0.02)
    assert res.max_name_weight == pytest.approx(0.1)
    (r,) = res.relaxations
    assert (r.cap, r.requested, r.effective) == ("name", 0.02, pytest.approx(0.1))
    assert all(w == pytest.approx(0.1) for w in res.weights.values())  # equal: nothing else fits
    assert_feasible(res, cands, name_cap=0.02)


def test_fewer_eligible_names_than_n_hold_them_all_and_record_the_shortfall() -> None:
    cands = spread(7)
    res = build_book(cands, n=10)
    assert res.n_held == 7 and res.shortfall == 3 and res.requested_n == 10
    assert len(res.weights) == 7  # never padded
    assert res.max_name_weight == pytest.approx(1.5 / 7)  # the band is on the actual count
    assert min(res.weights.values()) >= 0.5 / 7 - TOL
    assert_feasible(res, cands)


def test_vetoes_and_exclusions_count_against_the_eligible_names() -> None:
    cands = [cand(i, SECTORS[i % 8], veto="HARD" if i < 4 else "none") for i in range(12)]
    res = build_book(cands, n=10, exclude=["T004"])
    assert res.n_held == 7 and res.shortfall == 3


def test_nothing_eligible_is_an_empty_book_with_the_full_shortfall() -> None:
    res = build_book([cand(0, "Tech", veto="HARD")], n=5, pins=["T000"])
    assert res.weights == {} and res.n_held == 0 and res.shortfall == 5
    assert res.refused_pins
    assert build_book([], n=5).shortfall == 5


def test_all_scores_equal_gives_equal_weights() -> None:
    cands = [cand(i, SECTORS[i % 5], score=50.0) for i in range(20)]
    res = build_book(cands, n=10)
    assert all(w == pytest.approx(0.1) for w in res.weights.values())


def test_weights_are_linear_in_the_score_where_no_bound_binds() -> None:
    sectors = [f"S{i}" for i in range(10)]  # ten sectors: the 0.30 cap never binds
    # the mean score is the mid-range, so the shift is zero and nothing needs clipping
    scores = [90.0, 80.0, 75.0, 65.0, 60.0, 60.0, 55.0, 45.0, 40.0, 30.0]
    cands = [cand(i, sectors[i], score=scores[i]) for i in range(10)]
    res = build_book(cands, n=10)
    slope = (1.5 - 0.5) / 10 / (max(scores) - min(scores))
    for a, b in itertools.combinations(range(10), 2):
        assert res.weights[a] - res.weights[b] == pytest.approx((scores[a] - scores[b]) * slope)
    assert res.weights[0] == pytest.approx(0.15) and res.weights[9] == pytest.approx(0.05)


def test_a_binding_bound_shifts_the_names_inside_the_band_together() -> None:
    sectors = [f"S{i}" for i in range(10)]
    scores = [90.0, 82.0, 75.0, 71.0, 64.0, 60.0, 52.0, 47.0, 41.0, 30.0]  # mean above mid-range
    cands = [cand(i, sectors[i], score=scores[i]) for i in range(10)]
    res = build_book(cands, n=10)
    slope = (1.5 - 0.5) / 10 / (max(scores) - min(scores))
    inside = [i for i, w in res.weights.items() if 0.05 + TOL < w < 0.15 - TOL]
    assert len(inside) >= 6 and len(inside) < 10  # the top is clipped at the name cap
    for a, b in itertools.combinations(inside, 2):
        assert res.weights[a] - res.weights[b] == pytest.approx((scores[a] - scores[b]) * slope)
    assert_feasible(res, cands)


def test_higher_score_never_gets_a_lower_weight_within_a_sector() -> None:
    cands = spread(60)
    res = build_book(cands, n=30)
    for sector in SECTORS:
        ws = [
            res.weights[c.asset_id]
            for c in cands
            if c.asset_id in res.weights and c.sector == sector
        ]
        assert ws == sorted(ws, reverse=True)  # ranked best first, so weights never rise


# -- legacy schemes --------------------------------------------------------------------------


def test_legacy_schemes_go_through_the_same_projection_and_their_breaches_are_gone() -> None:
    sectors = ["Financials"] * 4 + ["Energy"] * 3 + ["Tech", "Health", "Utilities", "Industrials"]
    cands = [cand(i, s, vol=0.1 + 0.02 * i) for i, s in enumerate(sectors)]
    for scheme in ("equal", "score_proportional", "inverse_vol"):
        res = build_book(cands, n=10, scheme=scheme, max_sector_weight=0.50)
        assert res.max_name_weight == pytest.approx(0.10)  # the legacy default name cap
        assert_feasible(res, cands, sector_cap=0.50)


def test_equal_scheme_at_small_n_relaxes_the_legacy_name_cap() -> None:
    cands = spread(20)
    res = build_book(cands, n=5, scheme="equal")
    assert res.max_name_weight == pytest.approx(0.2)
    assert [r.cap for r in res.relaxations] == ["name"]
    assert_feasible(res, cands, name_cap=0.10)


def test_legacy_schemes_have_no_band_floor() -> None:
    """Only score_tilt has the [0.5/N, 1.5/N] band; the legacy schemes project with floor 0."""
    cands = [cand(i, SECTORS[i % 8], score=100.0 - 5 * i) for i in range(20)]
    res = build_book(cands, n=20, scheme="score_proportional")
    assert min(res.weights.values()) < 0.5 / 20 - TOL
    assert_feasible(res, cands)


def test_inverse_vol_tolerates_a_missing_volatility() -> None:
    cands = [cand(i, SECTORS[i % 8], vol=None if i == 2 else 0.1 + 0.01 * i) for i in range(20)]
    res = build_book(cands, n=12, scheme="inverse_vol", max_name_weight=0.2)
    assert_feasible(res, cands)
    assert all(w > 0 for w in res.weights.values())
    flat = build_book(
        [cand(i, SECTORS[i % 8], vol=None) for i in range(20)], n=8, scheme="inverse_vol"
    )
    assert len(flat.weights) == 8


# -- determinism and validation --------------------------------------------------------------


def test_the_result_does_not_depend_on_the_input_order_and_ties_break_by_ticker() -> None:
    cands = [cand(i, SECTORS[i % 8], score=float(i % 4)) for i in range(40)]  # many ties
    expected = build_book(cands, n=12, pins=["T039"])
    for seed in range(5):
        shuffled = cands[:]
        random.Random(seed).shuffle(shuffled)
        assert build_book(shuffled, n=12, pins=["T039"]) == expected
    assert build_book(cands, n=12, pins=["T039"]) == expected


def test_ties_break_by_ticker_not_by_asset_id() -> None:
    # asset ids run opposite to tickers; every score is tied, so the tickers decide who is held
    cands = [
        BookCandidate(
            asset_id=100 - i, ticker=f"T{i:03d}", blended_score=50.0, sector=SECTORS[i % 8]
        )
        for i in range(20)
    ]
    res = build_book(cands, n=5)
    held = sorted(c.ticker for c in cands if c.asset_id in res.weights)
    assert held == ["T000", "T001", "T002", "T003", "T004"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n": 0},
        {"n": -3},
        {"n": 5, "scheme": "bogus"},
        {"n": 5, "max_sector_weight": 0.0},
        {"n": 5, "max_sector_weight": 1.5},
        {"n": 5, "max_name_weight": 0.0},
        {"n": 5, "max_name_weight": 2.0},
        {"n": 5, "only_sectors": []},
    ],
)
def test_invalid_inputs_are_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build_book(spread(10), **kwargs)  # type: ignore[arg-type]


def test_duplicate_candidates_and_non_finite_scores_are_errors() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_book([cand(1, "Tech"), cand(1, "Energy")], n=1)
    with pytest.raises(ValueError, match="finite"):
        build_book([cand(1, "Tech", score=float("nan"))], n=1)


def test_candidates_without_a_sector_share_one_unclassified_group() -> None:
    cands = [cand(i, None) for i in range(10)] + [cand(10 + i, SECTORS[i]) for i in range(8)]
    res = build_book(cands, n=10)
    assert sum(1 for c in cands if c.asset_id in res.weights and c.sector is None) <= 3
    assert_feasible(res, cands)


def test_construction_imports_nothing_heavy() -> None:
    """cycle must not import quant or a numeric solver (T-135 rule)."""
    src = Path(__file__).resolve().parent.parent / "src" / "cycle" / "construction.py"
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.Import):
            imported.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"quant", "numpy", "scipy", "cvxpy", "clarabel", "pandas"}


# -- the projection is exact -----------------------------------------------------------------


def _dot_gap(t: list[float], w: list[float], z: list[float]) -> float:
    return sum((a - b) * (c - b) for a, b, c in zip(t, w, z, strict=True))


def test_the_projection_is_the_euclidean_projection() -> None:
    """Variational inequality: w = P(t) iff (t - w).(z - w) <= 0 for every feasible z. The z's are
    projections of random targets, hence feasible by construction."""
    rng = random.Random(7)
    for _ in range(300):
        n = rng.randint(2, 14)
        groups = [rng.randint(0, 3) for _ in range(n)]
        lo = rng.choice([0.0, 0.5 / n])
        hi = rng.uniform(1.0 / n, min(1.0, 2.5 / n))
        cap = rng.uniform(0.2, 1.0)
        counts = [groups.count(g) for g in set(groups)]
        # lift the cap to the smallest feasible one, as build_book does
        cap = _feasible_sector_cap(counts, lo, hi, cap)
        t = [rng.uniform(0.0, 2.0 / n) for _ in range(n)]
        w = _project_book(t, groups, lo, hi, cap)
        assert abs(sum(w) - 1.0) < TOL
        assert all(lo - TOL <= x <= hi + TOL for x in w)
        for g in set(groups):
            assert sum(x for x, gg in zip(w, groups, strict=True) if gg == g) <= cap + TOL
        for _ in range(12):
            z = _project_book([rng.uniform(0.0, 2.0 / n) for _ in range(n)], groups, lo, hi, cap)
            assert _dot_gap(t, w, z) <= 1e-9


# -- property test ---------------------------------------------------------------------------


_VETOES: list[VetoStatus] = ["none", "SOFT", "HARD"]


def _random_case(rng: random.Random) -> tuple[list[BookCandidate], dict[str, object]]:
    count = rng.randint(1, 60)
    sectors = rng.randint(1, 8)
    spread_kind = rng.choice(["wide", "tight", "equal", "ties"])
    cands = []
    for i in range(count):
        score = {
            "wide": rng.uniform(0, 100),
            "tight": rng.uniform(49.9, 50.1),
            "equal": 50.0,
            "ties": float(rng.randint(0, 3)),
        }[spread_kind]
        veto = rng.choices(_VETOES, weights=[8, 1, 1])[0]
        sector = None if rng.random() < 0.05 else SECTORS[rng.randrange(sectors)]
        cands.append(cand(i, sector, score=score, veto=veto, vol=rng.choice([None, 0.1, 0.4])))
    kwargs: dict[str, object] = {
        "n": rng.choice([1, 2, 3, 5, 7, 10, 15, 20, 30, 40]),
        "scheme": rng.choice(["score_tilt"] * 4 + ["equal", "score_proportional", "inverse_vol"]),
    }
    if rng.random() < 0.3:
        kwargs["max_name_weight"] = rng.choice([0.02, 0.05, 0.08, 0.12, 0.3, 1.0])
    if rng.random() < 0.3:
        kwargs["max_sector_weight"] = rng.choice([0.1, 0.2, 0.3, 0.5, 1.0])
    pool = [c for c in cands if c.veto != "HARD"]
    if rng.random() < 0.3 and pool:
        kwargs["pins"] = [c.ticker for c in rng.sample(pool, min(len(pool), rng.randint(1, 3)))]
    if rng.random() < 0.2:
        kwargs["exclude"] = [c.ticker for c in rng.sample(cands, min(len(cands), 3))]
    return cands, kwargs


def test_seeded_property_every_book_is_feasible_or_records_its_relaxation() -> None:
    rng = random.Random(20261005)
    checked = 0
    for _ in range(4000):
        cands, kwargs = _random_case(rng)
        try:
            res = build_book(cands, **kwargs)  # type: ignore[arg-type]
        except ValueError:
            # only the documented user errors: more pins than n, or a pin that was also excluded
            assert "pins" in kwargs
            continue
        checked += 1
        assert res.n_held + res.shortfall == kwargs["n"]
        assert len(res.weights) == res.n_held
        assert_feasible(
            res,
            cands,
            name_cap=kwargs.get("max_name_weight"),  # type: ignore[arg-type]
            sector_cap=float(kwargs.get("max_sector_weight", 0.30)),  # type: ignore[arg-type]
        )
        assert build_book(cands, **kwargs) == res  # type: ignore[arg-type]
        held_hard = {c.asset_id for c in cands if c.veto == "HARD"} & set(res.weights)
        assert not held_hard
    assert checked > 3000
