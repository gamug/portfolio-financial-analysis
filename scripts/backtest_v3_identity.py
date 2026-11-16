"""Round-3 identity check: X0 must equal v2's V3.

    uv run python scripts/backtest_v3_identity.py \
        --v3-db X0_P1.db --v2-db v2v3_P1.db \
        --v3-json X0_P1.json --stored-json /.../out/v2/runs/V3_P1.json \
        --out identity_P1.json

Two independent comparisons:

1. **DB vs DB** (the fresh v3 X0 copy vs a fresh V3 copy run on the v2 code at ``2ff9df7``):
   every ``backtest_summary`` row, every ``backtest_test`` row, every ``book_weight_daily``
   row (four weight columns) and every ``guard_action`` row, all arms, exact.
2. **JSON vs stored V3** (each run's collector output vs the round-2 ``V3_*.json``): summaries,
   tests and the per-session portfolio return series, exact.

Read-only. No per-asset value is printed; weight keys are holding keys and counts are aggregates.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from kg_schema import connect_ro

SUMMARY_COLUMNS = (
    "date_from",
    "date_to",
    "cumulative_return",
    "annualized_return",
    "annualized_vol",
    "ex_post_sharpe",
    "max_drawdown",
    "total_turnover",
    "total_cost_bps",
    "n_rebalances",
    "n_guard_actions",
)
WEIGHT_COLUMNS = (
    "markowitz_target_weight",
    "drifted_weight",
    "pre_guard_weight",
    "final_target_weight",
)


def _books(conn: Any) -> dict[int, str]:
    return {
        int(row["id"]): str(row["arm_code"])
        for row in conn.execute("SELECT id, arm_code FROM strategy_book")
    }


def _summaries(conn: Any) -> dict[tuple[str, str], dict[str, Any]]:
    books = _books(conn)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM backtest_summary"):
        arm = books[int(row["book_id"])]
        out[(arm, str(row["segment"]))] = {key: row[key] for key in SUMMARY_COLUMNS}
    return out


def _tests(conn: Any) -> dict[tuple[str, str, str, str], tuple[Any, ...]]:
    out: dict[tuple[str, str, str, str], tuple[Any, ...]] = {}
    for row in conn.execute("SELECT * FROM backtest_test"):
        key = (str(row["segment"]), str(row["arm_a"]), str(row["arm_b"]), str(row["test"]))
        out[key] = (row["statistic"], row["p_value"], int(row["n_obs"]), row["params_json"])
    return out


def _weights(conn: Any) -> dict[tuple[str, str, str], tuple[float, ...]]:
    books = _books(conn)
    out: dict[tuple[str, str, str], tuple[float, ...]] = {}
    for row in conn.execute("SELECT * FROM book_weight_daily"):
        arm = books[int(row["book_id"])]
        key = (arm, str(row["date"]), str(row["holding_key"]))
        out[key] = tuple(float(row[column]) for column in WEIGHT_COLUMNS)
    return out


def _actions(conn: Any) -> dict[tuple[Any, ...], tuple[float, float]]:
    books = _books(conn)
    out: dict[tuple[Any, ...], tuple[float, float]] = {}
    for row in conn.execute(
        "SELECT a.*, g.layer AS alert_layer, g.rule_id AS alert_rule "
        "FROM guard_action a JOIN guard_alert g ON g.id = a.alert_id"
    ):
        arm = books[int(row["book_id"])]
        key = (
            arm,
            str(row["decision_date"]),
            int(row["alert_layer"]),
            row["asset_id"],
            str(row["alert_rule"]),
            str(row["status"]),
        )
        out[key] = (float(row["weight_before"]), float(row["weight_after"]))
    return out


def _max_error(left: Mapping[Any, Sequence[float]], right: Mapping[Any, Sequence[float]]) -> float:
    errors = [
        abs(a - b)
        for key in set(left) & set(right)
        for a, b in zip(left[key], right[key], strict=True)
    ]
    return max(errors, default=0.0)


def _json_view(payload: dict[str, Any]) -> dict[str, Any]:
    sums = {
        (row["arm"], str(row["segment"])): {key: row[key] for key in SUMMARY_COLUMNS}
        for row in payload["summaries"]
    }
    tests = {
        (str(row["segment"]), str(row["arm_a"]), str(row["arm_b"]), str(row["test"])): (
            row["statistic"],
            row["p_value"],
            int(row["n_obs"]),
            row.get("params_json"),
        )
        for row in payload["tests"]
    }
    return {"summaries": sums, "tests": tests, "daily": payload["daily"]}


def compare(v3_db: Path, v2_db: Path, v3_json: Path, stored_json: Path) -> dict[str, Any]:
    left, right = connect_ro(v3_db), connect_ro(v2_db)
    try:
        ls, rs = _summaries(left), _summaries(right)
        lt, rt = _tests(left), _tests(right)
        lw, rw = _weights(left), _weights(right)
        la, ra = _actions(left), _actions(right)
        window = str(left.execute("SELECT window_id FROM backtest_run").fetchone()[0])
    finally:
        left.close()
        right.close()

    fresh = _json_view(json.loads(v3_json.read_text()))
    stored = _json_view(json.loads(stored_json.read_text()))
    result: dict[str, Any] = {
        "window": window,
        "v3_db": str(v3_db),
        "v2_db": str(v2_db),
        "db_vs_db": {
            "summaries": {"equal": ls == rs, "rows": len(rs)},
            "tests": {"equal": lt == rt, "rows": len(rt)},
            "weights": {
                "equal": lw == rw,
                "rows": len(rw),
                "max_abs_error": _max_error(lw, rw),
            },
            "guard_actions": {
                "equal": la == ra,
                "rows": len(ra),
                "max_abs_error": _max_error(la, ra),
            },
        },
        "json_vs_stored": {
            "summaries": {
                "equal": fresh["summaries"] == stored["summaries"],
                "rows": len(stored["summaries"]),
            },
            "tests": {"equal": fresh["tests"] == stored["tests"], "rows": len(stored["tests"])},
            "daily": {
                "equal": fresh["daily"] == stored["daily"],
                "arms": sorted(stored["daily"]),
            },
        },
    }
    ok = all(
        result["db_vs_db"][key]["equal"]
        for key in ("summaries", "tests", "weights", "guard_actions")
    ) and all(result["json_vs_stored"][key]["equal"] for key in ("summaries", "tests", "daily"))
    result["ok"] = ok
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v3-db", required=True)
    parser.add_argument("--v2-db", required=True)
    parser.add_argument("--v3-json", required=True)
    parser.add_argument("--stored-json", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = compare(
        Path(args.v3_db).expanduser().resolve(),
        Path(args.v2_db).expanduser().resolve(),
        Path(args.v3_json).expanduser().resolve(),
        Path(args.stored_json).expanduser().resolve(),
    )
    target = Path(args.out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
