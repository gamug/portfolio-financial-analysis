"""Selection / monitoring cycle: normalization, scores, rules, ranking, positions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database

import kg_schema
from cycle.cli import build_parser
from cycle.cli import main as cycle_main
from cycle.config import CycleSettings
from cycle.construction import Candidate, target_weights
from cycle.orchestrator import run_monitoring, run_selection
from cycle.rules import RuleContext, enabled_rules, seed_catalog
from cycle.scores import sector, technical, valorization
from cycle.scores.normalize import cross_sectional_z, rank_pct, z_to_score
from cycle.state import ManifestMismatch, _redact, checkpoint, open_cycle
from kg_schema.versions import VersionError

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


def test_leverage_rule_hard_vetoes_negative_equity_with_high_debt_to_assets(
    memory_db: Database,
) -> None:
    """C2 (docs/model_fixes.md): MCD-shaped negative book equity -- a
    negative debt_to_equity would trivially evade a plain `> 3.0` check, but
    a high debt_to_assets still triggers the veto."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -38.96,
                "leverage.debt_to_assets": 0.95,
                "leverage.interest_coverage": 8.0,
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
    """Proves debt_to_assets and interest_coverage are each independently
    sufficient (an OR, not an AND)."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -10.0,
                "leverage.debt_to_assets": 0.3,  # healthy
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
    """Not "any negative equity = HARD": healthy debt_to_assets/
    interest_coverage, including exactly at the calibrated thresholds
    (strict inequalities, not >=/<=), doesn't veto."""
    seed_catalog(memory_db)
    ctx = RuleContext(
        cycle_date="2026-06-30",
        metrics={
            1: {
                "leverage.debt_to_equity": -5.0,
                "leverage.debt_to_assets": 0.8,  # exactly at threshold, not >
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


def test_leverage_rule_negative_equity_with_no_corroborating_metrics_does_not_veto(
    memory_db: Database,
) -> None:
    """Accepted limitation: if both debt_to_assets and interest_coverage are
    missing, a negative debt_to_equity alone doesn't trigger the veto --
    same missing-data handling as every other rule in this catalog."""
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
    assert not any(h[1] == "LEVERAGE_EXTREME" for h in hits)


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


@pytest.fixture
def cycle_seed(memory_db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Database:
    conn = memory_db
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'S1'), (2, 'S2')")
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    udb = write_universe_db(
        tmp_path / "cycle_universe.db", [(tk, "2026-01-01", None) for tk in tickers]
    )
    monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))
    for i, tk in enumerate(tickers, start=1):
        conn.execute(
            "INSERT INTO assets (id, ticker, sector_id) VALUES (?, ?, ?)", (i, tk, 1 + i % 2)
        )
        fid = conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "retrieved_at) VALUES (?, '10-K', 2025, 'FY2025', '2025-12-31', '2026-02-01T00:00:00Z') "
            "RETURNING id",
            (i,),
        ).fetchone()["id"]
        for grp, name, val in [
            ("leverage", "debt_to_equity", 0.5 + i),  # AAA best, EEE worst
            ("profitability", "return_on_equity", 0.35 - 0.05 * i),
            ("valuation", "free_cash_flow_yield", 0.10 - 0.015 * i),
            ("cashflow", "free_cash_flow_margin", 0.2 - 0.03 * i),
            ("liquidity", "current_ratio", 2.5 - 0.1 * i),
        ]:
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, computed_at, engine_version, event_time) "
                "VALUES (?, ?, ?, ?, 'x', '2026-02-01T00:00:00Z', 'metrics-v1', '2025-12-31')",
                (fid, grp, name, val),
            )
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind) VALUES (?, 'FUNDAMENTAL', ?, ?, "
            "'2025-12-31', '2026-02-01T00:00:00Z', 'seed', 'analysis')",
            (i, 80 - 8 * i, 80 - 8 * i),
        )
        for d in range(1, 121):
            conn.execute(
                "INSERT INTO price_observation (asset_id, obs_date, close, atr_14, "
                "realized_vol_90d, max_drawdown_90d, momentum_63d, momentum_21d, event_time, "
                "computed_at, engine_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'2026-06-30T00:00:00Z', 'priceobs-v1')",
                (
                    i,
                    f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                    100.0 + d * (6 - i),
                    2.0 * i,
                    0.10 * i,
                    -0.03 * i,
                    0.25 - 0.06 * i,
                    0.05 - 0.01 * i,
                    f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}",
                ),
            )
    conn.commit()
    return conn


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


def test_t_minus_1_hard_veto_excludes_asset(cycle_seed: Database) -> None:
    conn = cycle_seed
    seed_catalog(conn)
    # AAA (asset 1) carries a HARD veto dated the day before the cycle
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, detected_at, cycle_date) "
        "VALUES (1, 'LEVERAGE_EXTREME', 'HARD', '2026-06-29T00:00:00Z', '2026-06-29')"
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
        "SELECT severity, cycle_date, cleared_at FROM veto "
        "WHERE asset_id = 5 AND rule_id = 'LEVERAGE_EXTREME'"
    ).fetchone()
    assert veto_row["severity"] == "HARD"
    assert veto_row["cycle_date"] == "2026-06-30"
    assert veto_row["cleared_at"] is None

    run_selection(_settings(conn), "2026-07-01", conn=conn)
    day2 = conn.execute(
        "SELECT vetoed, selected FROM v_cycle_ranking WHERE ticker = 'EEE' AND cycle_date = '2026-07-01'"
    ).fetchone()
    assert day2["vetoed"] == 1
    assert day2["selected"] == 0


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
        "computed_at, engine_version, event_time) "
        "SELECT filing_id, metric_group, metric_name, value * 2, unit, computed_at, "
        "'metrics-v2', event_time FROM fundamental_metrics WHERE engine_version = 'metrics-v1'"
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
