"""Selection / monitoring cycle: normalization, scores, rules, ranking, positions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

import cycle.orchestrator as cycle_orchestrator
import kg_schema
from cycle.cli import _print_unscored, build_parser
from cycle.cli import main as cycle_main
from cycle.config import CycleSettings
from cycle.construction import Candidate, target_weights
from cycle.data import TooManyUnscored, too_many_unscored_reason, unscored_assets
from cycle.orchestrator import CycleReport, run_monitoring, run_replay, run_selection
from cycle.replay import out_of_order_replay_reason, reset_replay_range
from cycle.rules import RuleContext, RuleResult, disabled_rule_ids, enabled_rules, seed_catalog
from cycle.rules.base import VetoHit
from cycle.scores import sector, technical, valorization
from cycle.scores.normalize import cross_sectional_z, rank_pct, z_to_score
from cycle.state import ManifestMismatch, _redact, checkpoint, open_cycle
from cycle.writers import (
    OutOfOrderCycle,
    active_soft_vetoes,
    hard_vetoed_as_of,
    out_of_order_reason,
    veto_out_of_order_reason,
    write_vetoes,
)
from kg_schema.provenance import DirtyTree
from kg_schema.queries import StaleAsOf, StaleGateVersion, VetoSchemaStale
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, VersionError
from pricing_agent import db as pricing_db

# -- normalize ----------------------------------------------------------


def test_cross_sectional_z_and_score() -> None:
    z = cross_sectional_z([1.0, 2.0, 3.0, 4.0, 5.0])
    assert z[2] == pytest.approx(0.0, abs=1e-9)
    assert z[0] < 0 < z[-1]
    assert z_to_score(0.0) == 50.0
    assert z_to_score(10.0) == 100.0
    assert z_to_score(-10.0) == 0.0


def test_rank_pct_handles_none_and_direction() -> None:
    asc = rank_pct([10.0, 20.0, 30.0, None])
    assert asc[0] == 0.0 and asc[2] == 1.0 and asc[3] is None
    desc = rank_pct([10.0, 20.0, 30.0], higher_is_better=False)
    assert desc[0] == 1.0 and desc[2] == 0.0


# -- score modules ------------------------------------------------


def test_technical_prefers_momentum_and_low_vol() -> None:
    obs: dict[int, dict[str, float | None]] = {
        1: {
            "momentum_63d": 0.30,
            "momentum_21d": 0.1,
            "realized_vol_90d": 0.15,
            "atr_14": 1.0,
            "close": 100.0,
            "max_drawdown_90d": -0.05,
        },
        2: {
            "momentum_63d": -0.20,
            "momentum_21d": -0.1,
            "realized_vol_90d": 0.60,
            "atr_14": 5.0,
            "close": 100.0,
            "max_drawdown_90d": -0.40,
        },
    }
    scores = {s.asset_id: s.raw_value for s in technical.compute(obs)}
    assert scores[1] > scores[2]


def test_valorization_prefers_cheap_and_profitable() -> None:
    rows: dict[int, dict[str, float | None]] = {
        1: {
            "valuation.free_cash_flow_yield": 0.08,
            "profitability.return_on_equity": 0.30,
            "leverage.debt_to_equity": 0.4,
            "earnings_yield": 0.07,
        },
        2: {
            "valuation.free_cash_flow_yield": 0.01,
            "profitability.return_on_equity": 0.02,
            "leverage.debt_to_equity": 4.0,
            "earnings_yield": 0.01,
        },
    }
    scores = {s.asset_id: s.raw_value for s in valorization.compute(rows)}
    assert scores[1] > scores[2]


def test_valorization_negative_equity_leverage_ranks_worst_not_best() -> None:
    """C2 (docs/model_fixes.md): a negative debt_to_equity (negative book
    equity) must not be inverted into the BEST leverage percentile by
    higher_is_better=False -- it should rank worst, same as very high
    positive leverage would."""
    rows: dict[int, dict[str, float | None]] = {
        1: {  # negative equity, extreme leverage
            "profitability.return_on_equity": 0.15,
            "roic.return_on_invested_capital": 0.10,
            "cashflow.free_cash_flow_margin": 0.10,
            "leverage.debt_to_equity": -38.96,
        },
        2: {  # moderate positive leverage, otherwise identical quality inputs
            "profitability.return_on_equity": 0.15,
            "roic.return_on_invested_capital": 0.10,
            "cashflow.free_cash_flow_margin": 0.10,
            "leverage.debt_to_equity": 1.0,
        },
    }
    scores = {s.asset_id: s.raw_value for s in valorization.compute(rows)}
    assert scores[1] < scores[2]


def test_sector_roll_up_mean_and_deviation() -> None:
    sector_of = {1: 10, 2: 10, 3: 20, 4: None, 5: 10}
    technical_raw = {1: 60.0, 2: 40.0, 3: 90.0, 4: 30.0, 5: 50.0}  # asset 4 has no sector
    technical_norm = {1: 55.0, 2: 45.0, 3: 88.0, 5: 51.0}
    aggs, momentum = sector.roll_up(sector_of, technical_raw, technical_norm)

    by_sector = {a.sector_id: a for a in aggs}
    assert by_sector[10].member_count == 3
    assert by_sector[10].mean_raw == pytest.approx(50.0)  # (60+40+50)/3
    assert by_sector[20].mean_raw == pytest.approx(90.0)
    # per-asset deviation from the sector mean; sums to ~0 within a sector
    assert momentum[1] == pytest.approx(10.0)
    assert momentum[2] == pytest.approx(-10.0)
    assert momentum[3] == pytest.approx(0.0)
    assert 4 not in momentum  # sector-less asset dropped


# -- construction -----------------------------------------------


def test_target_weights_respects_name_and_sector_caps() -> None:
    cands = [
        Candidate(
            asset_id=i, blended_score=100.0 - i, sector_id=1 if i < 4 else 2, realized_vol_90d=0.2
        )
        for i in range(8)
    ]
    w = target_weights(cands, top_n=6, max_name_weight=0.30, max_sector_weight=0.5)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    # caps are enforced iteratively and converge to within rounding
    assert max(w.values()) <= 0.30 + 1e-3
    sector1 = sum(v for aid, v in w.items() if aid < 4)
    assert sector1 <= 0.5 + 1e-3

    # without a binding sector cap, higher score keeps a higher weight
    flat = [
        Candidate(asset_id=i, blended_score=100.0 - 10 * i, sector_id=1, realized_vol_90d=0.2)
        for i in range(5)
    ]
    fw = target_weights(flat, top_n=5, max_name_weight=0.9, max_sector_weight=1.0)
    assert fw[0] > fw[1] > fw[4]


# -- rules ------------------------------------------------------


def test_threshold_and_drawdown_rules(memory_db: Database) -> None:
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": 5.0,
                "cashflow.free_cash_flow_margin": 0.2,
                "liquidity.current_ratio": 2.0,
            },
            2: {
                "leverage.debt_to_equity": 1.0,
                "cashflow.free_cash_flow_margin": -0.1,
                "liquidity.current_ratio": 0.7,
            },
        },
        price_obs={2: {"max_drawdown_90d": -0.50}},
        last_fundamental={1: "2026-03-31", 2: None},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits
    assert (2, "NEGATIVE_FCF", "HARD") in hits
    assert (2, "LIQUIDITY_DISTRESS", "SOFT") in hits
    assert (2, "PRICE_CRASH", "SOFT") in hits
    assert (2, "EARNINGS_MISSING", "SOFT") in hits


def test_leverage_rule_hard_vetoes_negative_equity_with_high_net_debt_to_ebitda(
    memory_db: Database,
) -> None:
    """T-116 (docs/model_fixes.md): SBAC-shaped negative book equity -- a
    negative debt_to_equity would trivially evade a plain `> 3.0` check, but
    a high net_debt_to_ebitda still triggers the veto."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -2.75,
                "leverage.net_debt_to_ebitda": 7.35,
                "leverage.interest_coverage": None,
            },
        },
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits


