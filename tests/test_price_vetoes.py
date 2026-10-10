"""The three price vetoes, their temporal stint semantics and the recalibrated
LIQUIDITY_DISTRESS (T-070)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest
from portfolio_common.db import Database
from test_cycle import _settings
from test_live_book_repair import LATER, _corrupted

from cycle import data
from cycle.orchestrator import run_monitoring
from cycle.repair import apply_undo, plan_undo
from cycle.replay import reset_replay_range
from cycle.rules import RuleContext, enabled_rules, hold_trading_days, seed_catalog
from cycle.rules.base import RuleResult, VetoHit
from cycle.rules.builtin import (
    LIQUIDITY_EXEMPT_SECTORS,
    MAX_OBSERVATION_AGE_DAYS,
    _CrashZRule,
    _LiquidityRule,
    _TrendBreakRule,
    _VolatilityShockRule,
)
from cycle.writers import hard_vetoed_as_of, write_vetoes
from kg_schema.trading_calendar import add_trading_days

TODAY = "2026-07-31"


def _ctx(
    price_obs: Mapping[int, Any],
    sectors: Mapping[int, str | None] | None = None,
    cycle_date: str = TODAY,
) -> RuleContext:
    return RuleContext(
        cycle_date=cycle_date,
        metrics={},
        price_obs={a: dict(r) for a, r in price_obs.items()},
        last_fundamental={},
        sectors=dict(sectors or {}),
    )


def _hit_ids(rule: object, ctx: RuleContext) -> tuple[set[int], frozenset[int]]:
    res = rule.evaluate(ctx)  # type: ignore[attr-defined]
    return {h.asset_id for h in res.hits}, res.evaluated


# -- BREAK_TREND_200 (SOFT): close < 0.95 * sma_200 ---------------------------------------------


def test_break_trend_200_fires_strictly_below_95_percent_of_the_sma() -> None:
    rule = _TrendBreakRule()
    assert (rule.RULE_ID, rule.SEVERITY, hold_trading_days(rule)) == (
        "BREAK_TREND_200",
        "SOFT",
        None,
    )
    edge = 0.95 * 200.0
    obs = {
        1: {"close": edge, "sma_200": 200.0},  # exactly on the line: not below it
        2: {"close": math.nextafter(edge, 0.0), "sma_200": 200.0},  # the next float down
        3: {"close": 210.0, "sma_200": 200.0},
        4: {"close": 150.0, "sma_200": None},  # fewer than 200 closes
        5: {"close": 150.0, "sma_200": 0.0},  # no usable average
    }
    hits, evaluated = _hit_ids(rule, _ctx(obs))
    assert hits == {2}
    assert evaluated == {1, 2, 3}  # 4 and 5 could not be told


def test_a_stale_observation_is_not_evaluated_by_any_price_rule() -> None:
    fresh = {"obs_date": TODAY, "close": 80.0, "sma_200": 100.0}
    edge = f"2026-07-{31 - MAX_OBSERVATION_AGE_DAYS:02d}"  # exactly the allowed age
    stale = "2026-07-23"  # eight days old
    assert (date.fromisoformat(TODAY) - date.fromisoformat(edge)).days == MAX_OBSERVATION_AGE_DAYS
    obs = {
        1: fresh,
        2: {**fresh, "obs_date": edge},
        3: {**fresh, "obs_date": stale},
    }
    hits, evaluated = _hit_ids(_TrendBreakRule(), _ctx(obs))
    assert hits == {1, 2} and evaluated == {1, 2}


# -- VOLATILITY_SHOCK (HARD-temporal): ratio > 2.5 AND ratio > 2.5 x the sector's median ratio ----


def _vol(ratio: float, base: float = 1.0) -> dict[str, float]:
    return {"vol_5d": ratio * base, "vol_60d_base": base}


def _calm_peers(n: int, first: int = 100) -> dict[int, dict[str, float]]:
    """*n* calm names (ratio 1.0, no return) -- the cross-section a shock is judged against."""
    return {first + i: {**_vol(1.0), "ret_5d": 0.0, "mu_60d_base": 0.0} for i in range(n)}


def test_volatility_shock_fires_strictly_above_a_ratio_of_2_5() -> None:
    rule = _VolatilityShockRule()
    assert (rule.RULE_ID, rule.SEVERITY, hold_trading_days(rule)) == (
        "VOLATILITY_SHOCK",
        "HARD",
        10,
    )
    obs = {
        1: _vol(2.5),  # exactly 2.5: not above
        2: _vol(2.5000001),
        3: _vol(0.5),
        4: {"vol_5d": 3.0, "vol_60d_base": 0.0},  # a flat baseline has no ratio
        5: {"vol_5d": None, "vol_60d_base": 1.0},
        **_calm_peers(8),
    }
    hits, evaluated = _hit_ids(rule, _ctx(obs))
    assert hits == {2}
    assert evaluated == {1, 2, 3, *range(100, 108)}
    evidence = rule.evaluate(_ctx(obs)).hits[0].evidence
    assert evidence["ratio"] == pytest.approx(2.5000001)
    assert (evidence["group"], evidence["group_median_ratio"]) == ("cross-section", 1.0)


def test_volatility_shock_also_needs_the_name_to_stand_out_from_its_sector() -> None:
    sectors = {a: "Utilities" for a in range(1, 11)}
    # a sector-wide shock: the whole sector's ratio is 5.0, each name is 1.0x its sector's median
    sector_wide = {a: _vol(5.0) for a in range(1, 11)}
    hits, evaluated = _hit_ids(_VolatilityShockRule(), _ctx(sector_wide, sectors))
    assert hits == set() and evaluated == set(range(1, 11))  # evaluated, and clean
    # one name at 5.0 in a calm sector: idiosyncratic, fires
    lone = {a: _vol(1.0) for a in range(1, 11)} | {1: _vol(5.0)}
    hits, _ = _hit_ids(_VolatilityShockRule(), _ctx(lone, sectors))
    assert hits == {1}


def test_volatility_shocks_relative_threshold_is_strict_at_its_edge() -> None:
    sectors = {a: "Energy" for a in range(1, 8)}
    obs = {a: _vol(2.0) for a in range(2, 8)} | {1: _vol(5.0)}  # sector median 2.0
    assert 5.0 == 2.5 * 2.0  # exactly 2.5x the median: not more than it
    hits, _ = _hit_ids(_VolatilityShockRule(), _ctx(obs, sectors))
    assert hits == set()
    obs[1] = _vol(5.000001)
    hits, _ = _hit_ids(_VolatilityShockRule(), _ctx(obs, sectors))
    assert hits == {1}


def test_a_sector_under_five_names_is_judged_against_the_cross_section() -> None:
    rule = _VolatilityShockRule()
    # Energy has 4 names with a ratio, so name 1 is compared with the whole cross-section's
    # median (1.0), not with Energy's own (which its sector-mates inflate to 4.0)
    sectors = {a: "Energy" for a in range(1, 5)} | {a: "Utilities" for a in range(100, 110)}
    obs = {1: _vol(6.0), 2: _vol(4.0), 3: _vol(4.0), 4: _vol(4.0), **_calm_peers(10)}
    res = rule.evaluate(_ctx(obs, sectors))
    assert {h.asset_id for h in res.hits} == {1, 2, 3, 4}  # all clear 2.5x the 1.0 median
    assert {h.evidence["group"] for h in res.hits} == {"cross-section"}
    # with five Energy names the sector is the group, and its median (4.0) is the bar
    obs[5] = _vol(4.0)
    sectors[5] = "Energy"
    res = rule.evaluate(_ctx(obs, sectors))
    assert {h.asset_id for h in res.hits} == set()  # 6.0 is not above 2.5 x 4.0
    assert res.evaluated >= {1, 2, 3, 4, 5}


def test_an_asset_with_no_sector_uses_the_cross_section() -> None:
    obs = {1: _vol(6.0), **_calm_peers(9)}
    res = _VolatilityShockRule().evaluate(_ctx(obs, {}))
    assert {h.asset_id for h in res.hits} == {1}
    assert res.hits[0].evidence["group_names"] == 10


# -- CRASH_Z_SCORE (HARD-temporal): both the absolute and the sector-relative z below -2.5 ------


def _crash_obs(z: float, *, mu: float = 0.0004, sd: float = 0.012) -> dict[str, float]:
    """An observation whose absolute crash z is *z* by the declared formula."""
    return {"ret_5d": 5 * mu + z * sd * math.sqrt(5), "mu_60d_base": mu, "vol_60d_base": sd}


def test_crash_z_score_fires_strictly_below_minus_2_5() -> None:
    rule = _CrashZRule()
    assert (rule.RULE_ID, rule.SEVERITY, hold_trading_days(rule)) == (
        "CRASH_Z_SCORE",
        "HARD",
        10,
    )
    obs = {
        1: _crash_obs(-2.49),
        2: _crash_obs(-2.51),
        3: _crash_obs(0.5),
        4: {"ret_5d": -0.2, "mu_60d_base": 0.0, "vol_60d_base": 0.0},  # no spread
        5: {"ret_5d": None, "mu_60d_base": 0.0, "vol_60d_base": 0.01},
        6: {"ret_5d": -0.2, "mu_60d_base": None, "vol_60d_base": 0.01},
        **{100 + i: _crash_obs(0.0) for i in range(8)},  # calm peers: a median near 5 mu
    }
    hits, evaluated = _hit_ids(rule, _ctx(obs))
    assert hits == {2}
    assert evaluated == {1, 2, 3, *range(100, 108)}
    evidence = rule.evaluate(_ctx(obs)).hits[0].evidence
    assert evidence["z"] == pytest.approx(-2.51) and evidence["relative_z"] < -2.5


def test_crash_z_score_compares_a_5_day_return_with_the_baseline_scaled_to_5_days() -> None:
    """PLAN's literal ``(R5d - mu60d) / sigma60d`` mixes a 5-day return with daily moments; the
    declared form scales the baseline (mean x 5, sd x sqrt(5)) instead."""
    mu, sd, ret = 0.001, 0.01, -0.05
    literal = (ret - mu) / sd
    declared = (ret - 5 * mu) / (sd * math.sqrt(5))
    assert literal < -2.5 < declared  # the two disagree on this observation
    calm = {100 + i: {"ret_5d": 0.0, "mu_60d_base": mu, "vol_60d_base": sd} for i in range(8)}
    obs = {1: {"ret_5d": ret, "mu_60d_base": mu, "vol_60d_base": sd}, **calm}
    hits, _ = _hit_ids(_CrashZRule(), _ctx(obs))
    assert hits == set()
    obs[1] = {"ret_5d": -0.07, "mu_60d_base": mu, "vol_60d_base": sd}
    hits, _ = _hit_ids(_CrashZRule(), _ctx(obs))
    assert hits == {1}  # (-0.07 - 0.005) / (0.01 sqrt 5) = -3.35


def test_crash_z_score_also_needs_the_name_to_fall_below_its_sector() -> None:
    mu, sd = 0.0, 0.01
    scale = sd * math.sqrt(5)
    sectors = {a: "Financials" for a in range(1, 11)}

    def row(ret: float) -> dict[str, float]:
        return {"ret_5d": ret, "mu_60d_base": mu, "vol_60d_base": sd}

    # a sector-wide fall of 8%: every name is far below its own baseline, none below its sector
    wide = {a: row(-0.08) for a in range(1, 11)}
    hits, evaluated = _hit_ids(_CrashZRule(), _ctx(wide, sectors))
    assert hits == set() and evaluated == set(range(1, 11))
    # the same -8% on one name while its sector is flat: idiosyncratic, fires
    lone = {a: row(0.0) for a in range(1, 11)} | {1: row(-0.08)}
    hits, _ = _hit_ids(_CrashZRule(), _ctx(lone, sectors))
    assert hits == {1}
    # the relative leg is in units of the name's own vol * sqrt(5): -2.5 of them below the median
    # (sector median 0.0), exactly on the line is not below it, a hair past it is
    edge = {a: row(0.0) for a in range(1, 11)}
    edge[1] = row(-2.5 * scale + 1e-9)
    assert _hit_ids(_CrashZRule(), _ctx(edge, sectors))[0] == set()
    edge[1] = row(-2.5 * scale - 1e-6)
    assert _hit_ids(_CrashZRule(), _ctx(edge, sectors))[0] == {1}


def test_crash_z_score_judges_a_small_sector_against_the_cross_section() -> None:
    mu, sd = 0.0, 0.01

    def row(ret: float) -> dict[str, float]:
        return {"ret_5d": ret, "mu_60d_base": mu, "vol_60d_base": sd}

    # four Banks all down 8%; ten calm names elsewhere. Banks has < 5 names: the group is the
    # cross-section (median 0.0), against which each fell 8% -- so all four fire
    sectors = {a: "Banks" for a in range(1, 5)} | {a: "Tech" for a in range(10, 20)}
    obs = {a: row(-0.08) for a in range(1, 5)} | {a: row(0.0) for a in range(10, 20)}
    res = _CrashZRule().evaluate(_ctx(obs, sectors))
    assert {h.asset_id for h in res.hits} == {1, 2, 3, 4}
    assert {h.evidence["group"] for h in res.hits} == {"cross-section"}
    # a fifth Banks name makes Banks its own group: the sector-wide fall no longer fires
    obs[5], sectors[5] = row(-0.08), "Banks"
    assert _hit_ids(_CrashZRule(), _ctx(obs, sectors))[0] == set()


# -- the catalog ----------------------------------------------------------------------------------


def test_the_catalog_holds_the_three_price_rules_with_their_severity_and_hold(
    memory_db: Database,
) -> None:
    seed_catalog(memory_db)
    rows = {
        r["rule_id"]: r
        for r in memory_db.execute("SELECT rule_id, severity, params_json FROM rule_catalog")
    }
    assert rows["BREAK_TREND_200"]["severity"] == "SOFT"
    assert rows["VOLATILITY_SHOCK"]["severity"] == "HARD"
    assert rows["CRASH_Z_SCORE"]["severity"] == "HARD"
    assert json.loads(rows["VOLATILITY_SHOCK"]["params_json"])["hold_trading_days"] == 10
    assert json.loads(rows["CRASH_Z_SCORE"]["params_json"])["hold_trading_days"] == 10
    assert "hold_trading_days" not in json.loads(rows["BREAK_TREND_200"]["params_json"])
    temporal = {r.RULE_ID for r in enabled_rules(memory_db) if hold_trading_days(r)}
    assert temporal == {"VOLATILITY_SHOCK", "CRASH_Z_SCORE"}


def test_reseeding_refreshes_a_rules_description_but_keeps_it_disabled(
    memory_db: Database,
) -> None:
    memory_db.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, params_json, enabled, created_at) "
        "VALUES ('LIQUIDITY_DISTRESS', 'current ratio below 1.0', 'SOFT', '{}', 0, '2026-08-30')"
    )
    memory_db.commit()
    seed_catalog(memory_db)
    row = memory_db.execute(
        "SELECT description, params_json, enabled, created_at FROM rule_catalog "
        "WHERE rule_id = 'LIQUIDITY_DISTRESS'"
    ).fetchone()
    assert "cash flow" in row["description"]
    assert json.loads(row["params_json"])["interest_coverage_floor"] == 1.5
    assert (row["enabled"], row["created_at"]) == (0, "2026-08-30")  # the operator's choice stays


# -- temporal stints: write_vetoes with a hold ------------------------------------------------------

HOLD = {"R1": 10}
R1_HIT = [VetoHit(1, "R1", "HARD", {"z": -3.0})]
EVAL = {"R1": frozenset({1})}


def _seed_temporal(conn: Database) -> None:
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, enabled, created_at) "
        "VALUES ('R1', 'x', 'HARD', 1, '2026-01-01')"
    )
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    conn.commit()


def _cycle(conn: Database, day: str, *, hit: bool, evaluated: bool = True) -> None:
    write_vetoes(
        conn,
        day,
        R1_HIT if hit else [],
        EVAL if evaluated else {"R1": frozenset()},
        [],
        run_id=1,
        hold_days=HOLD,
    )


def _stint(conn: Database) -> dict:
    rows = conn.execute("SELECT * FROM veto ORDER BY id").fetchall()
    assert len(rows) == 1
    return dict(rows[0])


def test_the_calendar_dates_the_tests_use() -> None:
    # 10 NYSE sessions after Mon 2026-06-01 is Mon 06-15; after 06-15, Juneteenth (Fri 06-19)
    # is skipped, so the 10th session is Tue 06-30
    assert add_trading_days(date(2026, 6, 1), 10) == date(2026, 6, 15)
    assert add_trading_days(date(2026, 6, 15), 10) == date(2026, 6, 30)


def test_a_temporal_stint_opens_with_its_expiry(memory_db: Database) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    row = _stint(memory_db)
    assert (row["raised_on"], row["expires_on"], row["cleared_on"]) == (
        "2026-06-01",
        "2026-06-15",
        None,
    )
    assert row["expiry_history_json"] is None


def test_a_non_temporal_stint_has_no_expiry_and_clears_at_once(memory_db: Database) -> None:
    _seed_temporal(memory_db)
    write_vetoes(memory_db, "2026-06-01", R1_HIT, EVAL, [], run_id=1)  # no hold_days
    assert _stint(memory_db)["expires_on"] is None
    write_vetoes(memory_db, "2026-06-02", [], EVAL, [], run_id=2)
    assert _stint(memory_db)["cleared_on"] == "2026-06-02"


def test_the_stint_cannot_clear_before_it_expires_even_when_the_condition_is_gone(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    for day in ("2026-06-02", "2026-06-08", "2026-06-12"):  # evaluated, clean, before 06-15
        _cycle(memory_db, day, hit=False)
        row = _stint(memory_db)
        assert row["cleared_on"] is None and row["expires_on"] == "2026-06-15"
        assert row["last_seen_on"] == "2026-06-01"  # the condition was not confirmed since
    assert hard_vetoed_as_of(memory_db, "2026-06-12") == {1}  # still vetoed throughout


def test_at_the_first_cycle_on_or_after_expiry_a_gone_condition_clears_the_stint(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-06-12", hit=False)  # held
    _cycle(memory_db, "2026-06-15", hit=False)  # the expiry date itself
    row = _stint(memory_db)
    assert row["cleared_on"] == "2026-06-15"
    assert row["expires_on"] == "2026-06-15"  # a cleared stint keeps the expiry it had
    # the T-1 lag is unchanged: active up to, not including, the clear date
    assert hard_vetoed_as_of(memory_db, "2026-06-14") == {1}
    assert hard_vetoed_as_of(memory_db, "2026-06-15") == set()


def test_a_hit_before_expiry_keeps_the_stint_and_does_not_move_the_expiry(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-06-08", hit=True)
    row = _stint(memory_db)
    assert (row["last_seen_on"], row["expires_on"]) == ("2026-06-08", "2026-06-15")
    assert row["expiry_history_json"] is None


def test_a_condition_that_persists_extends_the_expiry_from_that_cycle(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-06-16", hit=True)  # the first cycle after 06-15, still holding
    row = _stint(memory_db)
    assert row["cleared_on"] is None and row["raised_on"] == "2026-06-01"  # the same stint
    assert row["expires_on"] == "2026-07-01"  # 06-16 + 10 sessions (06-19 is a holiday)
    assert json.loads(row["expiry_history_json"]) == [{"on": "2026-06-16", "was": "2026-06-15"}]
    _cycle(memory_db, "2026-06-30", hit=False)  # before the new expiry: held
    assert _stint(memory_db)["cleared_on"] is None
    _cycle(memory_db, "2026-07-01", hit=False)  # at it: clears
    assert _stint(memory_db)["cleared_on"] == "2026-07-01"


def test_an_asset_that_could_not_be_evaluated_is_left_alone_even_past_expiry(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-07-13", hit=False, evaluated=False)
    row = _stint(memory_db)
    assert row["cleared_on"] is None and row["expires_on"] == "2026-06-15"


def test_with_cycles_further_apart_than_the_hold_the_hold_is_one_cycle(
    memory_db: Database,
) -> None:
    """A monthly cadence: the next cycle is already past the expiry, so a gone condition clears
    the stint at once -- and the T-1 lag still keeps the name out of that cycle's ranking."""
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-07-01", hit=False)
    assert _stint(memory_db)["cleared_on"] == "2026-07-01"
    assert hard_vetoed_as_of(memory_db, "2026-06-30") == {1}  # cycle 07-01 ranks at cutoff 06-30
    assert hard_vetoed_as_of(memory_db, "2026-07-01") == set()  # the one after is free


