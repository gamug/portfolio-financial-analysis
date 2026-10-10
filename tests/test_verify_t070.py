"""``scripts/verify_t070.py``: the arithmetic and the guards the T-070 verification relies on."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from portfolio_common.db import Database
from verify_t070 import (
    ProductionRefused,
    StintBook,
    absolute_flags,
    breadth,
    fallback_counts,
    liquidity_flags,
    median,
    percentile,
    refuse_production,
    rule_flags,
    spearman,
    trading_days_between,
)

from cycle.rules.base import RuleContext, VetoHit
from cycle.rules.builtin import _LiquidityRule
from cycle.writers import write_vetoes
from kg_schema.trading_calendar import is_trading_day


def test_spearman_is_the_rank_correlation_with_ties_averaged() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4, 5], [1, 3, 2, 5, 4]) == pytest.approx(0.8)
    assert spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)  # ties share a rank
    assert spearman([1, 2], [1, 2]) is None  # under three pairs
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # no spread


def test_percentile_is_nearest_rank_and_median_ignores_empty() -> None:
    values = list(range(1, 101))
    assert percentile(values, 0.95) == 95
    assert percentile(values, 1.0) == 100 and percentile(values, 0.0) == 1
    assert percentile([], 0.5) is None and median([]) is None
    assert median([3, 1, 2]) == 2


def test_trading_days_between_counts_sessions_after_the_start() -> None:
    assert trading_days_between("2026-06-01", "2026-06-15") == 10
    assert trading_days_between("2026-06-15", "2026-06-30") == 10  # Juneteenth is skipped
    assert trading_days_between("2026-06-05", "2026-06-08") == 1  # over a weekend


def test_breadth_counts_dates_over_the_thresholds() -> None:
    shares = [0.0, 0.05, 0.10, 0.11, 0.20, 0.25]
    b = breadth(shares)
    assert (b["over_10pct"], b["over_20pct"], b["cycles"], b["max"]) == (3, 1, 6, 0.25)


def test_the_writing_commands_refuse_production(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProductionRefused):
        refuse_production("/workspaces/thesis/data/financial.db")
    scratch = tmp_path / "scratch.db"  # type: ignore[operator]
    monkeypatch.setenv("KG_FINANCIAL_DB", str(scratch))
    with pytest.raises(ProductionRefused):  # the path the environment names is production
        refuse_production(scratch)
    monkeypatch.setenv("KG_FINANCIAL_DB", "/somewhere/else.db")
    refuse_production(scratch)  # a scratch copy is fine


def test_the_in_memory_stint_book_matches_the_real_writer(memory_db: Database) -> None:
    """``StintBook`` re-implements ``write_vetoes``' temporal semantics without a database, to
    evaluate variants on the same daily cycles; the two must agree on every day."""
    memory_db.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, enabled, created_at) "
        "VALUES ('R1', 'x', 'HARD', 1, '2026-01-01')"
    )
    assets = (1, 2, 3)
    for a in assets:
        memory_db.execute("INSERT INTO assets (id, ticker) VALUES (?, ?)", (a, f"A{a}"))
    memory_db.commit()
    rng = random.Random(7)
    book = StintBook(10)
    day = date(2026, 1, 5)
    for session in range(120):
        while not is_trading_day(day):
            day += timedelta(days=1)
        hit = {a for a in assets if rng.random() < 0.18}
        evaluated = {a for a in assets if rng.random() < 0.95}
        hit &= evaluated
        write_vetoes(
            memory_db,
            day.isoformat(),
            [VetoHit(a, "R1", "HARD", {}) for a in sorted(hit)],
            {"R1": frozenset(evaluated)},
            [],
            run_id=1,
            hold_days={"R1": 10},
        )
        written = {
            int(r["asset_id"])
            for r in memory_db.execute("SELECT asset_id FROM veto WHERE cleared_on IS NULL")
        }
        assert book.step(session, hit, evaluated) == written, day
        day += timedelta(days=1)


def _row(vol: float | None, ret: float | None, **extra: float) -> dict:
    return {
        "obs_date": "2026-07-31",
        "vol_5d": vol,
        "vol_60d_base": 1.0 if vol is not None else None,
        "ret_5d": ret,
        "mu_60d_base": 0.0 if ret is not None else None,
        **extra,
    }


def test_the_absolute_variant_has_no_relative_leg_the_rule_variant_has_one() -> None:
    rows = {a: _row(5.0, -10.0) for a in range(1, 11)}  # everyone shocks together
    assert absolute_flags(rows) == ({*rows}, {*rows})
    sectors = {a: "Utilities" for a in rows}
    assert rule_flags(rows, "2026-07-31", sectors) == (set(), set())
    rows[1] = _row(15.0, -30.0)  # one name stands out from its sector
    vol, crash = rule_flags(rows, "2026-07-31", sectors)
    assert vol == {1} and crash == {1}


def test_fallback_counts_split_sector_groups_from_cross_section_fallbacks() -> None:
    rows = {a: _row(1.0, 0.0) for a in range(1, 10)}
    sectors: dict[int, str | None] = {a: "Big" for a in range(1, 6)}  # 5 names: its own group
    sectors |= {a: "Small" for a in range(6, 9)}  # 3 names: falls back
    sectors[9] = None  # no sector: falls back
    out = fallback_counts(rows, sectors)
    assert out["VOLATILITY_SHOCK.groups"] == 2 and out["VOLATILITY_SHOCK.groups_fallback"] == 1
    assert out["VOLATILITY_SHOCK.names"] == 9 and out["VOLATILITY_SHOCK.names_fallback"] == 4
    assert out["CRASH_Z_SCORE.names_fallback"] == 4


def test_the_sensitivity_helper_agrees_with_the_rule_at_its_default_floors() -> None:
    rule = _LiquidityRule()
    cases = [
        (0.7, 0.9, 0.2),
        (0.7, 8.0, -0.05),
        (0.7, 8.0, 0.2),
        (0.7, None, 0.3),
        (1.2, 0.1, -0.5),
        (0.99, 1.5, 0.0),
    ]
    for ratio, coverage, margin in cases:
        ctx = RuleContext(
            cycle_date="2026-07-31",
            metrics={
                1: {
                    "liquidity.current_ratio": ratio,
                    "leverage.interest_coverage": coverage,
                    "cashflow.operating_cash_flow_margin": margin,
                }
            },
            price_obs={},
            last_fundamental={},
        )
        flagged = bool(rule.evaluate(ctx).hits)
        assert flagged == liquidity_flags(ratio, coverage, margin, 1.5, 0.0), (
            ratio,
            coverage,
            margin,
        )