def test_leverage_rule_hard_vetoes_negative_equity_with_low_interest_coverage(
    memory_db: Database,
) -> None:
    """Proves net_debt_to_ebitda and interest_coverage are each independently
    sufficient (an OR, not an AND)."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -10.0,
                "leverage.net_debt_to_ebitda": 2.0,  # healthy
                "leverage.interest_coverage": 1.0,  # < 1.5
            },
        },
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits


def test_leverage_rule_spares_negative_equity_with_healthy_debt_load(
    memory_db: Database,
) -> None:
    """Not "any negative equity = HARD": MCD-shaped negative equity -- a
    healthy net_debt_to_ebitda/interest_coverage, including exactly at the
    calibrated thresholds (strict inequalities, not >=/<=), doesn't veto."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -5.0,
                "leverage.net_debt_to_ebitda": 5.0,  # exactly at threshold, not >
                "leverage.interest_coverage": 1.5,  # exactly at threshold, not <
            },
        },
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert not any(h[1] == "LEVERAGE_EXTREME" for h in hits)


def test_leverage_rule_negative_equity_with_no_corroborating_metrics_soft_vetoes(
    memory_db: Database,
) -> None:
    """T-116: if both net_debt_to_ebitda and interest_coverage are missing, a
    negative debt_to_equity can't be verified either way -- routed to SOFT
    review, not silently passed (the pre-T-116 gap)."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={1: {"leverage.debt_to_equity": -38.96}},
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "SOFT") in hits
    assert not any(h[1] == "LEVERAGE_EXTREME" and h[2] == "HARD" for h in hits)


def test_leverage_rule_treats_a_negative_net_debt_to_ebitda_as_unresolved_not_healthy(
    memory_db: Database,
) -> None:
    """T-116 (PR #95 review): the rule only ever sees the already-divided ratio, never the raw
    EBITDA/net debt it came from, so it cannot tell a genuine net-cash position (negative
    ratio, healthy) apart from negative EBITDA with positive net debt (negative ratio, the most
    distressed profile of all, WAT-shaped). A negative ratio is therefore unresolved, falling
    back to interest_coverage alone -- SOFT review here, since neither is truly available."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {"leverage.debt_to_equity": -5.0, "leverage.net_debt_to_ebitda": -399.0},
        },
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "SOFT") in hits
    assert not any(h[1] == "LEVERAGE_EXTREME" and h[2] == "HARD" for h in hits)


def test_leverage_rule_a_negative_net_debt_to_ebitda_still_hard_vetoes_via_interest_coverage(
    memory_db: Database,
) -> None:
    """The negative ratio contributes nothing either way, but interest_coverage is a
    well-defined signal on its own and still fires."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -5.0,
                "leverage.net_debt_to_ebitda": -399.0,
                "leverage.interest_coverage": 1.0,
            },
        },
        price_obs={},
        last_fundamental={1: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits


def test_leverage_rule_positive_debt_to_equity_path_unchanged(memory_db: Database) -> None:
    """Confirms the pre-existing positive-value threshold behavior survives
    the _ThresholdRule -> _LeverageRule swap (same values as
    test_threshold_and_drawdown_rules's LEVERAGE_EXTREME case)."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={1: {"leverage.debt_to_equity": 5.0}, 2: {"leverage.debt_to_equity": 1.0}},
        price_obs={},
        last_fundamental={1: "2026-03-31", 2: "2026-03-31"},
    )
    hits = [
        (h.asset_id, h.rule_id, h.severity)
        for r in enabled_rules(memory_db)
        for h in r.evaluate(ctx)
    ]  # type: ignore[attr-defined]
    assert (1, "LEVERAGE_EXTREME", "HARD") in hits
    assert not any(h[0] == 2 and h[1] == "LEVERAGE_EXTREME" for h in hits)


# -- full cycle smoke -----------------------------------------


def _settings(conn: Database) -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3)


