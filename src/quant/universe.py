"""The score-independent benchmark universe gate.

A name is in the Markowitz universe iff it (a) is an index member as of the date,
(b) has at least ``min_history_days`` daily return observations, (c) clears a
median-dollar-volume liquidity floor, and (optionally) (d) is not under a T-1 HARD
veto. None of these read ``score_snapshot`` / ``cycle_ranking`` / any blended
score, so the benchmark stays an independent control -- a property pinned by
``tests/test_quant_gate``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from quant.db import hard_vetoed_as_of, load_universe_asset_ids


@dataclass
class GateResult:
    asset_ids: list[int]
    dropped: dict[int, str] = field(default_factory=dict)  # asset_id -> reason


def _t_minus_1(iso_date: str) -> str:
    return (date.fromisoformat(iso_date) - timedelta(days=1)).isoformat()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])


def _history_counts(conn: sqlite3.Connection, as_of: str) -> dict[int, int]:
    have_tr = conn.execute("SELECT 1 FROM quant_return_daily LIMIT 1").fetchone() is not None
    if have_tr:
        rows = conn.execute(
            "SELECT asset_id, COUNT(*) n FROM quant_return_daily "
            "WHERE obs_date <= ? AND tr_log_return IS NOT NULL GROUP BY asset_id",
            (as_of,),
        )
    else:
        rows = conn.execute(
            "SELECT asset_id, COUNT(*) n FROM price_observation "
            "WHERE obs_date <= ? AND log_return IS NOT NULL GROUP BY asset_id",
            (as_of,),
        )
    return {int(r["asset_id"]): int(r["n"]) for r in rows}


def _median_dollar_volume(
    conn: sqlite3.Connection, asset_id: int, *, as_of: str, lookback: int
) -> float:
    vals = [
        float(r["dollar_volume"])
        for r in conn.execute(
            "SELECT dollar_volume FROM price_observation "
            "WHERE asset_id = ? AND obs_date <= ? AND dollar_volume IS NOT NULL "
            "ORDER BY obs_date DESC LIMIT ?",
            (asset_id, as_of, lookback),
        )
    ]
    return _median(vals)


def liquidity_data_gate(  # noqa: PLR0913 - all keyword-only knobs with defaults
    conn: sqlite3.Connection,
    *,
    as_of: str,
    universe: str = "SP500",
    min_history_days: int = 504,
    min_dollar_volume: float = 5_000_000.0,
    liquidity_lookback_days: int = 21,
    exclude_hard_vetoed: bool = True,
) -> GateResult:
    members = load_universe_asset_ids(conn, universe=universe, as_of=as_of)
    history = _history_counts(conn, as_of)
    vetoed = hard_vetoed_as_of(conn, _t_minus_1(as_of)) if exclude_hard_vetoed else set()

    kept: list[int] = []
    dropped: dict[int, str] = {}
    for aid in members:
        if history.get(aid, 0) < min_history_days:
            dropped[aid] = "short_history" if aid in history else "no_data"
            continue
        if aid in vetoed:
            dropped[aid] = "hard_veto"
            continue
        if (
            _median_dollar_volume(conn, aid, as_of=as_of, lookback=liquidity_lookback_days)
            < min_dollar_volume
        ):
            dropped[aid] = "illiquid"
            continue
        kept.append(aid)
    return GateResult(asset_ids=sorted(kept), dropped=dropped)
