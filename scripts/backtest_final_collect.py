"""Collect one final-round backtest run's aggregates before its copy is deleted.

    uv run python scripts/backtest_final_collect.py <run.db> <out.json>

Read-only. As round 4's collector plus the final-round parameters, the per-decision
weighting records and the layer-3 / layer-4 scaling counts. Portfolio aggregates only:
no per-asset value and no SPY price/return series is read or written.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cycle.backtest.driver import load_rf, next_session
from cycle.backtest.exits import TERMINAL_RETURN
from kg_schema import connect_ro

#: A weight below this is not a holding (the engine's own order tolerance is 1e-12).
_W_EPS = 1e-12

PARAM_KEYS = (
    "window",
    "blend",
    "tilt",
    "core_weight",
    "trend_filter",
    "pool_m",
    "core_source",
    "filter_exit",
    "stop_rule",
    "satellite_weighting",
    "guard_layers",
    "vol_target",
    "filter_series",
    "cvar_scenarios",
    "pool_mode",
    "exit_events_path",
    "exit_events_sha256",
    "exit_event_count",
    "exit_asset_count",
    "exit_recoveries_sha256",
    "core_asset_id",
    "core_series_path",
    "core_series_sha256",
    "core_manifest_declared_sha256",
    "trend_filter_dates",
    "pool_decisions",
    "rp",
    "weighting",
    "bp",
    "distance_cells",
    "core_stop_actions",
    "tilt_triggers",
    "market_series_sha256",
    "warmup_steps",
    "cost_bps",
    "top_n",
    "returns_engine_version",
    "rf_curve",
)


def _markowitz_hash(conn: Any) -> str | None:
    rows: list[Any] = conn.execute(
        "SELECT w.date, w.holding_key, w.markowitz_target_weight "
        "FROM book_weight_daily w JOIN strategy_book b ON b.id = w.book_id "
        "WHERE b.arm_code = 'MARKOWITZ_BASE' ORDER BY w.date, w.holding_key"
    ).fetchall()
    if not rows:
        return None
    payload = [
        [str(row["date"]), str(row["holding_key"]), float(row["markowitz_target_weight"])]
        for row in rows
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _equity(conn: Any, books: dict[int, str]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT b.arm_code, s.segment, p.date, p.n_equity "
        "FROM book_daily_performance p "
        "JOIN strategy_book b ON b.id = p.book_id "
        "JOIN backtest_summary s ON s.book_id = p.book_id "
        "AND p.date BETWEEN s.date_from AND s.date_to "
        "ORDER BY b.arm_code, s.segment, p.date"
    ):
        arm = str(row["arm_code"])
        segment = str(row["segment"])
        values = out.setdefault(arm, {}).setdefault(
            segment, {"min": int(row["n_equity"]), "max": int(row["n_equity"]), "last": 0}
        )
        values["min"] = min(values["min"], int(row["n_equity"]))
        values["max"] = max(values["max"], int(row["n_equity"]))
        values["last"] = int(row["n_equity"])
    return out


def _layer_scales(conn: Any, books: dict[int, str]) -> dict[str, dict[str, Any]]:
    """Per arm and layer 3/4: applied sessions, mean scale and minimum scale (aggregates)."""
    out: dict[str, dict[str, Any]] = {}
    for book_id, arm in books.items():
        for row in conn.execute(
            "SELECT g.layer, COUNT(DISTINCT a.decision_date) AS sessions, "
            "AVG(g.value) AS mean_scale, MIN(g.value) AS min_scale "
            "FROM guard_action a JOIN guard_alert g ON g.id = a.alert_id "
            "WHERE a.book_id = ? AND a.status = 'APPLIED' AND g.layer IN (3, 4) "
            "GROUP BY g.layer ORDER BY g.layer",
            (book_id,),
        ):
            out.setdefault(arm, {})[f"layer{int(row['layer'])}"] = {
                "sessions_scaled": int(row["sessions"]),
                "mean_scale": float(row["mean_scale"]),
                "min_scale": float(row["min_scale"]),
            }
    return out


def _exit_records(conn: Any, books: dict[int, str], params: dict[str, Any]) -> list[dict[str, Any]]:
    """Every declared exit this run held, per arm: ticker/date/type/basis/weight/source.

    Weights and dates are portfolio data; the table carries no per-asset price or return.  The
    residual drift on the recognition session is the liquidation turnover, and its cost at the
    run's cost_bps is the exit's share of that session's cost."""
    path = params.get("exit_events_path")
    if not path:
        return []
    table = json.loads(Path(str(path)).read_text())
    events = table.get("events", [])
    if not events:
        return []
    tickers = {
        int(row["asset_id"]): str(row["ticker"])
        for row in conn.execute(
            "SELECT w.asset_id, a.ticker FROM book_weight_daily w "
            "JOIN assets a ON a.id = w.asset_id WHERE w.asset_id IS NOT NULL GROUP BY w.asset_id"
        )
    }
    out: list[dict[str, Any]] = []
    cost_bps = float(params.get("cost_bps", 10.0))
    for event in events:
        asset_id = event.get("asset_id")
        if asset_id is None:
            continue
        recognition = event.get("recognition_date")
        if recognition is None and event.get("last_return_session"):
            recognition = next_session(str(event["last_return_session"]))
        if recognition is None:
            continue
        for book_id, arm in books.items():
            row = conn.execute(
                "SELECT date, drifted_weight, final_target_weight FROM book_weight_daily "
                "WHERE book_id = ? AND asset_id = ? AND date = ?",
                (book_id, int(asset_id), str(recognition)),
            ).fetchone()
            if row is None:
                continue
            residual = float(row["drifted_weight"] or 0.0)
            if residual <= _W_EPS:
                continue
            before = conn.execute(
                "SELECT final_target_weight FROM book_weight_daily "
                "WHERE book_id = ? AND asset_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
                (book_id, int(asset_id), str(recognition)),
            ).fetchone()
            out.append(
                {
                    "arm": arm,
                    "ticker": tickers.get(int(asset_id), event.get("ticker")),
                    "stint_end": event.get("stint_end"),
                    "recognition_date": str(recognition),
                    "type": event.get("type"),
                    "valuation_basis": event.get("valuation_basis"),
                    "terminal_return": TERMINAL_RETURN.get(str(event.get("valuation_basis"))),
                    "weight_before": float(before["final_target_weight"]) if before else 0.0,
                    "residual_weight": residual,
                    "liquidation_cost_bps": cost_bps * residual,
                    "source": event.get("source"),
                }
            )
    return out


