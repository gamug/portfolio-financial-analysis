"""The universe gate drops names by data quality only -- never by any score."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from portfolio_common.db import Database

from quant.config import QuantSettings
from quant.universe import benchmark_gate, liquidity_data_gate, settings_gate


def test_gate_drops_illiquid_short_and_vetoed(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=5, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]

    # asset 2: short history -- keep only 40 obs
    conn.execute(
        "DELETE FROM price_observation WHERE asset_id = 2 AND obs_date NOT IN "
        "(SELECT obs_date FROM price_observation WHERE asset_id = 2 "
        " ORDER BY obs_date DESC LIMIT 40)"
    )
    # asset 3: illiquid -- crush recent dollar volume
    conn.execute("UPDATE price_observation SET dollar_volume = 1000.0 WHERE asset_id = 3")
    # asset 4: HARD veto dated before as_of
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, created_at) "
        "VALUES ('R1', 'x', 'HARD', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (4, 'R1', 'HARD', '2024-06-01', '2024-06-01', ?)",
        (as_of,),
    )
    conn.commit()

    res = liquidity_data_gate(
        conn, as_of=as_of, min_history_days=200, min_dollar_volume=1_000_000.0
    )
    assert res.asset_ids == [1, 5]
    assert res.dropped[2] == "short_history"
    assert res.dropped[3] == "illiquid"
    assert res.dropped[4] == "hard_veto"


def test_gate_is_independent_of_score_snapshot(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    conn = quant_seed(memory_quant_db, n_assets=4, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    before = liquidity_data_gate(conn, as_of=as_of, min_history_days=200, min_dollar_volume=0.0)

    for aid, val in ((1, 99.0), (2, 1.0), (3, 50.0), (4, 5.0)):
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, raw_value, normalized_score, "
            "event_time, computed_at, model, run_kind) "
            "VALUES (?, 'VALORIZATION', ?, ?, ?, ?, 'seed', 'cycle')",
            (aid, val, val, as_of, as_of + "T00:00:00Z"),
        )
    conn.commit()
    after = liquidity_data_gate(conn, as_of=as_of, min_history_days=200, min_dollar_volume=0.0)
    assert after.asset_ids == before.asset_ids


def test_benchmark_gate_keeps_a_hard_vetoed_name_a_book_would_drop(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> None:
    """The benchmark is the investable universe, not a book's own filtered picture of it: a
    book's hard-veto exclusions can't show up as alpha against the yardstick it's graded
    against (T-108)."""
    conn = quant_seed(memory_quant_db, n_assets=5, n_days=260, with_dividends=False)
    as_of = conn.execute("SELECT MAX(obs_date) FROM price_observation").fetchone()[0]
    conn.execute(
        "INSERT INTO rule_catalog (rule_id, description, severity, created_at) "
        "VALUES ('R1', 'x', 'HARD', '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO veto (asset_id, rule_id, severity, raised_on, last_seen_on, detected_at) "
        "VALUES (4, 'R1', 'HARD', '2024-06-01', '2024-06-01', ?)",
        (as_of,),
    )
    conn.commit()
    settings = QuantSettings(
        db_path=Path(":memory:"), min_history_days=200, liquidity_min_dollar_volume=0.0
    )
    book_gate = settings_gate(conn, settings, as_of=as_of)
    bench_gate = benchmark_gate(conn, settings, as_of=as_of)
    assert book_gate.dropped[4] == "hard_veto"
    assert 4 not in book_gate.asset_ids
    assert 4 in bench_gate.asset_ids
    assert 4 not in bench_gate.dropped