def test_a_stint_with_no_expiry_recorded_is_treated_as_expired(memory_db: Database) -> None:
    _seed_temporal(memory_db)
    write_vetoes(memory_db, "2026-06-01", R1_HIT, EVAL, [], run_id=1)  # opened without a hold
    _cycle(memory_db, "2026-06-02", hit=False)
    assert _stint(memory_db)["cleared_on"] == "2026-06-02"


# -- re-runs and resets restore expires_on exactly -----------------------------------------------


def test_a_same_date_rerun_puts_an_extension_back_and_recomputes(memory_db: Database) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-06-16", hit=True)  # extends to 07-01
    # re-run 06-16, now clean: the extension is undone first, then the stint clears (06-16 >= 06-15)
    _cycle(memory_db, "2026-06-16", hit=False)
    row = _stint(memory_db)
    assert (row["cleared_on"], row["expires_on"], row["expiry_history_json"]) == (
        "2026-06-16",
        "2026-06-15",
        None,
    )
    # re-run once more, hit again: a single extension, not two
    _cycle(memory_db, "2026-06-16", hit=True)
    row = _stint(memory_db)
    assert row["cleared_on"] is None and row["expires_on"] == "2026-07-01"
    assert json.loads(row["expiry_history_json"]) == [{"on": "2026-06-16", "was": "2026-06-15"}]