def test_selection_cycle_end_to_end(cycle_seed: Database) -> None:
    conn = cycle_seed
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)

    assert report.cycle_type == "SELECTION"
    assert "positions" in report.steps_run
    run_row = conn.execute("SELECT status FROM cycle_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run_row["status"] == "completed"

    for stype in ("TECHNICAL", "VALORIZATION", "SECTOR"):
        n = conn.execute(
            "SELECT COUNT(*) FROM score_snapshot WHERE score_type = ? AND event_time = '2026-06-30' "
            "AND normalized_score IS NOT NULL",
            (stype,),
        ).fetchone()[0]
        assert n == 5

    # one sector_aggregate_snapshot row per sector present in the cohort
    agg = conn.execute(
        "SELECT sector_name, member_count FROM v_sector_aggregate_snapshot "
        "WHERE cycle_date = '2026-06-30' ORDER BY sector_name"
    ).fetchall()
    assert [(r["sector_name"], r["member_count"]) for r in agg] == [("S1", 2), ("S2", 3)]

    ranked = conn.execute(
        "SELECT ticker, rank, selected FROM v_cycle_ranking ORDER BY rank"
    ).fetchall()
    assert next(r["ticker"] for r in ranked) == "AAA"  # strongest on every factor
    assert sum(r["selected"] for r in ranked) == 3
    assert (
        conn.execute("SELECT COUNT(*) FROM portfolio_position WHERE valid_to IS NULL").fetchone()[0]
        == 3
    )


def test_cycle_resumes_without_duplicating(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    before = conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0]

    again = run_selection(_settings(conn), "2026-06-30", conn=conn)
    assert set(again.steps_skipped) >= {"technical", "valorization", "rank", "positions"}
    assert conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 3


# -- T-097: the out-of-order-cycle guard -------------------------


def test_out_of_order_reason_pure_function(memory_db: Database) -> None:
    assert out_of_order_reason(memory_db, "2020-01-01") is None  # no rows yet -- nothing to guard
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    memory_db.execute(
        "INSERT INTO portfolio_position (asset_id, valid_from, valid_to, weight, opened_by_cycle) "
        "VALUES (1, '2026-06-30', NULL, 0.5, 1)"
    )
    memory_db.commit()
    assert out_of_order_reason(memory_db, "2026-07-01") is None  # newer -- fine
    assert out_of_order_reason(memory_db, "2026-06-30") is None  # same date -- fine
    reason = out_of_order_reason(memory_db, "2026-05-01")
    assert reason is not None
    assert "2026-05-01" in reason and "2026-06-30" in reason and "--allow-backdated" in reason


def test_select_refuses_to_write_a_backdated_book(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    before = conn.execute(
        "SELECT id, valid_from, valid_to FROM portfolio_position ORDER BY id"
    ).fetchall()

    # allow_backdated_veto: this test is about the *positions* guard (T-097) specifically --
    # the 2026-06-30 run also opened veto stints, so without it the (separate, T-125) veto
    # out-of-order guard would refuse first and mask the assertion below.
    settings = _settings(conn).model_copy(update={"allow_backdated_veto": True})
    with pytest.raises(OutOfOrderCycle, match="2026-06-30"):
        run_selection(settings, "2026-05-01", conn=conn)

    # the live book is untouched -- refused before any write, not partially applied
    after = conn.execute(
        "SELECT id, valid_from, valid_to FROM portfolio_position ORDER BY id"
    ).fetchall()
    assert [dict(r) for r in after] == [dict(r) for r in before]
    # the refused run's own cycle_run is marked "failed", not left stuck "running" (T-097 review)
    assert (
        conn.execute("SELECT status FROM cycle_run WHERE cycle_date = '2026-05-01'").fetchone()[
            "status"
        ]
        == "failed"
    )
    step_status = conn.execute(
        "SELECT cc.status FROM cycle_checkpoint cc "
        "JOIN cycle_run cr ON cr.id = cc.cycle_run_id "
        "WHERE cr.cycle_date = '2026-05-01' AND cc.step = 'positions'"
    ).fetchone()["status"]
    assert step_status == "failed"


def test_allow_backdated_cannot_end_newer_open_positions(cycle_seed: Database) -> None:
    """T-104: the override passes T-097's guard, but closing or re-weighting a stint opened
    after the run's date would end it before it started -- refused before any write."""
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    before = [dict(r) for r in conn.execute("SELECT * FROM portfolio_position ORDER BY id")]

    backdated = _settings(conn).model_copy(
        update={"allow_backdated_positions": True, "allow_backdated_veto": True}
    )
    with pytest.raises(OutOfOrderCycle, match="would end positions opened later"):
        run_selection(backdated, "2026-05-01", conn=conn)
    assert [dict(r) for r in conn.execute("SELECT * FROM portfolio_position ORDER BY id")] == before


def test_allow_backdated_overrides_the_guard_and_records_it(cycle_seed: Database) -> None:
    """With no newer stint still open, the override writes and records why it was needed."""
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    conn.execute("UPDATE portfolio_position SET valid_to = '2026-07-15'")  # the book was closed
    conn.commit()

    backdated = _settings(conn).model_copy(
        update={"allow_backdated_positions": True, "allow_backdated_veto": True}
    )
    report = run_selection(backdated, "2026-05-01", conn=conn)

    assert "positions" in report.steps_run
    assert report.backdated_guard_bypassed is not None
    assert "2026-05-01" in report.backdated_guard_bypassed
    assert "2026-06-30" in report.backdated_guard_bypassed
    # the guard's own MAX(valid_from) read still reflects the later, now-closed positions --
    # a closed position's valid_from still marks a date this book has already moved past.
    assert out_of_order_reason(conn, "2026-04-01") is not None


def test_allow_backdated_veto_is_read_only_not_a_history_rewrite(cycle_seed: Database) -> None:
    """PR #103 review: ``write_vetoes`` only ever undoes/redoes *its own* cycle_date's own
    rows (T-125 f) -- called at an older, already-superseded date under the override, it
    would instead delete the stint(s) raised on the latest date and roll back last_seen_on
    on any stint still open from before it, rewriting history rather than replaying it (a
    reproduced case made ``hard_vetoed_as_of`` retroactively stop seeing an asset as
    vetoed). The override must make the veto step read-only: it still runs (so ``vetoed``
    reflects what today's rules say), but never calls ``write_vetoes``."""
    conn = cycle_seed
    run_selection(_settings(conn), "2026-06-30", conn=conn)  # CCC/DDD/EEE breach LEVERAGE_EXTREME
    before = [dict(r) for r in conn.execute("SELECT * FROM veto ORDER BY id")]
    assert before

    backdated = _settings(conn).model_copy(update={"allow_backdated_veto": True})
    # MONITORING, not SELECTION: isolates the veto guard from T-097's separate positions guard.
    report = run_monitoring(backdated, "2026-05-01", conn=conn)

    assert report.veto_backdated_bypassed is not None
    assert "veto" in report.steps_run
    after = [dict(r) for r in conn.execute("SELECT * FROM veto ORDER BY id")]
    assert after == before  # untouched -- the step ran read-only, it did not rewrite history


# -- T-115: cycle backfill replays into its own book, isolated from the live one --------


def test_out_of_order_replay_reason_pure_function(memory_db: Database) -> None:
    assert (
        out_of_order_replay_reason(memory_db, "2020-01-01") is None
    )  # no rows -- nothing to guard
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    memory_db.execute(
        "INSERT INTO portfolio_position_replay (asset_id, valid_from, valid_to, weight, "
        "opened_by_cycle) VALUES (1, '2026-06-30', NULL, 0.5, 1)"
    )
    memory_db.commit()
    assert out_of_order_replay_reason(memory_db, "2026-07-01") is None  # newer -- fine
    assert out_of_order_replay_reason(memory_db, "2026-06-30") is None  # same date -- fine
    reason = out_of_order_replay_reason(memory_db, "2026-05-01")
    assert reason is not None
    assert "2026-05-01" in reason and "2026-06-30" in reason and "--force" in reason


def test_replay_writes_the_replay_book_not_the_live_one(cycle_seed: Database) -> None:
    conn = cycle_seed
    report = run_replay(_settings(conn), "2026-05-01", conn=conn)

    assert report.cycle_type == "REPLAY"
    assert "positions" in report.steps_run
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 3
    run_row = conn.execute(
        "SELECT cycle_type, status FROM cycle_run WHERE cycle_date = '2026-05-01'"
    ).fetchone()
    assert (run_row["cycle_type"], run_row["status"]) == ("REPLAY", "completed")


def test_replay_never_conflicts_with_an_existing_live_book(cycle_seed: Database) -> None:
    """The old backfill, which called the live `run_selection`, would have been refused by
    T-097 the moment a newer live entry existed. A replay is refused only against its own
    (isolated) book, so it can freely replay a date earlier than the live book's latest --
    for *positions*: `portfolio_position_replay` is fully isolated (T-115). `veto` is not
    (T-125's own g) -- it is one shared stints table read by both live and REPLAY runs --
    so `allow_backdated_veto` stands in here for what a real backfill does with `--force`
    against its own (always-a-copy, per `_refuse_production_backfill`) database."""
    conn = cycle_seed
    run_selection(_settings(conn), "2026-07-31", conn=conn)  # the live book's latest so far

    replay_settings = _settings(conn).model_copy(update={"allow_backdated_veto": True})
    report = run_replay(replay_settings, "2026-05-01", conn=conn)  # older than the live book

    assert "positions" in report.steps_run
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 3
    # the live book itself is untouched by the replay
    live = conn.execute(
        "SELECT COUNT(*) FROM portfolio_position WHERE valid_to IS NULL"
    ).fetchone()[0]
    assert live == 3


def test_replay_resumes_without_duplicating(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_replay(_settings(conn), "2026-05-01", conn=conn)
    before = conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0]

    again = run_replay(_settings(conn), "2026-05-01", conn=conn)

    assert "positions" in again.steps_skipped
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == before


def test_veto_guard_does_not_block_resuming_an_already_completed_date(
    cycle_seed: Database,
) -> None:
    """PR #103 review: the veto out-of-order guard used to run unconditionally at the top of
    ``_run``, before ``done_steps`` was even read -- so resuming ``cycle backfill --from F
    --to T`` after it was killed refused outright at F, even though every step there
    (``veto`` included) was already ``done`` and no write would happen. Moved inside
    ``_veto()`` (mirroring the positions guard, T-097), it must fire only when the step is
    actually about to execute, never on a pure resume."""
    conn = cycle_seed
    first = run_replay(_settings(conn), "2026-06-30", conn=conn)
    assert "veto" in first.steps_run  # CCC/DDD/EEE breach LEVERAGE_EXTREME -- real stints open

    # a later replay date extends those same open stints (last_seen_on moves past F)
    later = run_replay(_settings(conn), "2026-07-07", conn=conn)
    assert "veto" in later.steps_run
    assert veto_out_of_order_reason(conn, "2026-06-30") is not None  # sanity: F is now "old"

    resumed = run_replay(_settings(conn), "2026-06-30", conn=conn)  # must not raise

    assert "veto" in resumed.steps_skipped
    assert "positions" in resumed.steps_skipped


def test_replay_refuses_a_new_older_date_without_force(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_replay(_settings(conn), "2026-06-01", conn=conn)  # the replay book's latest so far

    with pytest.raises(OutOfOrderCycle, match="--force"):
        run_replay(_settings(conn), "2026-05-01", conn=conn)  # new, never-replayed, older date

    # refused before any write -- the replay book is exactly as the first run left it
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 3


def test_reset_replay_range_lets_a_completed_date_be_recomputed(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_replay(_settings(conn), "2026-05-01", conn=conn)

    reset_replay_range(conn, "2026-05-01")
    again = run_replay(_settings(conn), "2026-05-01", conn=conn)

    assert "positions" in again.steps_run  # recomputed, not skipped as already done
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position_replay").fetchone()[0] == 3


def test_reset_replay_range_leaves_earlier_positions_untouched(cycle_seed: Database) -> None:
    conn = cycle_seed
    run_replay(_settings(conn), "2026-05-01", conn=conn)
    earlier = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM portfolio_position_replay WHERE valid_from = '2026-05-01' ORDER BY id"
        )
    ]
    assert earlier  # the earlier date's stints exist to begin with
    run_replay(_settings(conn), "2026-06-01", conn=conn)

    reset_replay_range(conn, "2026-06-01")

    assert (
        conn.execute(
            "SELECT COUNT(*) FROM portfolio_position_replay WHERE valid_from = '2026-06-01'"
        ).fetchone()[0]
        == 0
    )
    still_there = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM portfolio_position_replay WHERE valid_from = '2026-05-01' ORDER BY id"
        )
    ]
    assert still_there == earlier
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date = '2026-05-01'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date = '2026-06-01'"
        ).fetchone()[0]
        == 0
    )