def main(db: str, out: str) -> int:
    conn = connect_ro(Path(db).expanduser().resolve())
    try:
        run = conn.execute("SELECT * FROM backtest_run").fetchone()
        if run is None:
            raise SystemExit(f"{db}: no backtest_run row")
        params = json.loads(str(run["params_json"]))
        books = {
            int(row["id"]): str(row["arm_code"])
            for row in conn.execute("SELECT id, arm_code FROM strategy_book")
        }
        summaries: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM backtest_summary ORDER BY segment, book_id"):
            item = dict(row)
            item["arm"] = books[int(item.pop("book_id"))]
            summaries.append(item)
        tests = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM backtest_test ORDER BY segment, arm_a, arm_b, test"
            )
        ]
        daily: dict[str, dict[str, float]] = {}
        daily_detail: dict[str, dict[str, dict[str, float]]] = {}
        benchmark: dict[str, float] = {}
        for row in conn.execute(
            "SELECT book_id, date, gross_return, turnover, cost_return, net_return, "
            "cash_weight, n_equity, benchmark_return "
            "FROM book_daily_performance ORDER BY date"
        ):
            arm = books[int(row["book_id"])]
            daily.setdefault(arm, {})[str(row["date"])] = float(row["net_return"])
            daily_detail.setdefault(arm, {})[str(row["date"])] = {
                "gross": float(row["gross_return"]),
                "turnover": float(row["turnover"]),
                "cost": float(row["cost_return"]),
                "cash": float(row["cash_weight"]),
                "n_equity": int(row["n_equity"]),
            }
            if row["benchmark_return"] is not None:
                benchmark[str(row["date"])] = float(row["benchmark_return"])
        target_cash: dict[str, dict[str, float]] = {}
        for row in conn.execute(
            "SELECT b.arm_code, w.date, w.final_target_weight FROM book_weight_daily w "
            "JOIN strategy_book b ON b.id = w.book_id WHERE w.holding_key = 'CASH' "
            "ORDER BY w.date"
        ):
            target_cash.setdefault(str(row["arm_code"]), {})[str(row["date"])] = float(
                row["final_target_weight"]
            )
        exits = _exit_records(conn, books, params)
        sessions = sorted({day for series in daily.values() for day in series})
        rf, carried = load_rf(conn, str(params["rf_curve"]), sessions)
        payload: dict[str, Any] = {
            "run": {
                "id": int(run["id"]),
                "status": str(run["status"]),
                "window": str(run["window_id"]),
                "formation_date": run["formation_date"],
                "code_version": run["code_version"],
                "input_db_sha256": run["input_db_sha256"],
                "created_at": run["created_at"],
                "rf_carried_sessions": carried,
            },
            "params": {key: params.get(key) for key in PARAM_KEYS},
            "arms": sorted(books.values()),
            "summaries": summaries,
            "tests": tests,
            "daily": daily,
            "daily_detail": daily_detail,
            "target_cash": target_cash,
            "exits": exits,
            "rf": rf,
            "benchmark": benchmark,
            "equity": _equity(conn, books),
            "layer_scales": _layer_scales(conn, books),
            "markowitz_weights_sha256": _markowitz_hash(conn),
        }
    finally:
        conn.close()
    target = Path(out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=1) + "\n")
    print(
        json.dumps(
            {
                "out": str(target),
                "window": payload["run"]["window"],
                "stop_rule": payload["params"]["stop_rule"],
                "satellite_weighting": payload["params"]["satellite_weighting"],
                "guard_layers": payload["params"]["guard_layers"],
                "vol_target": payload["params"]["vol_target"],
                "cost_bps": payload["params"]["cost_bps"],
                "status": payload["run"]["status"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
