"""T-131 on the quant side: a day counted once however many engine versions hold it, and a
return series that follows a corrected price history."""

from __future__ import annotations

from collections.abc import Callable

from portfolio_common.db import Database

from quant.db import ReturnRow, upsert_return_daily
from quant.universe import _history_counts, liquidity_data_gate


def _second_version(conn: Database, version: str, computed_at: str, **overrides: float) -> None:
    """Copy every ``price_observation`` row under *version*, stamped *computed_at*."""
    dollar_volume = overrides.get("dollar_volume")
    conn.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, prev_close, log_return, "
        "dollar_volume, event_time, computed_at, engine_version) "
        "SELECT asset_id, obs_date, close, prev_close, log_return, "
        "COALESCE(?, dollar_volume), event_time, ?, ? FROM price_observation",
        (dollar_volume, computed_at, version),
    )
    conn.commit()


def test_history_is_counted_once_per_day_not_once_per_engine_version(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """A second observation version would otherwise double every name's history and let a
    short-history name through the 504-day gate."""
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    single = _history_counts(conn, as_of)

    _second_version(conn, "priceobs-v2", "2099-01-01T00:00:00Z")

    assert _history_counts(conn, as_of) == single
    gate = liquidity_data_gate(conn, as_of=as_of, min_history_days=300, min_dollar_volume=0.0)
    assert gate.asset_ids == []
    assert set(gate.dropped.values()) == {"short_history"}


def test_return_history_is_counted_once_per_day_across_return_versions(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    dates = [r[0] for r in conn.execute("SELECT date FROM price_daily ORDER BY date")]
    for version, stamp in (
        ("qret-v2", "2026-01-01T00:00:00Z"),
        ("qret-v3", "2026-02-01T00:00:00Z"),
    ):
        conn.executemany(
            "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, "
            "tr_index, tr_log_return, source, engine_version, computed_at) "
            "VALUES (1, ?, 1, 1, 1, 0.001, 'quant-tr-v1', ?, ?)",
            [(d, version, stamp) for d in dates],
        )
    conn.commit()

    assert _history_counts(conn, dates[-1]) == {1: 260}


def test_median_dollar_volume_reads_the_latest_version_of_each_day(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """A stale version's tiny volumes must not drag down the liquidity of the day the latest
    version has right."""
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    _second_version(conn, "priceobs-v0", "2000-01-01T00:00:00Z", dollar_volume=1.0)

    gate = liquidity_data_gate(
        conn,
        as_of=as_of,
        min_history_days=200,
        min_dollar_volume=40_000_000.0,
        liquidity_lookback_days=20,
    )
    assert gate.asset_ids == [1]


def _row(day: str, close: float, tr_index: float) -> ReturnRow:
    return ReturnRow(day, close, close, tr_index, 0.0, 1.0, 0.01, 0.01)


def test_a_return_row_follows_a_corrected_close_and_is_otherwise_left_alone(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=3, with_dividends=False)
    rows = [_row("2030-01-02", 100.0, 100.0), _row("2030-01-03", 101.0, 101.0)]
    assert upsert_return_daily(conn, 1, rows, engine_version="qret-v2") == 2
    conn.execute("UPDATE quant_return_daily SET computed_at = 'sentinel'")
    conn.commit()

    assert upsert_return_daily(conn, 1, rows, engine_version="qret-v2") == 0  # unchanged
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM quant_return_daily WHERE computed_at != 'sentinel'"
        ).fetchone()[0]
        == 0
    )

    corrected = [rows[0], _row("2030-01-03", 50.5, 50.5)]  # the day was re-adjusted
    assert upsert_return_daily(conn, 1, corrected, engine_version="qret-v2") == 1
    got = conn.execute(
        "SELECT obs_date, close_split_adj, computed_at FROM quant_return_daily ORDER BY obs_date"
    ).fetchall()
    assert [(r[0], r[1]) for r in got] == [("2030-01-02", 100.0), ("2030-01-03", 50.5)]
    assert got[0][2] == "sentinel" and got[1][2] != "sentinel"
