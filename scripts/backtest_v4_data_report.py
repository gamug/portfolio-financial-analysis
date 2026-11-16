"""Round-4 §5 data report: missing bars, impossible jumps and the ten known splits.

    uv run python scripts/backtest_v4_data_report.py --db X0_P1.db ... --out report.json

Read-only, counts only (no prices). §5 was amended before any run: the private Tiingo file
covers only the 33 leaver lines, so the split check is (a) close-to-close jumps below -40% or
above +67% with no SPLIT row on that date, and (b) ten known 2022-2025 splits present with
their stated ratio. The run proceeds only if (a) is empty and (b) is complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cycle.backtest.driver import schedule_for, window_spec
from cycle.guard.basing import TRACKING_SESSIONS
from kg_schema import connect_ro

JUMP_LOW = -0.40
JUMP_HIGH = 0.67
KNOWN_SPLITS: tuple[tuple[str, str, float], ...] = (
    ("AMZN", "2022-06-06", 20.0),
    ("GOOGL", "2022-07-18", 20.0),
    ("TSLA", "2022-08-25", 3.0),
    ("NVDA", "2024-06-10", 10.0),
    ("AVGO", "2024-07-15", 10.0),
    ("CMG", "2024-06-26", 50.0),
    ("WMT", "2024-02-26", 3.0),
    ("ORLY", "2025-06-10", 15.0),
    ("LRCX", "2024-10-03", 10.0),
    ("CTAS", "2024-09-12", 4.0),
)


def _span_pairs(conn: Any, window: str, core_asset_id: int | None) -> set[tuple[int, str]]:
    """The (name, session) pairs of every satellite tracking span in the window."""
    spec = window_spec(window)
    decisions = [row.decision_date for row in schedule_for(spec)]
    sessions = [
        str(row["date"])
        for row in conn.execute(
            "SELECT DISTINCT date FROM price_daily WHERE date >= ? AND date <= ? ORDER BY date",
            ("2000-01-01", spec.d_end),
        )
    ]
    index = {session: position for position, session in enumerate(sessions)}
    targets: dict[str, set[int]] = {}
    for decision in decisions:
        rows = conn.execute(
            "SELECT w.asset_id FROM book_weight_daily w JOIN strategy_book b ON b.id = w.book_id "
            "WHERE b.arm_code = 'TOPN_MV_NOGUARD' AND w.date = ? "
            "AND w.markowitz_target_weight > 0 AND w.asset_id IS NOT NULL",
            (decision,),
        ).fetchall()
        targets[decision] = {
            int(row["asset_id"]) for row in rows if int(row["asset_id"]) != core_asset_id
        }
    pairs: set[tuple[int, str]] = set()
    window_end = len(sessions) - 1
    for position, decision in enumerate(decisions):
        start = max(index[decision] - TRACKING_SESSIONS, 0)
        for asset_id in targets[decision]:
            end = window_end
            for later in decisions[position + 1 :]:
                if asset_id not in targets[later]:
                    end = index[later] - 1
                    break
            for session in sessions[start : end + 1]:
                pairs.add((asset_id, session))
    return pairs


def _prices(conn: Any) -> tuple[dict[int, dict[str, float]], dict[tuple[int, str], bool]]:
    closes: dict[int, dict[str, float]] = {}
    splits: dict[tuple[int, str], bool] = {}
    for row in conn.execute("SELECT asset_id, date, close FROM price_daily"):
        closes.setdefault(int(row["asset_id"]), {})[str(row["date"])] = float(row["close"])
    for row in conn.execute(
        "SELECT asset_id, ex_date FROM corporate_action WHERE action_type = 'SPLIT'"
    ):
        splits[(int(row["asset_id"]), str(row["ex_date"]))] = True
    return closes, splits


def _report(conn: Any, window: str, core_asset_id: int | None) -> dict[str, Any]:
    pairs = _span_pairs(conn, window, core_asset_id)
    closes, splits = _prices(conn)
    prices = {
        (asset_id, day): value
        for asset_id, series in closes.items()
        for day, value in series.items()
    }
    missing = {(asset, day) for asset, day in pairs if (asset, day) not in prices}
    jumps: list[str] = []
    for asset_id, session in pairs:
        if (asset_id, session) not in prices:
            continue
        series = closes[asset_id]
        earlier = [day for day in series if day < session]
        if not earlier:
            continue
        previous = max(earlier)
        change = series[session] / series[previous] - 1.0
        if (change < JUMP_LOW or change > JUMP_HIGH) and (asset_id, session) not in splits:
            jumps.append(session)
    return {
        "window": window,
        "pairs": len(pairs),
        "missing": len(missing),
        "missing_share": len(missing) / len(pairs) if pairs else 0.0,
        "jumps": len(jumps),
        "jump_dates": [min(jumps), max(jumps)] if jumps else [],
    }


def _known_splits(conn: Any) -> dict[str, Any]:
    ids = {
        str(row["ticker"]).upper(): int(row["id"])
        for row in conn.execute("SELECT id, ticker FROM assets")
    }
    missing: list[str] = []
    present = 0
    for ticker, ex_date, ratio in KNOWN_SPLITS:
        asset_id = ids.get(ticker)
        if asset_id is None:
            missing.append(ticker)
            continue
        row = conn.execute(
            "SELECT 1 FROM corporate_action WHERE asset_id = ? AND action_type = 'SPLIT' "
            "AND ex_date = ? AND ABS(value - ?) <= 1e-9",
            (asset_id, ex_date, ratio),
        ).fetchone()
        if row is None:
            missing.append(ticker)
        else:
            present += 1
    return {"checked": len(KNOWN_SPLITS), "present": present, "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    reports = []
    for path in args.db:
        conn = connect_ro(Path(path))
        try:
            run = conn.execute("SELECT window_id, params_json FROM backtest_run").fetchone()
            params = json.loads(str(run["params_json"]))
            report = _report(conn, str(run["window_id"]), params.get("core_asset_id"))
            report["run"] = Path(path).name
            reports.append(report)
        finally:
            conn.close()
    conn = connect_ro(Path(args.db[0]))
    try:
        splits = _known_splits(conn)
    finally:
        conn.close()
    payload = {"missing_bars": reports, "known_splits": splits}
    text = json.dumps(payload, indent=1) + "\n"
    if args.out:
        Path(args.out).expanduser().resolve().write_text(text)
    print(text)
    jump_windows = [row["window"] for row in reports if row["jumps"]]
    if jump_windows:
        print(
            f"backtest_v4_data_report: jumps without a SPLIT row in {jump_windows}",
            file=sys.stderr,
        )
        return 3
    if splits["missing"]:
        print(
            f"backtest_v4_data_report: known splits missing for {splits['missing']}",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
