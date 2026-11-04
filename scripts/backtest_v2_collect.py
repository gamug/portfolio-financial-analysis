"""Collect one v2 backtest run's aggregates before its copy is deleted (V2_PLAN §3).

    uv run python scripts/backtest_v2_collect.py <run.db> <out.json>

Read-only. The JSON holds the run's parameters (window, configuration, filter dates, series
sha256), every ``backtest_summary`` row, every ``backtest_test`` row, the per-session **portfolio**
net returns per arm (needed by the cross-run tests of §4(i); never a per-asset value), the run's
daily risk-free rate and the SP500_EW_INTERNAL benchmark return. No price or return of a single
asset is read or written.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cycle.backtest.driver import load_rf
from kg_schema import connect_ro

#: The params_json keys the grid/report needs.
PARAM_KEYS = (
    "window",
    "blend",
    "tilt",
    "core_weight",
    "trend_filter",
    "core_asset_id",
    "core_series_path",
    "core_series_sha256",
    "core_manifest_declared_sha256",
    "trend_filter_dates",
    "tilt_triggers",
    "market_series_sha256",
    "warmup_steps",
    "cost_bps",
    "top_n",
    "returns_engine_version",
    "rf_curve",
)


def _markowitz_hash(conn: Any) -> str | None:
    """sha256 of ``MARKOWITZ_BASE``'s target weights: ``(date, holding_key, weight)``, sorted."""
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
        benchmark: dict[str, float] = {}
        for row in conn.execute(
            "SELECT book_id, date, net_return, benchmark_return "
            "FROM book_daily_performance ORDER BY date"
        ):
            daily.setdefault(books[int(row["book_id"])], {})[str(row["date"])] = float(
                row["net_return"]
            )
            if row["benchmark_return"] is not None:
                benchmark[str(row["date"])] = float(row["benchmark_return"])
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
            "rf": rf,
            "benchmark": benchmark,
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
                "core_weight": payload["params"]["core_weight"],
                "trend_filter": payload["params"]["trend_filter"],
                "status": payload["run"]["status"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
