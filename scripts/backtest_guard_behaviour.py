"""GUARD's measured behaviour on completed backtest run copies (aggregates only).

    uv run python scripts/backtest_guard_behaviour.py <run.db> [<run.db> ...]

Read-only. For each run it prints one JSON object with the quantities that
``docs/backtest.md`` §5 reports: the layer-4 scaler's active sessions and the sessions below the
high-water mark before the arm first exceeds its initial capital, layer-1 re-entries that are
followed by another stop (the whipsaw of a one-session re-entry rule applied daily), the sessions
on which GUARD trades while its NOGUARD twin does not, and each arm's mean excess return,
volatility and Sharpe on the window's own risk-free rate.

No single asset's price or return is read or printed: the only per-asset data touched is the
alert's ``asset_id``, used to pair a re-entry with a later stop of the same name.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Any

GUARD = "TOPN_MV_GUARD_NO_NLP"
NOGUARD = "TOPN_MV_NOGUARD"
TOL = 1e-12
#: A re-entry "held" for at most this many closes before the same name is stopped again.
REENTRY_WINDOW_SESSIONS = 2


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rf(conn: sqlite3.Connection, curve: str, dates: list[str]) -> list[float]:
    rows = conn.execute(
        "SELECT rate_date, annualized_rate FROM risk_free_rate WHERE curve = ? ORDER BY rate_date",
        (curve,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"risk_free_rate has no {curve} rows")
    keys = [str(r["rate_date"]) for r in rows]
    daily = [float(r["annualized_rate"]) / 252.0 for r in rows]
    out: list[float] = []
    for day in dates:
        index = bisect_right(keys, day) - 1
        if index < 0:
            raise SystemExit(f"risk_free_rate has no observation on or before {day}")
        out.append(daily[index])
    return out


def _summarize(returns: list[float], rf: list[float]) -> dict[str, float]:
    excess = [r - f for r, f in zip(returns, rf, strict=True)]
    mean = sum(excess) / len(excess)
    sd = math.sqrt(sum((x - mean) ** 2 for x in excess) / (len(excess) - 1))
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    raw_mean = sum(returns) / len(returns)
    raw_sd = math.sqrt(sum((x - raw_mean) ** 2 for x in returns) / (len(returns) - 1))
    for value in returns:
        nav *= 1.0 + value
        peak = max(peak, nav)
        mdd = max(mdd, 1.0 - nav / peak)
    return {
        "mean_excess": mean,
        "ann_vol": raw_sd * math.sqrt(252.0),
        "sharpe": mean / sd * math.sqrt(252.0) if sd > 0.0 else math.nan,
        "cumulative_return": nav - 1.0,
        "max_drawdown": mdd,
    }


def measure(path: Path) -> dict[str, Any]:  # noqa: C901, PLR0915 - one read-only measurement
    conn = _connect(path)
    try:
        run = conn.execute("SELECT * FROM backtest_run ORDER BY id DESC LIMIT 1").fetchone()
        params = json.loads(str(run["params_json"]))
        guard = params["guard"][GUARD]
        books = {
            str(row["arm_code"]): int(row["id"])
            for row in conn.execute(
                "SELECT id, arm_code FROM strategy_book WHERE backtest_run_id = ?",
                (int(run["id"]),),
            )
        }
        perf: dict[str, list[sqlite3.Row]] = {}
        for code, book_id in books.items():
            perf[code] = conn.execute(
                "SELECT date, net_return, nav, turnover, cost_return FROM book_daily_performance "
                "WHERE book_id = ? ORDER BY date",
                (book_id,),
            ).fetchall()
        dates = [str(row["date"]) for row in perf[GUARD]]
        rf = _rf(conn, str(params.get("rf_curve") or "FF_RF_DAILY"), dates)

        # -- layer 4: the alert on every session the scaler is below one.
        l4 = {
            str(row["date"]): float(row["value"])
            for row in conn.execute(
                "SELECT a.date AS date, a.value AS value FROM guard_alert a "
                "JOIN guard_action x ON x.alert_id = a.id "
                "WHERE x.book_id = ? AND a.layer = 4",
                (books[GUARD],),
            ).fetchall()
        }
        peak = 1.0
        max_err = 0.0
        missing = 0
        extra = 0
        below = 0
        first_new_high: str | None = None
        for row in perf[GUARD]:
            day = str(row["date"])
            nav = float(row["nav"])
            peak = max(peak, nav)
            scale = min(1.0, max(0.0, guard["m"] * (nav - guard["alpha"] * peak) / nav))
            if scale < 1.0:
                below += 1
                if day in l4:
                    max_err = max(max_err, abs(l4[day] - scale))
                else:
                    missing += 1
            elif day in l4:
                extra += 1
            if first_new_high is None and nav > 1.0:
                first_new_high = day
        before_new_high = dates.index(first_new_high) if first_new_high is not None else len(dates)

        # -- layer 1: an APPLIED re-entry followed by another stop of the same name.
        alerts: dict[int, list[tuple[str, str, str]]] = {}
        for row in conn.execute(
            "SELECT a.date AS date, a.asset_id AS asset_id, a.rule_id AS rule_id, "
            "x.status AS status FROM guard_alert a JOIN guard_action x ON x.alert_id = a.id "
            "WHERE x.book_id = ? AND a.layer = 1 ORDER BY a.date",
            (books[GUARD],),
        ).fetchall():
            alerts.setdefault(int(row["asset_id"]), []).append(
                (str(row["date"]), str(row["rule_id"]), str(row["status"]))
            )
        reentries = [
            (asset_id, day)
            for asset_id, events in alerts.items()
            for day, rule, _status in events
            if rule == "L1_REENTRY"
        ]
        applied = [
            (asset_id, day)
            for asset_id, events in alerts.items()
            for day, rule, status in events
            if rule == "L1_REENTRY" and status == "APPLIED"
        ]
        index = {day: position for position, day in enumerate(dates)}
        followed = 0
        within_two = 0
        for asset_id, day in applied:
            later = [
                stop_day
                for stop_day, rule, _status in alerts[asset_id]
                if rule == "L1_STOP_LOSS" and stop_day > day
            ]
            if later:
                followed += 1
                # The re-entry is *held* for at most the two closes after the re-entry close
                # before the same name is stopped again.
                if index[later[0]] - index[day] <= REENTRY_WINDOW_SESSIONS:
                    within_two += 1

        # -- GUARD's own trades: NOGUARD does not trade on the session.
        guard_perf = perf[GUARD]
        twin_perf = perf[NOGUARD]
        own = [
            (g, n)
            for g, n in zip(guard_perf, twin_perf, strict=True)
            if float(g["turnover"]) > TOL and float(n["turnover"]) <= TOL
        ]
        own_turnover = sum(float(g["turnover"]) for g, _ in own)
        own_cost_bps = sum(float(g["cost_return"]) for g, _ in own) * 10_000.0

        return {
            "run": path.stem,
            "window": str(run["window_id"]),
            "n_sessions": len(dates),
            "l4": {
                "n_alert_sessions": len(l4),
                "n_sessions_below_one": below,
                "n_missing": missing,
                "n_extra": extra,
                "max_abs_value_error": max_err,
                "first_new_high": first_new_high,
                "n_sessions_before_first_new_high": before_new_high,
            },
            "l1": {
                "n_reentries": len(reentries),
                "n_applied_reentries": len(applied),
                "n_followed_by_stop": followed,
                "n_followed_by_stop_within_two_sessions": within_two,
            },
            "guard_only_trades": {
                "n_sessions": len(own),
                "turnover": own_turnover,
                "cost_bps": own_cost_bps,
            },
            "arms": {
                code: _summarize([float(r["net_return"]) for r in rows], rf)
                for code, rows in perf.items()
            },
        }
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    for argument in argv:
        print(json.dumps(measure(Path(argument)), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