def test_reset_replay_range_resets_through_the_end_of_the_book(cycle_seed: Database) -> None:
    """T-115 review: a replay is path-dependent -- a stint past --to must reset too, or
    redoing --from immediately trips the out-of-order-replay guard against it, half-deleted."""
    conn = cycle_seed
    run_replay(_settings(conn), "2026-06-30", conn=conn)
    run_replay(_settings(conn), "2026-07-08", conn=conn)  # past the --to a bounded reset would use

    reset_replay_range(conn, "2026-06-30")
    again = run_replay(_settings(conn), "2026-06-30", conn=conn)  # must not raise OutOfOrderCycle

    assert "positions" in again.steps_run
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM cycle_run WHERE cycle_type = 'REPLAY' AND cycle_date = '2026-07-08'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT MAX(valid_from) FROM portfolio_position_replay").fetchone()[0]
        == "2026-06-30"
    )


def test_force_flag_exists_on_backfill_only() -> None:
    parser = build_parser()
    assert (
        parser.parse_args(["backfill", "--from", "2026-01-01", "--to", "2026-02-01"]).force is False
    )
    assert parser.parse_args(
        ["backfill", "--from", "2026-01-01", "--to", "2026-02-01", "--force"]
    ).force
    for command in ("select", "monitor"):
        assert not hasattr(parser.parse_args([command]), "force")


