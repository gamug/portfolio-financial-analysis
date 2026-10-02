"""T-131 on the quant side: a day counted once however many engine versions hold it, and a
return series that follows a corrected price history."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from portfolio_common.db import Database

from quant.config import QuantSettings
from quant.db import ReturnRow, upsert_return_daily
from quant.universe import _history_counts, benchmark_gate, liquidity_data_gate, settings_gate


def _copy_observations(
    conn: Database, version: str, computed_at: str, *, since: str = "", **overrides: float
) -> None:
    """Copy ``price_observation`` rows dated on/after *since* under *version*, stamped
    *computed_at*."""
    conn.execute(
        "INSERT INTO price_observation (asset_id, obs_date, close, prev_close, log_return, "
        "dollar_volume, event_time, computed_at, engine_version) "
        "SELECT asset_id, obs_date, close, prev_close, log_return, "
        "COALESCE(?, dollar_volume), event_time, ?, ? FROM price_observation WHERE obs_date >= ?",
        (overrides.get("dollar_volume"), computed_at, version, since),
    )
    conn.commit()


def _nth_last_date(conn: Database, n: int) -> str:
    rows = conn.execute("SELECT DISTINCT obs_date FROM price_observation ORDER BY obs_date DESC")
    return str([r[0] for r in rows][n - 1])


def test_observation_history_follows_the_pinned_version_not_the_newest_one(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """Every other quant reader pins ``settings.*_engine_version``; a gate that resolved the
    latest version per day could count a different series than the panel is built from, and a
    rewrite bumps ``computed_at`` so "latest" would flip after any rebuild."""
    conn = quant_seed(memory_quant_db, n_assets=2, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    # a newer, shorter v2 series: 100 days
    _copy_observations(conn, "priceobs-v2", "2099-01-01T00:00:00Z", since=_nth_last_date(conn, 100))

    v1 = _history_counts(
        conn, as_of, return_engine_version="qret-v2", observation_engine_version="priceobs-v1"
    )
    v2 = _history_counts(
        conn, as_of, return_engine_version="qret-v2", observation_engine_version="priceobs-v2"
    )
    assert set(v1.values()) == {259}  # 260 bars, the first has no log return
    assert set(v2.values()) == {100}

    gate = liquidity_data_gate(
        conn,
        as_of=as_of,
        min_history_days=200,
        min_dollar_volume=0.0,
        observation_engine_version="priceobs-v1",
    )
    assert gate.asset_ids == [1, 2]  # the pinned series is long enough
    gate = liquidity_data_gate(
        conn,
        as_of=as_of,
        min_history_days=200,
        min_dollar_volume=0.0,
        observation_engine_version="priceobs-v2",
    )
    assert set(gate.dropped.values()) == {"short_history"}


def test_return_history_follows_the_pinned_version_not_the_newest_one(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    dates = [r[0] for r in conn.execute("SELECT date FROM price_daily ORDER BY date")]
    for version, stamp, days in (
        ("qret-v2", "2026-01-01T00:00:00Z", dates),
        ("qret-v3", "2026-02-01T00:00:00Z", dates[-100:]),  # newer, shorter
    ):
        conn.executemany(
            "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, "
            "tr_index, tr_log_return, source, engine_version, computed_at) "
            "VALUES (1, ?, 1, 1, 1, 0.001, 'quant-tr-v1', ?, ?)",
            [(d, version, stamp) for d in days],
        )
    conn.commit()

    def counts(version: str) -> dict[int, int]:
        return _history_counts(
            conn, dates[-1], return_engine_version=version, observation_engine_version="priceobs-v1"
        )

    assert counts("qret-v2") == {1: 260}
    assert counts("qret-v3") == {1: 100}


def test_median_dollar_volume_reads_only_the_pinned_version(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """A *newer* version with tiny volumes must not drag down the pinned version's liquidity."""
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    _copy_observations(conn, "priceobs-v2", "2099-01-01T00:00:00Z", dollar_volume=1.0)

    def kept(version: str) -> list[int]:
        return liquidity_data_gate(
            conn,
            as_of=as_of,
            min_history_days=200,
            min_dollar_volume=40_000_000.0,
            liquidity_lookback_days=20,
            observation_engine_version=version,
        ).asset_ids

    assert kept("priceobs-v1") == [1]
    assert kept("priceobs-v2") == []


def test_the_gates_take_their_pins_from_the_settings(
    memory_quant_db: Database, quant_seed: Callable[..., Database], tmp_path: Path
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    _copy_observations(conn, "priceobs-v2", "2099-01-01T00:00:00Z", since=_nth_last_date(conn, 100))
    settings = QuantSettings(
        db_path=tmp_path / "x.db", min_history_days=200, liquidity_min_dollar_volume=0.0
    )

    assert settings_gate(conn, settings, as_of=as_of).asset_ids == [1]
    pinned_short = settings.model_copy(update={"observation_engine_version": "priceobs-v2"})
    assert settings_gate(conn, pinned_short, as_of=as_of).asset_ids == []
    assert benchmark_gate(conn, pinned_short, as_of=as_of).asset_ids == []


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


def test_settings_gate_counts_the_series_the_panel_will_read(
    memory_quant_db: Database, quant_seed: Callable[..., Database], tmp_path: Path
) -> None:
    """``--returns-version`` makes the panel read the resolved manifest version, not the configured
    default; the gate has to count that same series."""
    conn = quant_seed(memory_quant_db, n_assets=1, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]
    dates = [r[0] for r in conn.execute("SELECT date FROM price_daily ORDER BY date")]
    for version, days in (("qret-v2", dates), ("qret-v3", dates[-100:])):
        conn.executemany(
            "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, "
            "tr_index, tr_log_return, source, engine_version, computed_at) "
            "VALUES (1, ?, 1, 1, 1, 0.001, 'quant-tr-v1', ?, '2026-01-01T00:00:00Z')",
            [(d, version) for d in days],
        )
    conn.commit()
    settings = QuantSettings(
        db_path=tmp_path / "x.db", min_history_days=200, liquidity_min_dollar_volume=0.0
    )

    assert settings_gate(conn, settings, as_of=as_of).asset_ids == [1]  # configured qret-v2
    assert (
        settings_gate(conn, settings, as_of=as_of, return_engine_version="qret-v3").asset_ids == []
    )
