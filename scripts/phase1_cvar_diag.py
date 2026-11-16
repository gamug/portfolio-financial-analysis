"""Phase 1 §1.2 diagnostic: the min-CVaR solution under log and simple scenario units.

    uv run python scripts/phase1_cvar_diag.py --out DIR

Reads the kept L1 (log) and L2 (simple) FINAL copies, rebuilds each decision's scenario matrix
from ``quant_return_daily`` (the last 756 sessions <= the decision, dense with zero fill), and
reports max |dw| and the CVaR95 difference between the two solutions.  Aggregates only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np


def _weights(conn: sqlite3.Connection, portfolio_id: int) -> dict[int, float]:
    rows = conn.execute(
        "SELECT asset_id, weight FROM quant_position WHERE portfolio_id = ? AND valid_to IS NULL",
        (portfolio_id,),
    ).fetchall()
    return {int(row[0]): float(row[1]) for row in rows}


def _scenarios(
    conn: sqlite3.Connection, decision: str, asset_ids: list[int], *, simple: bool
) -> np.ndarray:
    sessions = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT obs_date FROM quant_return_daily WHERE obs_date <= ? "
            "ORDER BY obs_date DESC LIMIT 756",
            (decision,),
        )
    ]
    sessions.sort()
    placeholders = ",".join("?" for _ in asset_ids)
    rows = conn.execute(
        f"SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "  # noqa: S608
        f"WHERE obs_date >= ? AND obs_date <= ? AND asset_id IN ({placeholders}) "
        "AND tr_log_return IS NOT NULL",
        (sessions[0], decision, *asset_ids),
    ).fetchall()
    matrix = np.zeros((len(sessions), len(asset_ids)))
    index = {asset_id: i for i, asset_id in enumerate(asset_ids)}
    day_index = {day: i for i, day in enumerate(sessions)}
    for asset_id, day, value in rows:
        matrix[day_index[str(day)], index[int(asset_id)]] = float(value)
    return np.expm1(matrix) if simple else matrix


def _cvar(portfolio: np.ndarray) -> float:
    tail = max(1, int(np.floor(0.05 * len(portfolio))))
    return float(-np.mean(np.sort(portfolio)[:tail]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    for window in ("P1", "H1", "P3", "H2", "P2", "H3"):
        paths = {
            "log": out / "dbs" / f"L1-FINAL-{window}.db",
            "simple": out / "dbs" / f"L2-FINAL-{window}.db",
        }
        if not all(path.is_file() for path in paths.values()):
            raise SystemExit(f"missing L1/L2 copy for {window}: {paths}")
        conns = {
            key: sqlite3.connect(f"file:{path}?mode=ro", uri=True) for key, path in paths.items()
        }
        try:
            decisions = [
                str(row[0])
                for row in conns["log"].execute(
                    "SELECT r.decision_date FROM book_rebalance r "
                    "JOIN strategy_book b ON b.id = r.book_id "
                    "WHERE b.arm_code = 'TOPN_MV_NOGUARD' AND r.quant_portfolio_id IS NOT NULL "
                    "ORDER BY r.decision_date"
                )
            ]
            for decision in decisions:
                portfolio = {
                    key: conns[key]
                    .execute(
                        "SELECT r.quant_portfolio_id FROM book_rebalance r "
                        "JOIN strategy_book b ON b.id = r.book_id "
                        "WHERE b.arm_code = 'TOPN_MV_NOGUARD' AND r.decision_date = ?",
                        (decision,),
                    )
                    .fetchone()
                    for key in conns
                }
                ids = {key: _weights(conns[key], int(portfolio[key][0])) for key in conns}
                common = sorted(set(ids["log"]) | set(ids["simple"]))
                delta = max(
                    abs(ids["log"].get(asset_id, 0.0) - ids["simple"].get(asset_id, 0.0))
                    for asset_id in common
                )
                matrix_simple = _scenarios(conns["simple"], decision, common, simple=True)
                matrix_log = _scenarios(conns["log"], decision, common, simple=False)
                w_log = np.asarray([ids["log"].get(asset_id, 0.0) for asset_id in common])
                w_simple = np.asarray([ids["simple"].get(asset_id, 0.0) for asset_id in common])
                cvar_log_units = _cvar(matrix_log @ w_log)
                cvar_simple_units = _cvar(matrix_simple @ w_simple)
                rows.append(
                    {
                        "window": window,
                        "decision": decision,
                        "max_abs_dw": float(delta),
                        "cvar_log_solution": cvar_log_units,
                        "cvar_simple_solution": cvar_simple_units,
                        "cvar_diff": cvar_simple_units - cvar_log_units,
                    }
                )
        finally:
            for conn in conns.values():
                conn.close()
    payload = {
        "decisions": len(rows),
        "max_abs_dw": max(row["max_abs_dw"] for row in rows),
        "max_abs_cvar_diff": max(abs(row["cvar_diff"]) for row in rows),
        "mean_cvar_diff": float(np.mean([row["cvar_diff"] for row in rows])),
        "rows": rows,
    }
    (out / "cvar_diag.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(
        json.dumps({key: payload[key] for key in ("decisions", "max_abs_dw", "max_abs_cvar_diff")})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