def test_backfill_refuses_without_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-115 review: only `positions` is isolated -- every other REPLAY step still writes the
    shared database, so --db is mandatory (a copy), never inferred from KG_FINANCIAL_DB."""
    monkeypatch.delenv("KG_FINANCIAL_DB", raising=False)
    monkeypatch.delenv("KG_FINANTIAL_DB", raising=False)
    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)

    assert cycle_main(["backfill", "--from", "2026-01-01", "--to", "2026-01-01"]) == 1
    assert "--db" in capsys.readouterr().err


def test_backfill_refuses_the_production_database_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    prod = tmp_path / "prod.db"
    monkeypatch.setenv("KG_FINANCIAL_DB", str(prod))
    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)

    assert (
        cycle_main(["backfill", "--from", "2026-01-01", "--to", "2026-01-01", "--db", str(prod)])
        == 1
    )
    assert "production database" in capsys.readouterr().err


def test_backfill_runs_against_an_explicit_non_production_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prod = tmp_path / "prod.db"
    copy = tmp_path / "copy.db"
    monkeypatch.setenv("KG_FINANCIAL_DB", str(prod))
    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)
    calls: list[str] = []

    def _stub_run_replay(_settings: CycleSettings, d: str, **_k: object) -> CycleReport:
        calls.append(d)
        return CycleReport(1, "REPLAY", d, selected=0)

    monkeypatch.setattr("cycle.cli.run_replay", _stub_run_replay)

    assert (
        cycle_main(["backfill", "--from", "2026-01-01", "--to", "2026-01-01", "--db", str(copy)])
        == 0
    )
    assert calls == ["2026-01-01"]


def test_backfill_refuses_a_relative_path_to_the_production_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """T-115 review: a plain string comparison let ``--db data/financial.db`` slip past a
    refusal keyed on the full ``KG_FINANCIAL_DB`` path -- ``os.path.samefile`` catches it."""
    prod = tmp_path / "data" / "financial.db"
    prod.parent.mkdir()
    prod.touch()
    monkeypatch.setenv("KG_FINANCIAL_DB", str(prod))
    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)
    monkeypatch.chdir(tmp_path)

    assert (
        cycle_main(
            [
                "backfill",
                "--from",
                "2026-01-01",
                "--to",
                "2026-01-01",
                "--db",
                "data/financial.db",
            ]
        )
        == 1
    )
    assert "production database" in capsys.readouterr().err


def test_backfill_refuses_a_symlink_to_the_production_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    prod = tmp_path / "prod.db"
    prod.touch()
    link = tmp_path / "link.db"
    link.symlink_to(prod)
    monkeypatch.setenv("KG_FINANCIAL_DB", str(prod))
    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)

    assert (
        cycle_main(["backfill", "--from", "2026-01-01", "--to", "2026-01-01", "--db", str(link)])
        == 1
    )
    assert "production database" in capsys.readouterr().err


# -- T-110: the price-spine guard ---------------------------------


def _seed_price_daily(conn: Database, as_of: str) -> None:
    """``price_daily`` is pricing_agent's own table; ``cycle_seed`` only seeds
    ``price_observation``. In production the two packages share one DB, so
    ``price_daily`` already exists by the time a cycle runs -- create it here too."""
    pricing_db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, open, high, low, close, volume) "
        "VALUES (1, ?, 100, 101, 99, 100, 1000000)",
        (as_of,),
    )
    conn.commit()


def test_select_refuses_a_cycle_date_past_the_price_spine(cycle_seed: Database) -> None:
    conn = cycle_seed
    _seed_price_daily(conn, "2026-06-30")
    before = conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0]

    with pytest.raises(StaleAsOf, match=r"2026-07-01.*2026-06-30"):
        run_selection(_settings(conn), "2026-07-01", conn=conn)

    # refused before any cycle_run row is written, like the manifest-mismatch precheck
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == before


def test_monitor_also_refuses_a_stale_cycle_date(cycle_seed: Database) -> None:
    """Both cycle types read prices for TECHNICAL/veto, so both are guarded."""
    conn = cycle_seed
    _seed_price_daily(conn, "2026-06-30")
    with pytest.raises(StaleAsOf, match="past price_daily's last stored date"):
        run_monitoring(_settings(conn), "2026-07-01", conn=conn)


def test_a_cycle_date_at_or_before_the_price_spine_is_safe(cycle_seed: Database) -> None:
    conn = cycle_seed
    _seed_price_daily(conn, "2026-06-30")
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)  # equal -- fine
    assert report.stale_price_bypassed is None


def test_allow_stale_prices_overrides_the_price_spine_guard_and_records_it(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    _seed_price_daily(conn, "2026-06-30")
    settings = _settings(conn).model_copy(update={"allow_stale_prices": True})

    report = run_selection(settings, "2026-07-01", conn=conn)

    assert "positions" in report.steps_run
    assert report.stale_price_bypassed is not None
    assert "2026-07-01" in report.stale_price_bypassed
    assert "2026-06-30" in report.stale_price_bypassed


# -- T-116 (PR #95 review): the Ring-1 gate-version guard -----------------------------------


def _dq_issue(conn: Database, filing_id: int, asset_id: int, gate_version: str) -> None:
    conn.execute(
        "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
        "metric_engine_version, rule_id, severity, quarantined, gate_version, created_at) "
        "VALUES (?, ?, 'leverage', 'debt_to_equity', 'metrics-v1', 'DQ_NEG_EQUITY', 'SOFT', 1, "
        "?, '2026-01-01T00:00:00Z')",
        (filing_id, asset_id, gate_version),
    )
    conn.commit()


def test_select_refuses_when_the_gate_only_has_an_older_version_s_rows(
    cycle_seed: Database,
) -> None:
    conn = cycle_seed
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _dq_issue(conn, fid, 1, "dq-v1")
    before = conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0]

    with pytest.raises(StaleGateVersion, match=r"dq-v1.*dq-v2"):
        run_selection(_settings(conn), "2026-06-30", conn=conn)

    # refused before any cycle_run row is written, like the other prechecks
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == before


def test_monitor_also_refuses_a_stale_gate_version(cycle_seed: Database) -> None:
    conn = cycle_seed
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _dq_issue(conn, fid, 1, "dq-v1")
    with pytest.raises(StaleGateVersion):
        run_monitoring(_settings(conn), "2026-06-30", conn=conn)


def test_no_data_quality_rows_at_all_is_safe(cycle_seed: Database) -> None:
    """Ring-1 never having run is a bootstrap situation, not a version regression."""
    conn = cycle_seed
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)
    assert report.stale_dq_gate_bypassed is None


def test_rows_already_under_the_current_gate_version_are_safe(cycle_seed: Database) -> None:
    conn = cycle_seed
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _dq_issue(conn, fid, 1, "dq-v1")
    _dq_issue(conn, fid, 1, DATA_QUALITY_GATE_VERSION)
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)
    assert report.stale_dq_gate_bypassed is None


def test_allow_stale_dq_gate_overrides_the_guard_and_records_it(cycle_seed: Database) -> None:
    conn = cycle_seed
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _dq_issue(conn, fid, 1, "dq-v1")
    settings = _settings(conn).model_copy(update={"allow_stale_dq_gate": True})

    report = run_selection(settings, "2026-06-30", conn=conn)

    assert "positions" in report.steps_run
    assert report.stale_dq_gate_bypassed is not None
    assert "dq-v1" in report.stale_dq_gate_bypassed


# -- T-114: the clean-tree guard -----------------------------------------------------------


def test_select_refuses_a_dirty_code_version(
    cycle_seed: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = cycle_seed
    monkeypatch.setattr(cycle_orchestrator, "code_version", lambda: "deadbee-dirty")
    before = conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0]

    with pytest.raises(DirtyTree, match="deadbee-dirty"):
        run_selection(_settings(conn), "2026-06-30", conn=conn)

    # refused before any cycle_run row is written, like the price-spine/manifest prechecks
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == before


def test_monitor_also_refuses_a_dirty_code_version(
    cycle_seed: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = cycle_seed
    monkeypatch.setattr(cycle_orchestrator, "code_version", lambda: "deadbee-dirty")
    with pytest.raises(DirtyTree, match="deadbee-dirty"):
        run_monitoring(_settings(conn), "2026-06-30", conn=conn)


def test_allow_dirty_overrides_the_clean_tree_guard_and_records_it(
    cycle_seed: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = cycle_seed
    monkeypatch.setattr(cycle_orchestrator, "code_version", lambda: "deadbee-dirty")
    settings = _settings(conn).model_copy(update={"allow_dirty": True})

    report = run_selection(settings, "2026-06-30", conn=conn)

    assert "positions" in report.steps_run
    assert report.dirty_tree_bypassed is not None
    assert "deadbee-dirty" in report.dirty_tree_bypassed


def test_t_minus_1_hard_veto_excludes_asset(cycle_seed: Database) -> None:
    conn = cycle_seed
    seed_catalog(conn)
    # AAA (asset 1) carries an open HARD veto stint raised the day before the cycle
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (1, 'LEVERAGE_EXTREME', 'HARD', '2026-06-29', '2026-06-29', '2026-06-29T00:00:00Z')"
    )
    conn.commit()
    run_selection(_settings(conn), "2026-06-30", conn=conn)
    row = conn.execute(
        "SELECT vetoed, selected FROM v_cycle_ranking WHERE ticker = 'AAA'"
    ).fetchone()
    assert row["vetoed"] == 1
    assert row["selected"] == 0


def test_hard_veto_detected_via_rules_excludes_asset_starting_next_cycle(
    cycle_seed: Database,
) -> None:
    """C1 (docs/model_fixes.md): the T-1 lag mechanism exercised end-to-end
    through the *real* rule-detection path, not a hand-inserted veto row
    (unlike `test_t_minus_1_hard_veto_excludes_asset` above) -- closing the
    one gap that left room to suspect a live comparator bug. EEE (asset 5)
    naturally trips `LEVERAGE_EXTREME` via `cycle_seed`'s own
    `debt_to_equity = 5.5` (threshold 3.0)."""
    conn = cycle_seed
    seed_catalog(conn)

    run_selection(_settings(conn), "2026-06-30", conn=conn)
    day1 = conn.execute(
        "SELECT vetoed, selected FROM v_cycle_ranking WHERE ticker = 'EEE' AND cycle_date = '2026-06-30'"
    ).fetchone()
    assert day1["vetoed"] == 0  # same-day exemption: today's own veto doesn't apply yet
    veto_row = conn.execute(
        "SELECT severity, raised_on, cleared_on FROM veto "
        "WHERE asset_id = 5 AND rule_id = 'LEVERAGE_EXTREME'"
    ).fetchone()
    assert veto_row["severity"] == "HARD"
    assert veto_row["raised_on"] == "2026-06-30"
    assert veto_row["cleared_on"] is None

    run_selection(_settings(conn), "2026-07-01", conn=conn)
    day2 = conn.execute(
        "SELECT vetoed, selected FROM v_cycle_ranking WHERE ticker = 'EEE' AND cycle_date = '2026-07-01'"
    ).fetchone()
    assert day2["vetoed"] == 1
    assert day2["selected"] == 0


# -- T-125: veto is a stint (raised_on/cleared_on/last_seen_on), not a per-date event ----


def test_rule_result_iterates_as_its_hits() -> None:
    hit = VetoHit(1, "R", "HARD", {})
    result = RuleResult([hit], frozenset({1, 2}))
    assert list(result) == [hit]
    assert result.hits == [hit]
    assert result.evaluated == frozenset({1, 2})


def _seed_rule(conn: Database, rule_id: str = "R1", severity: str = "HARD") -> None:
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, enabled, created_at) "
        "VALUES (?, 'x', ?, 1, '2026-01-01')",
        (rule_id, severity),
    )
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    conn.commit()


def test_write_vetoes_opens_extends_then_clears_a_hard_stint(memory_db: Database) -> None:
    """The core T-125 fix: a HARD veto clears the first evaluated cycle its condition is
    false, rather than staying permanent once raised."""
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {"v": 1})

    write_vetoes(memory_db, "2026-06-30", [hit], {"R1": frozenset({1})}, [], run_id=1)
    row = memory_db.execute("SELECT * FROM veto WHERE asset_id = 1 AND rule_id = 'R1'").fetchone()
    assert (row["raised_on"], row["cleared_on"], row["last_seen_on"]) == (
        "2026-06-30",
        None,
        "2026-06-30",
    )

    # still hit a week later: the same stint extends, no second row
    write_vetoes(memory_db, "2026-07-07", [hit], {"R1": frozenset({1})}, [], run_id=2)
    rows = memory_db.execute("SELECT * FROM veto WHERE asset_id = 1 AND rule_id = 'R1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["last_seen_on"] == "2026-07-07"
    assert rows[0]["raised_on"] == "2026-06-30"  # unchanged -- still the same stint

    # evaluated, no longer hit: the stint closes on this cycle, not before, not never
    write_vetoes(memory_db, "2026-07-14", [], {"R1": frozenset({1})}, [], run_id=3)
    row = memory_db.execute("SELECT * FROM veto WHERE asset_id = 1 AND rule_id = 'R1'").fetchone()
    assert row["cleared_on"] == "2026-07-14"

    # the T-1 point-in-time predicate: active up to (not including) its clear date
    assert hard_vetoed_as_of(memory_db, "2026-07-13") == {1}
    assert hard_vetoed_as_of(memory_db, "2026-07-14") == set()

    # a later re-raise opens a new, distinct stint rather than reusing the closed one
    write_vetoes(memory_db, "2026-08-01", [hit], {"R1": frozenset({1})}, [], run_id=4)
    reraised = memory_db.execute(
        "SELECT COUNT(*) FROM veto WHERE asset_id = 1 AND rule_id = 'R1'"
    ).fetchone()[0]
    assert reraised == 2
    assert hard_vetoed_as_of(memory_db, "2026-08-01") == {1}


def test_write_vetoes_soft_stint_penalizes_once_not_per_cycle_held(memory_db: Database) -> None:
    """A SOFT rule held across several cycles is one open stint, not one hit per cycle."""
    _seed_rule(memory_db, severity="SOFT")
    hit = VetoHit(1, "R1", "SOFT", {})

    for cycle_date in ("2026-06-30", "2026-07-07", "2026-07-14"):
        write_vetoes(memory_db, cycle_date, [hit], {"R1": frozenset({1})}, [], run_id=1)

    count = memory_db.execute(
        "SELECT COUNT(*) FROM veto WHERE asset_id = 1 AND rule_id = 'R1'"
    ).fetchone()[0]
    assert count == 1  # not 3
    soft = active_soft_vetoes(memory_db, "2026-07-14")
    assert soft == {1: ["R1"]}  # one entry, not three


def test_write_vetoes_leaves_open_stint_untouched_when_asset_not_evaluated(
    memory_db: Database,
) -> None:
    """Missing data (the rule couldn't resolve this asset this cycle) must never be
    misread as "condition cleared" -- the open stint stays open, untouched."""
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {})
    write_vetoes(memory_db, "2026-06-30", [hit], {"R1": frozenset({1})}, [], run_id=1)

    # asset 1 missing from R1's evaluated set entirely this cycle (e.g. no filing data)
    write_vetoes(memory_db, "2026-07-07", [], {"R1": frozenset()}, [], run_id=2)

    row = memory_db.execute("SELECT * FROM veto WHERE asset_id = 1 AND rule_id = 'R1'").fetchone()
    assert row["cleared_on"] is None
    assert row["raised_on"] == "2026-06-30"
    assert row["last_seen_on"] == "2026-06-30"  # untouched, not bumped to 2026-07-07


def test_write_vetoes_disabled_rule_closes_its_open_stints(memory_db: Database) -> None:
    """A rule turned off in rule_catalog is never asked to evaluate anything again -- its
    open stints must still close, or they would stay open forever."""
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {})
    write_vetoes(memory_db, "2026-06-30", [hit], {"R1": frozenset({1})}, [], run_id=1)

    memory_db.execute("UPDATE rule_catalog SET enabled = 0 WHERE rule_id = 'R1'")
    memory_db.commit()
    assert disabled_rule_ids(memory_db) == {"R1"}
    write_vetoes(memory_db, "2026-07-07", [], {}, disabled_rule_ids(memory_db), run_id=2)

    row = memory_db.execute("SELECT cleared_on FROM veto WHERE asset_id = 1").fetchone()
    assert row["cleared_on"] == "2026-07-07"


def test_write_vetoes_same_date_rerun_is_idempotent(memory_db: Database) -> None:
    """Re-running the same cycle_date recomputes cleanly: it undoes what it itself wrote
    the first time before reapplying, rather than compounding (T-125 f)."""
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {"v": 1})
    write_vetoes(memory_db, "2026-06-30", [hit], {"R1": frozenset({1})}, [], run_id=1)

    # re-run the same date with a different result: no hit this time
    write_vetoes(memory_db, "2026-06-30", [], {"R1": frozenset({1})}, [], run_id=2)
    assert memory_db.execute("SELECT COUNT(*) FROM veto").fetchone()[0] == 0

    # re-run again, hit once more: exactly one stint, not a stack of leftovers
    write_vetoes(memory_db, "2026-06-30", [hit], {"R1": frozenset({1})}, [], run_id=3)
    assert memory_db.execute("SELECT COUNT(*) FROM veto").fetchone()[0] == 1


def test_veto_out_of_order_reason_pure_function(memory_db: Database) -> None:
    assert veto_out_of_order_reason(memory_db, "2020-01-01") is None  # no rows yet
    _seed_rule(memory_db)
    write_vetoes(
        memory_db,
        "2026-06-30",
        [VetoHit(1, "R1", "HARD", {})],
        {"R1": frozenset({1})},
        [],
        run_id=1,
    )
    assert veto_out_of_order_reason(memory_db, "2026-07-01") is None  # newer -- fine
    assert veto_out_of_order_reason(memory_db, "2026-06-30") is None  # same date -- fine
    reason = veto_out_of_order_reason(memory_db, "2026-05-01")
    assert reason is not None
    assert "2026-05-01" in reason and "2026-06-30" in reason


def test_reset_replay_range_also_undoes_veto_transitions(memory_db: Database) -> None:
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {})
    write_vetoes(memory_db, "2026-06-01", [hit], {"R1": frozenset({1})}, [], run_id=1)
    write_vetoes(memory_db, "2026-07-01", [], {"R1": frozenset({1})}, [], run_id=2)  # clears it

    reset_replay_range(memory_db, "2026-06-15")

    # raised_on < date_from is left alone; the cleared_on >= date_from is undone (reopened)
    row = memory_db.execute("SELECT raised_on, cleared_on FROM veto").fetchone()
    assert row["raised_on"] == "2026-06-01"
    assert row["cleared_on"] is None

    reset_replay_range(memory_db, "2026-06-01")  # now also wipes the stint's own raising
    assert memory_db.execute("SELECT COUNT(*) FROM veto").fetchone()[0] == 0


def test_reset_replay_range_rolls_back_last_seen_on_past_date_from(memory_db: Database) -> None:
    """PR #103 review: the void/reopen pair above only ever touches raised_on/cleared_on. A
    stint raised *before* date_from but extended (last_seen_on bumped forward) by a hit on
    or after it survived untouched -- itself a transition dated on or after date_from, so
    the very guard this reset exists to clear the way for still found one and refused the
    redo. On a copy of production, where a live stint raised earlier was still being seen
    after the range being reset, ``cycle backfill --force`` failed on its first date."""
    _seed_rule(memory_db)
    hit = VetoHit(1, "R1", "HARD", {})
    write_vetoes(memory_db, "2026-06-01", [hit], {"R1": frozenset({1})}, [], run_id=1)
    write_vetoes(memory_db, "2026-07-01", [hit], {"R1": frozenset({1})}, [], run_id=2)  # extends

    reset_replay_range(memory_db, "2026-06-15")

    assert veto_out_of_order_reason(memory_db, "2026-06-15") is None
    row = memory_db.execute("SELECT raised_on, last_seen_on FROM veto").fetchone()
    assert (row["raised_on"], row["last_seen_on"]) == ("2026-06-01", "2026-06-01")


def test_veto_predicates_raise_a_clear_error_against_a_pre_m009_veto_table(
    memory_db: Database,
) -> None:
    """PR #103 review: before production ``migrate`` runs (deliberately deferred by this PR),
    ``veto`` may still be the old per-(asset, rule, cycle_date) hit-row shape. Every reader
    here, and ``write_vetoes``, used to catch a bare ``DatabaseError`` around the query --
    swallowing "no such column: raised_on" the same as "no such table: veto" -- so a cycle
    run would either silently see no vetoes at all, or crash mid-write with a raw traceback
    instead of an actionable message."""
    conn = memory_db
    conn.executescript(
        """
        DROP TABLE veto;
        CREATE TABLE veto (
            id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
            rule_id TEXT NOT NULL REFERENCES rule_catalog(rule_id), severity TEXT NOT NULL,
            detected_at TEXT NOT NULL, cycle_date TEXT NOT NULL, cleared_at TEXT,
            evidence_json TEXT, run_id INTEGER,
            UNIQUE (asset_id, rule_id, cycle_date)
        );
        """
    )
    conn.commit()

    for fn in (
        lambda: hard_vetoed_as_of(conn, "2026-06-30"),
        lambda: active_soft_vetoes(conn, "2026-06-30"),
        lambda: veto_out_of_order_reason(conn, "2026-06-30"),
        lambda: write_vetoes(conn, "2026-06-30", [], {}, [], run_id=1),
    ):
        with pytest.raises(VetoSchemaStale, match="migrate"):
            fn()


# -- T-119: no FUNDAMENTAL score at all -----------------------------------


def test_unscored_assets_diffs_universe_against_scored_keys() -> None:
    assert unscored_assets([1, 2, 3], {1: "x", 3: None}) == [2]


def test_unscored_assets_includes_every_asset_when_none_is_scored() -> None:
    """PR #99 review: not exempt -- PR #78's decision ('more than 5% unscored stops the
    cycle') applies at 100% too, not just a partial gap."""
    assert unscored_assets([1, 2, 3], {}) == [1, 2, 3]


def test_too_many_unscored_reason_pure_function() -> None:
    assert too_many_unscored_reason([], 20, 0.05) is None
    assert too_many_unscored_reason([1], 20, 0.05) is None  # exactly 5% -- not "more than"
    reason = too_many_unscored_reason([1, 2], 20, 0.05)
    assert reason is not None
    assert "2/20" in reason
    assert "10.0%" in reason


def test_unscored_asset_ineligible_immediately_not_through_t1_lag(cycle_seed: Database) -> None:
    """T-119 (PR #78 review): a universe member with *no* FUNDAMENTAL score at all -- not
    merely a stale one -- is ineligible the same cycle it's detected, unlike a HARD veto (T-1
    lag, see `test_hard_veto_detected_via_rules_excludes_asset_starting_next_cycle` above), and
    is not penalized via a SOFT veto (no `veto` table row, no `soft_veto_penalty` stacking)."""
    conn = cycle_seed
    seed_catalog(conn)
    conn.execute("DELETE FROM score_snapshot WHERE asset_id = 2 AND score_type = 'FUNDAMENTAL'")
    conn.commit()
    settings = _settings(conn).model_copy(update={"unscored_max_share": 0.5})
    report = run_selection(settings, "2026-06-30", conn=conn)

    assert report.unscored == 1
    assert report.unscored_tickers == ["BBB"]
    row = conn.execute(
        "SELECT vetoed, selected, veto_rules_json FROM v_cycle_ranking WHERE ticker = 'BBB'"
    ).fetchone()
    assert row["vetoed"] == 1  # same cycle -- not the T-1 lag
    assert row["selected"] == 0
    assert json.loads(row["veto_rules_json"]) == ["UNSCORED"]
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM veto WHERE asset_id = 2").fetchone()["n"] == 0
    )  # no SOFT veto row -- ineligible, not penalized
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio_position WHERE asset_id = 2 AND valid_to IS NULL"
        ).fetchone()["n"]
        == 0
    )


