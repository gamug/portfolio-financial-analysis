"""Round-4 §5 data report: missing bars in the tracking spans, and SPLIT counts.

    uv run python scripts/backtest_v4_data_report.py --db X0_P1.db ... --tiingo DIR

Read-only. The first part is computed from the run copies' ``price_daily``; the second
needs the private Tiingo CSV (counts only, nothing is copied). If the Tiingo file is not
reachable the script prints the missing-bar part and exits 2: the plan makes that a stop.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from cycle.backtest.driver import schedule_for, window_spec
from cycle.guard.basing import TRACKING_SESSIONS
from kg_schema import connect_ro

TIINGO_DEFAULT = Path("/workspaces/thesis/data/private/tiingo")


def _sessions(conn: Any, date_from: str, date_to: str) -> list[str]:
    return [
        str(row["date"])
        for row in conn.execute(
            "SELECT DISTINCT date FROM price_daily WHERE date >= ? AND date <= ? ORDER BY date",
            (date_from, date_to),
        )
    ]


def _targets(conn: Any, decision: str, core_asset_id: int | None) -> set[int]:
    rows = conn.execute(
        "SELECT w.asset_id, w.markowitz_target_weight FROM book_weight_daily w "
        "JOIN strategy_book b ON b.id = w.book_id "
        "WHERE b.arm_code = 'TOPN_MV_NOGUARD' AND w.date = ? "
        "AND w.markowitz_target_weight > 0",
        (decision,),
    ).fetchall()
    return {
        int(row["asset_id"])
        for row in rows
        if row["asset_id"] is not None and int(row["asset_id"]) != core_asset_id
    }


def _missing_bars(conn: Any, window: str, core_asset_id: int | None) -> dict[str, Any]:
    spec = window_spec(window)
    decisions = [row.decision_date for row in schedule_for(spec)]
    prices = {
        (int(row["asset_id"]), str(row["date"]))
        for row in conn.execute("SELECT asset_id, date FROM price_daily")
    }
    sessions = _sessions(conn, "2000-01-01", "2100-01-01")
    index = {session: position for position, session in enumerate(sessions)}
    targets = {decision: _targets(conn, decision, core_asset_id) for decision in decisions}
    assets = {
        int(row["id"]): str(row["ticker"]) for row in conn.execute("SELECT id, ticker FROM assets")
    }
    pairs: set[tuple[int, str]] = set()
    spans = 0
    for position, decision in enumerate(decisions):
        names = targets[decision]
        later_decisions = decisions[position + 1 :]
        for asset_id in names:
            start = max(index[decision] - TRACKING_SESSIONS, 0)
            end = max(position for position, day in enumerate(sessions) if day <= spec.d_end)
            for later in later_decisions:
                if asset_id not in targets[later]:
                    end = index[later] - 1
                    break
            for session in sessions[start : end + 1]:
                pairs.add((asset_id, session))
            spans += 1
    missing = {
        (asset_id, session) for asset_id, session in pairs if (asset_id, session) not in prices
    }
    per_name = {
        assets.get(asset_id, str(asset_id)): sum(1 for pair in missing if pair[0] == asset_id)
        for asset_id in sorted({pair[0] for pair in pairs})
    }
    return {
        "window": window,
        "entries": spans,
        "names": len({pair[0] for pair in pairs}),
        "pairs": len(pairs),
        "missing": len(missing),
        "missing_share": len(missing) / len(pairs) if pairs else 0.0,
        "missing_by_name": dict(sorted(per_name.items())),
    }


def _find_tiingo(path: Path) -> Path | None:
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(path.glob("*.csv"))
        return candidates[0] if candidates else None
    return None


def _split_counts(conn: Any, ever_names: set[str], tiingo: Path | None) -> dict[str, Any]:
    assets = {
        str(row["ticker"]).upper(): int(row["id"])
        for row in conn.execute("SELECT id, ticker FROM assets")
    }
    db_counts: dict[str, int] = {}
    for ticker in sorted(ever_names):
        asset_id = assets.get(ticker.upper())
        if asset_id is None:
            db_counts[ticker] = 0
            continue
        db_counts[ticker] = int(
            conn.execute(
                "SELECT COUNT(*) FROM corporate_action "
                "WHERE asset_id = ? AND action_type = 'SPLIT'",
                (asset_id,),
            ).fetchone()[0]
        )
    if tiingo is None:
        return {
            "tiingo_path": None,
            "tiingo_reachable": False,
            "split_rows_db": db_counts,
            "split_rows_tiingo": None,
        }
    tiingo_counts: dict[str, int] = {}
    with tiingo.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = {str(name).casefold(): name for name in (reader.fieldnames or [])}
        if "splitfactor" not in fields or "ticker" not in fields:
            raise SystemExit(
                f"{tiingo}: expected a ticker and a splitFactor column, got {reader.fieldnames}"
            )
        ticker_field = fields["ticker"]
        split_field = fields["splitFactor"]
        for row in reader:
            ticker = str(row[ticker_field]).upper()
            if ticker not in ever_names:
                continue
            try:
                factor = float(row[split_field])
            except (TypeError, ValueError):
                continue
            if factor != 1.0:
                tiingo_counts[ticker] = tiingo_counts.get(ticker, 0) + 1
    return {
        "tiingo_path": str(tiingo),
        "tiingo_reachable": True,
        "split_rows_db": db_counts,
        "split_rows_tiingo": dict(sorted(tiingo_counts.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", required=True)
    parser.add_argument("--tiingo", default=str(TIINGO_DEFAULT))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    reports = []
    ever_names: set[str] = set()
    for path in args.db:
        conn = connect_ro(Path(path))
        try:
            run = conn.execute("SELECT window_id, params_json FROM backtest_run").fetchone()
            params = json.loads(str(run["params_json"]))
            report = _missing_bars(conn, str(run["window_id"]), params.get("core_asset_id"))
            report["run"] = Path(path).name
            reports.append(report)
            ever_names.update(report["missing_by_name"])
        finally:
            conn.close()
    tiingo = _find_tiingo(Path(args.tiingo).expanduser())
    split_conn = connect_ro(Path(args.db[0]))
    try:
        split_report = _split_counts(split_conn, ever_names, tiingo)
    finally:
        split_conn.close()
    payload = {"missing_bars": reports, "splits": split_report}
    text = json.dumps(payload, indent=1) + "\n"
    if args.out:
        Path(args.out).expanduser().resolve().write_text(text)
    print(text)
    if tiingo is None:
        print(
            f"backtest_v4_data_report: Tiingo file not reachable under {args.tiingo}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