def _extended_twice(conn: Database) -> None:
    _seed_temporal(conn)
    _cycle(conn, "2026-06-01", hit=True)  # expires 06-15
    _cycle(conn, "2026-06-15", hit=True)  # extends to 06-30
    _cycle(conn, "2026-06-30", hit=True)  # extends to 07-15


def test_the_replay_reset_puts_back_the_expiry_that_stood_before_the_range(
    memory_db: Database,
) -> None:
    _extended_twice(memory_db)
    assert _stint(memory_db)["expires_on"] == "2026-07-15"

    reset_replay_range(memory_db, "2026-06-20")  # undoes the 06-30 extension only
    row = _stint(memory_db)
    assert (row["expires_on"], row["cleared_on"]) == ("2026-06-30", None)
    assert json.loads(row["expiry_history_json"]) == [{"on": "2026-06-15", "was": "2026-06-15"}]
    assert row["last_seen_on"] == "2026-06-01"  # the existing rollback to raised_on

    # replaying from the reset reproduces the original stint exactly
    _cycle(memory_db, "2026-06-30", hit=True)
    row = _stint(memory_db)
    assert row["expires_on"] == "2026-07-15"
    assert json.loads(row["expiry_history_json"])[-1] == {"on": "2026-06-30", "was": "2026-06-30"}