def test_unscored_report_field_survives_resume(cycle_seed: Database) -> None:
    """Sourcery (PR #99 review): report.unscored/unscored_tickers used to fall back to their
    defaults (0, []) on a resumed run that skips the already-`done` `rank` step, even though the
    persisted cycle_ranking still has UNSCORED rows -- now read back from cycle_ranking after
    `_do("rank", ...)` regardless of whether that call ran or was skipped."""
    conn = cycle_seed
    seed_catalog(conn)
    conn.execute("DELETE FROM score_snapshot WHERE asset_id = 2 AND score_type = 'FUNDAMENTAL'")
    conn.commit()
    settings = _settings(conn).model_copy(update={"unscored_max_share": 0.5})
    run_selection(settings, "2026-06-30", conn=conn)

    again = run_selection(settings, "2026-06-30", conn=conn)
    assert "rank" in again.steps_skipped
    assert again.unscored == 1
    assert again.unscored_tickers == ["BBB"]


def test_print_unscored_names_the_tickers(capsys: pytest.CaptureFixture[str]) -> None:
    """PR #99 review: an excluded asset must be visible in the CLI output, not only queryable
    from cycle_ranking."""
    r = CycleReport(1, "SELECTION", "2026-06-30", unscored=2, unscored_tickers=["ABC", "XYZ"])
    _print_unscored(r)
    assert "2 unscored (ineligible): ABC, XYZ" in capsys.readouterr().out


def test_print_unscored_silent_when_none(capsys: pytest.CaptureFixture[str]) -> None:
    _print_unscored(CycleReport(1, "SELECTION", "2026-06-30"))
    assert capsys.readouterr().out == ""


def test_too_many_unscored_refuses_the_selection_cycle(cycle_seed: Database) -> None:
    conn = cycle_seed
    seed_catalog(conn)
    conn.execute("DELETE FROM score_snapshot WHERE asset_id = 2 AND score_type = 'FUNDAMENTAL'")
    conn.commit()
    with pytest.raises(TooManyUnscored, match=r"1/5.*20\.0%"):
        run_selection(_settings(conn), "2026-06-30", conn=conn)
    run_row = conn.execute("SELECT status FROM cycle_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run_row["status"] == "failed"


def test_monitor_also_refuses_too_many_unscored(cycle_seed: Database) -> None:
    conn = cycle_seed
    seed_catalog(conn)
    conn.execute("DELETE FROM score_snapshot WHERE asset_id = 2 AND score_type = 'FUNDAMENTAL'")
    conn.commit()
    with pytest.raises(TooManyUnscored):
        run_monitoring(_settings(conn), "2026-06-30", conn=conn)


def test_all_unscored_also_refuses_the_selection_cycle(cycle_seed: Database) -> None:
    """PR #99 review: an earlier version of this fix exempted a *whole* unscored universe
    (reasoning: a cycle dated before any filing is public yet has nothing to compare against).
    That inverted PR #78's own decision -- 100% unscored is still "more than 5% unscored" and
    must refuse, not silently build a portfolio on TECHNICAL/VALORIZATION alone. Reproduced live
    against `cycle_seed`: deleting all 5 FUNDAMENTAL rows used to rank AAA/BBB/CCC on
    TECHNICAL+VALORIZATION alone with zero UNSCORED marks and no warning."""
    conn = cycle_seed
    seed_catalog(conn)
    conn.execute("DELETE FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'")
    conn.commit()
    with pytest.raises(TooManyUnscored, match=r"5/5.*100\.0%"):
        run_selection(_settings(conn), "2026-06-30", conn=conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM portfolio_position WHERE valid_to IS NULL"
        ).fetchone()["n"]
        == 0
    )


def test_monitoring_cycle_skips_positions(cycle_seed: Database) -> None:
    conn = cycle_seed
    r = run_monitoring(_settings(conn), "2026-07-31", conn=conn)
    assert "positions" not in r.steps_run and "positions" not in r.steps_skipped
    assert conn.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0] == 0