def test_the_replay_reset_undoes_every_extension_on_or_after_the_date(memory_db: Database) -> None:
    _extended_twice(memory_db)
    reset_replay_range(memory_db, "2026-06-15")  # both extensions are dated >= 06-15
    row = _stint(memory_db)
    assert (row["expires_on"], row["expiry_history_json"]) == ("2026-06-15", None)
    reset_replay_range(memory_db, "2026-06-01")  # the stint itself was raised in the range
    assert memory_db.execute("SELECT COUNT(*) FROM veto").fetchone()[0] == 0


def test_the_replay_reset_leaves_an_earlier_extension_alone(memory_db: Database) -> None:
    _extended_twice(memory_db)
    reset_replay_range(memory_db, "2026-07-20")  # after every extension
    assert _stint(memory_db)["expires_on"] == "2026-07-15"


def test_the_replay_reset_reopens_a_cleared_stint_with_the_expiry_it_had(
    memory_db: Database,
) -> None:
    _seed_temporal(memory_db)
    _cycle(memory_db, "2026-06-01", hit=True)
    _cycle(memory_db, "2026-06-16", hit=False)  # clears (06-16 >= 06-15)
    reset_replay_range(memory_db, "2026-06-10")
    row = _stint(memory_db)
    assert (row["cleared_on"], row["expires_on"]) == (None, "2026-06-15")
    _cycle(memory_db, "2026-06-16", hit=False)  # the redo clears it again, identically
    assert _stint(memory_db)["cleared_on"] == "2026-06-16"


def test_undo_run_never_touches_vetoes(memory_db: Database) -> None:
    """``cycle undo-run`` repairs ``portfolio_position`` only; a temporal stint, its expiry and
    its history come through untouched."""
    memory_db.execute(
        "INSERT INTO assets (id, ticker) VALUES (1, 'APA'), (2, 'WFC'), (3, 'BFB'), (4, 'XOM')"
    )
    memory_db.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, enabled, created_at) "
        "VALUES ('R1', 'x', 'HARD', 1, '2026-01-01')"
    )
    memory_db.commit()
    _corrupted(memory_db)
    write_vetoes(memory_db, "2026-09-01", R1_HIT, EVAL, [], run_id=1, hold_days=HOLD)
    write_vetoes(memory_db, LATER, R1_HIT, EVAL, [], run_id=2, hold_days=HOLD)  # extends
    before = [dict(r) for r in memory_db.execute("SELECT * FROM veto")]
    assert before[0]["expiry_history_json"] is not None
    apply_undo(memory_db, plan_undo(memory_db, 2))
    assert [dict(r) for r in memory_db.execute("SELECT * FROM veto")] == before


# -- the cycle reads one observation engine version ----------------------------------------------


def _obs(conn: Database, asset: int, day: str, version: str, **cols: float | None) -> None:
    fields = ["asset_id", "obs_date", "close", "event_time", "computed_at", "engine_version", *cols]
    values = [asset, day, 100.0, day, "2026-07-31T00:00:00Z", version, *cols.values()]
    conn.execute(
        f"INSERT INTO price_observation ({', '.join(fields)}) "  # noqa: S608 - test literals
        f"VALUES ({', '.join('?' * len(fields))})",
        values,
    )
    conn.commit()