# -- provenance secret-redaction --------------------------------


def test_redact_masks_secret_keys_recursively() -> None:
    out = _redact(
        {
            "llm_api_key": "sk-abc123",
            "llm_model": "deepseek-chat",
            "nested": {"auth_token": "nested-secret-value", "top_n": 30},
            "list": [{"client_credential": "list-secret-value"}, {"ok": 1}],
            "empty_secret": None,
        }
    )
    dumped = json.dumps(out)
    assert "sk-abc123" not in dumped
    assert "nested-secret-value" not in dumped
    assert "list-secret-value" not in dumped
    assert dumped.count("***REDACTED***") == 3
    assert out["llm_model"] == "deepseek-chat"
    assert out["nested"]["top_n"] == 30
    assert out["list"][1]["ok"] == 1
    assert out["empty_secret"] is None  # nothing to hide, leave the shape intact


def test_open_cycle_never_persists_the_api_key() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    kg_schema.ensure(conn)

    run_id = open_cycle(
        conn,
        "SELECTION",
        "2026-08-27",
        {"db_path": "/x", "llm_api_key": "sk-DEADBEEF", "llm_url": "https://api.deepseek.com"},
    )
    checkpoint(conn, run_id, "score", "done", {"llm_api_key": "sk-DEADBEEF", "n": 5})

    params = conn.execute("SELECT params_json FROM cycle_run WHERE id = ?", (run_id,)).fetchone()[
        "params_json"
    ]
    detail = conn.execute(
        "SELECT detail_json FROM cycle_checkpoint WHERE cycle_run_id = ?", (run_id,)
    ).fetchone()["detail_json"]

    assert "sk-DEADBEEF" not in params
    assert "sk-DEADBEEF" not in detail
    assert json.loads(params)["llm_api_key"] == "***REDACTED***"
    assert json.loads(params)["llm_url"] == "https://api.deepseek.com"
    assert json.loads(detail)["n"] == 5


# -- metric-version manifest (T-090) --------------------------------------------------------


def _manifest(conn: Database, cycle_type: str, cycle_date: str) -> dict[str, object]:
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    return dict(json.loads(row["params_json"]))


def _add_metrics_v2(conn: Database) -> None:
    """A newer engine writes a parallel copy of every metric (values doubled)."""
    kg_schema.apply_migrations(conn)  # the migrated key is what lets versions coexist
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
        "computed_at, engine_version, event_time, available_at) "
        "SELECT filing_id, metric_group, metric_name, value * 2, unit, computed_at, "
        "'metrics-v2', event_time, available_at FROM fundamental_metrics "
        "WHERE engine_version = 'metrics-v1'"
    )
    conn.commit()


def test_a_cycle_records_the_metric_versions_it_read(cycle_seed: Database) -> None:
    conn = cycle_seed
    report = run_selection(_settings(conn), "2026-06-30", conn=conn)
    recorded = _manifest(conn, "SELECTION", "2026-06-30")
    assert recorded["manifest_tag"] == report.manifest_tag
    assert recorded["manifest"] == {
        "consumer": "cycle",
        "metrics": {
            "cashflow": "metrics-v1",
            "leverage": "metrics-v1",
            "liquidity": "metrics-v1",
            "profitability": "metrics-v1",
            "valuation": "metrics-v1",
        },
        "quality": "dq-v2",  # the Ring-1 gate version it read quarantines under (T-065/T-116)
    }


def test_resuming_a_run_after_a_newer_metrics_version_appears_is_refused(
    cycle_seed: Database,
) -> None:
    """cycle_run is unique per (type, date) and its outputs are keyed by date, so a second run
    over other inputs cannot sit beside the first -- resuming would mix them. Refuse, and do
    not touch the earlier run."""
    conn = cycle_seed
    first = run_selection(_settings(conn), "2026-06-30", conn=conn)
    _add_metrics_v2(conn)  # the default now resolves metrics-v2

    with pytest.raises(ManifestMismatch, match=first.manifest_tag):
        run_selection(_settings(conn), "2026-06-30", conn=conn)

    status = conn.execute(
        "SELECT status FROM cycle_run WHERE cycle_type = 'SELECTION' AND cycle_date = '2026-06-30'"
    ).fetchone()["status"]
    assert status == "completed"  # the refusal happened before open_cycle re-opened the run
    assert _manifest(conn, "SELECTION", "2026-06-30")["manifest_tag"] == first.manifest_tag


def test_naming_the_recorded_version_resumes_the_run(cycle_seed: Database) -> None:
    conn = cycle_seed
    first = run_selection(_settings(conn), "2026-06-30", conn=conn)
    _add_metrics_v2(conn)
    pinned = _settings(conn).model_copy(update={"metrics_version": "metrics-v1"})

    again = run_selection(pinned, "2026-06-30", conn=conn)

    assert again.manifest_tag == first.manifest_tag
    assert again.steps_run == []  # everything was already done under that manifest


def test_a_new_date_reads_the_newer_version_and_gets_its_own_manifest(cycle_seed: Database) -> None:
    conn = cycle_seed
    first = run_selection(_settings(conn), "2026-06-30", conn=conn)
    _add_metrics_v2(conn)

    later = run_monitoring(_settings(conn), "2026-07-31", conn=conn)

    assert later.manifest_tag != first.manifest_tag
    metrics = _manifest(conn, "MONITORING", "2026-07-31")["manifest"]
    assert metrics["metrics"]["leverage"] == "metrics-v2"  # type: ignore[index]


def test_an_unstored_metrics_version_fails_before_a_run_is_created(cycle_seed: Database) -> None:
    conn = cycle_seed
    bad = _settings(conn).model_copy(update={"metrics_version": "metrics-v9"})
    with pytest.raises(VersionError, match="metrics-v9"):
        run_selection(bad, "2026-06-30", conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM cycle_run").fetchone()[0] == 0


def test_allow_dirty_flag_exists_on_select_monitor_and_backfill() -> None:
    parser = build_parser()
    for command in ("select", "monitor"):
        assert parser.parse_args([command]).allow_dirty is False
        assert parser.parse_args([command, "--allow-dirty"]).allow_dirty is True
    assert (
        parser.parse_args(["backfill", "--from", "2026-01-01", "--to", "2026-02-01"]).allow_dirty
        is False
    )
    assert parser.parse_args(
        ["backfill", "--from", "2026-01-01", "--to", "2026-02-01", "--allow-dirty"]
    ).allow_dirty


def test_the_cycle_flag_exists_and_the_cli_exits_1_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parser = build_parser()
    for command in ("select", "monitor"):
        assert parser.parse_args([command, "--metrics-version", "metrics-v1"]).metrics_version == (
            "metrics-v1"
        )
    assert (
        parser.parse_args(
            ["backfill", "--from", "2026-01-01", "--to", "2026-02-01"]
        ).metrics_version
        is None
    )

    monkeypatch.setattr(
        "cycle.cli.CycleSettings.load", lambda: CycleSettings(db_path=Path(":memory:"))
    )
    monkeypatch.setattr("cycle.cli.make_hook", lambda _s: None)

    def refuse(*_a: object, **_k: object) -> None:
        raise ManifestMismatch("built on manifest abc12345")

    monkeypatch.setattr("cycle.cli.run_selection", refuse)
    assert cycle_main(["select", "--date", "2026-06-30"]) == 1
    assert "abc12345" in capsys.readouterr().err

    def unstored(*_a: object, **_k: object) -> None:
        raise VersionError("metrics version 'metrics-v9' is not stored")

    monkeypatch.setattr("cycle.cli.run_monitoring", unstored)
    assert cycle_main(["monitor", "--date", "2026-06-30", "--metrics-version", "metrics-v9"]) == 1
    assert "metrics-v9" in capsys.readouterr().err