def test_the_cycle_reads_only_the_configured_observation_version(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA'), (2, 'BBB')")
    _obs(memory_db, 1, "2026-07-30", "priceobs-v1", realized_vol_90d=0.2)
    _obs(memory_db, 1, "2026-07-30", "priceobs-v2", realized_vol_90d=0.3, sma_200=99.0)
    _obs(memory_db, 1, "2026-07-31", "priceobs-v1", realized_vol_90d=0.9)  # newer, but v1
    _obs(memory_db, 2, "2026-07-29", "priceobs-v2", realized_vol_90d=0.4)
    got = data.latest_price_observation(memory_db, TODAY, "priceobs-v2")
    assert {a: r["obs_date"] for a, r in got.items()} == {1: "2026-07-30", 2: "2026-07-29"}
    assert got[1]["realized_vol_90d"] == 0.3 and got[1]["sma_200"] == 99.0  # never the v1 row
    only_v1 = data.latest_price_observation(memory_db, TODAY, "priceobs-v1")
    assert set(only_v1) == {1} and only_v1[1]["realized_vol_90d"] == 0.9


def test_a_cycle_date_bounds_the_observation_it_reads(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _obs(memory_db, 1, "2026-07-30", "priceobs-v2", realized_vol_90d=0.3)
    _obs(memory_db, 1, "2026-08-03", "priceobs-v2", realized_vol_90d=0.5)  # after the cycle date
    got = data.latest_price_observation(memory_db, TODAY, "priceobs-v2")
    assert got[1]["obs_date"] == "2026-07-30"


def test_missing_the_configured_version_is_refused_not_scored_as_nothing(
    memory_db: Database,
) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    assert data.latest_price_observation(memory_db, TODAY, "priceobs-v2") == {}  # a fresh database
    _obs(memory_db, 1, "2026-07-30", "priceobs-v1", realized_vol_90d=0.2)
    with pytest.raises(data.MissingObservations, match=r"priceobs-v2.*pricing_agent"):
        data.latest_price_observation(memory_db, TODAY, "priceobs-v2")


# -- a whole cycle: the veto, its hold and the T-1 lag -------------------------------------------


def test_a_volatility_shock_vetoes_for_ten_sessions_through_a_real_cycle(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    for a in (1, 2, 4, 5):  # the calm cross-section the shock is judged against
        _obs(conn, a, "2026-07-31", "priceobs-v2", vol_5d=0.01, vol_60d_base=0.01, sma_200=90.0)
    _obs(conn, 3, "2026-07-31", "priceobs-v2", vol_5d=0.05, vol_60d_base=0.01, sma_200=90.0)
    r1 = run_monitoring(_settings(conn), "2026-07-31", conn=conn)
    row = conn.execute(
        "SELECT * FROM veto WHERE rule_id = 'VOLATILITY_SHOCK' AND asset_id = 3"
    ).fetchone()
    assert (row["raised_on"], row["expires_on"], row["severity"]) == (
        "2026-07-31",
        "2026-08-14",
        "HARD",
    )
    assert json.loads(row["evidence_json"])["ratio"] == pytest.approx(5.0)
    # T-1: the veto raised on 07-31 does not touch the 07-31 ranking ...
    assert (
        conn.execute(
            "SELECT vetoed FROM cycle_ranking WHERE cycle_run_id = ? AND asset_id = 3",
            (r1.cycle_run_id,),
        ).fetchone()[0]
        == 0
    )
    # ... and affects 08-03 even though the shock is gone that day (held until 08-14)
    for a in (1, 2, 3, 4, 5):
        _obs(conn, a, "2026-08-03", "priceobs-v2", vol_5d=0.01, vol_60d_base=0.01, sma_200=90.0)
    r2 = run_monitoring(_settings(conn), "2026-08-03", conn=conn)
    open_hard = conn.execute(
        "SELECT COUNT(DISTINCT asset_id) FROM veto WHERE severity = 'HARD' AND cleared_on IS NULL"
    ).fetchone()[0]
    assert r2.vetoed == open_hard  # the held stint still counts as hard-vetoed
    assert r2.vetoed >= 1 and r1.vetoed == r2.vetoed
    row = conn.execute("SELECT cleared_on FROM veto WHERE rule_id = 'VOLATILITY_SHOCK'").fetchone()
    assert row["cleared_on"] is None
    assert (
        conn.execute(
            "SELECT vetoed FROM cycle_ranking WHERE cycle_run_id = ? AND asset_id = 3",
            (r2.cycle_run_id,),
        ).fetchone()[0]
        == 1
    )


# -- LIQUIDITY_DISTRESS: current ratio below 1.0 AND a failed cash-coverage test -----------------


def _liq(
    ratio: float | None,
    *,
    coverage: float | None = None,
    ocf_margin: float | None = None,
) -> dict[str, float | None]:
    return {
        "liquidity.current_ratio": ratio,
        "leverage.interest_coverage": coverage,
        "cashflow.operating_cash_flow_margin": ocf_margin,
    }


def _liquidity(
    metrics: dict[int, dict[str, float | None]], sectors: dict[int, str | None] | None = None
) -> tuple[set[int], frozenset[int]]:
    ctx = RuleContext(
        cycle_date=TODAY,
        metrics=metrics,
        price_obs={},
        last_fundamental={},
        sectors=sectors or {},
    )
    return _hit_ids(_LiquidityRule(), ctx)


def test_liquidity_distress_needs_both_conditions() -> None:
    hits, evaluated = _liquidity(
        {
            1: _liq(0.7, coverage=0.9, ocf_margin=0.2),  # weak interest coverage
            2: _liq(0.7, coverage=8.0, ocf_margin=-0.05),  # operations burn cash
            3: _liq(0.7, coverage=8.0, ocf_margin=0.2),  # below 1.0 but covered: healthy
            4: _liq(1.4, coverage=0.5, ocf_margin=-0.5),  # fine liquidity, weak coverage
            5: _liq(0.7, coverage=None, ocf_margin=0.3),  # coverage unknown, cash positive
            6: _liq(0.7, coverage=None, ocf_margin=None),  # nothing to corroborate it with
            7: _liq(None, coverage=0.5, ocf_margin=-0.5),  # no current ratio at all
        }
    )
    assert hits == {1, 2}
    assert evaluated == {1, 2, 3, 4, 5}  # 6 and 7 could not be told


def test_liquidity_distress_thresholds_are_strict_at_their_edges() -> None:
    hits, _ = _liquidity(
        {
            1: _liq(1.0, coverage=0.5),  # current ratio exactly 1.0 is not below it
            2: _liq(0.99, coverage=1.5, ocf_margin=0.0),  # coverage and margin exactly on the floor
            3: _liq(0.99, coverage=1.4999),
            4: _liq(0.99, ocf_margin=-1e-9),
        }
    )
    assert hits == {3, 4}


def test_liquidity_distress_hit_names_the_failed_tests() -> None:
    ctx = RuleContext(
        cycle_date=TODAY,
        metrics={
            1: _liq(0.5, coverage=0.4, ocf_margin=-0.1),
            2: _liq(0.5, coverage=9.0, ocf_margin=-0.1),
        },
        price_obs={},
        last_fundamental={},
    )
    by_asset = {h.asset_id: h for h in _LiquidityRule().evaluate(ctx).hits}
    assert by_asset[1].evidence["failed"] == ["interest_coverage", "operating_cash_flow_margin"]
    assert by_asset[2].evidence["failed"] == ["operating_cash_flow_margin"]
    assert by_asset[2].severity == "SOFT"


def test_financials_and_utilities_are_exempt_and_never_hit() -> None:
    assert {"Financials", "Utilities"} == LIQUIDITY_EXEMPT_SECTORS
    weak = _liq(0.3, coverage=0.2, ocf_margin=-0.4)
    hits, evaluated = _liquidity(
        {1: weak, 2: weak, 3: weak, 4: weak},
        {1: "Financials", 2: "Utilities", 3: "Real Estate", 4: None},
    )
    assert hits == {3, 4}
    assert evaluated == {1, 2, 3, 4}  # an exempt name is evaluated -- and found clean


def test_an_exemption_closes_a_stint_opened_before_it(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'NEE')")
    seed_catalog(memory_db)
    weak = _liq(0.53, coverage=0.4, ocf_margin=-0.1)
    rule = _LiquidityRule()

    def verdict(sector: str) -> RuleResult:
        return rule.evaluate(RuleContext(TODAY, {1: weak}, {}, {}, sectors={1: sector}))

    first = verdict("Industrials")
    write_vetoes(
        memory_db, "2026-06-01", first.hits, {"LIQUIDITY_DISTRESS": first.evaluated}, [], run_id=1
    )
    assert (
        memory_db.execute("SELECT COUNT(*) FROM veto WHERE cleared_on IS NULL").fetchone()[0] == 1
    )
    second = verdict("Utilities")
    write_vetoes(
        memory_db, "2026-06-08", second.hits, {"LIQUIDITY_DISTRESS": second.evaluated}, [], run_id=2
    )
    assert memory_db.execute("SELECT cleared_on FROM veto").fetchone()[0] == "2026-06-08"


def test_the_pilots_seven_healthy_names_are_no_longer_flagged() -> None:
    """The audit's N7 list, on the latest metrics-v4 filing of each (the 20-asset pilot)."""
    pilot = {
        "APA": (0.95, None, 0.51, "Energy"),
        "NEE": (0.53, None, 0.48, "Utilities"),
        "PG": (0.68, 22.52, 0.22, "Consumer Staples"),
        "PM": (0.98, None, 0.34, "Consumer Staples"),
        "SBAC": (0.17, None, 0.45, "Real Estate"),
        "STZ": (0.91, None, 0.30, "Consumer Staples"),
        "T": (0.97, 3.74, 0.31, "Communication Services"),
    }
    ids = {t: i for i, t in enumerate(pilot, start=1)}
    metrics = {
        ids[t]: _liq(cr, coverage=ic, ocf_margin=om) for t, (cr, ic, om, _s) in pilot.items()
    }
    before = {
        t for t, (cr, *_rest) in pilot.items() if cr < 1.0
    }  # the old rule: current ratio only
    assert before == set(pilot)
    hits, evaluated = _liquidity(metrics, {ids[t]: s[3] for t, s in pilot.items()})
    assert hits == set()
    assert evaluated == set(ids.values())
