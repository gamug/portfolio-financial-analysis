"""V2_PLAN §2 identity check: a V0 run must reproduce its v1 run on the v1 arms.

    uv run python scripts/backtest_v2_identity.py <v0.db> <v1.db> <out.json>

Read-only. Compares, for ``MARKOWITZ_BASE`` / ``TOPN_MV_NOGUARD`` / ``TOPN_MV_GUARD_NO_NLP``:

* every ``backtest_summary`` row (all columns, exact),
* every v1 ``backtest_test`` row (the three tests and the two v1 pairs; the v2 run's extra
  ``bootstrap_mdd`` rows and the extra GUARD_NO_NLP/BASE pair are not part of v1 and are ignored),
* every ``book_weight_daily`` row (the four weight columns, exact, keyed by date and holding),
* every ``guard_action`` row (status and both weights, exact, keyed by date/layer/asset/rule).

Exit code 1 with the list of differences when anything differs. No per-asset value is printed:
the weight keys are holding keys and the counts are aggregates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from kg_schema import connect_ro

V1_ARMS = ("MARKOWITZ_BASE", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP")
V1_TESTS = ("memmel_jk", "ledoit_wolf", "hac_mean_diff")
V1_PAIRS = {
    ("TOPN_MV_GUARD_NO_NLP", "TOPN_MV_NOGUARD"),
    ("TOPN_MV_NOGUARD", "MARKOWITZ_BASE"),
}
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


def _summaries(conn: Any, books: dict[int, str]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute("SELECT * FROM backtest_summary"):
        arm = books[int(row["book_id"])]
        if arm in V1_ARMS:
            out[(arm, str(row["segment"]))] = {key: row[key] for key in SUMMARY_COLUMNS}
    return out


def _tests(conn: Any) -> dict[tuple[str, str, str, str], tuple[float, float, int]]:
    out: dict[tuple[str, str, str, str], tuple[float, float, int]] = {}
    for row in conn.execute("SELECT * FROM backtest_test"):
        key = (str(row["segment"]), str(row["arm_a"]), str(row["arm_b"]), str(row["test"]))
        if (row["arm_a"], row["arm_b"]) in V1_PAIRS and row["test"] in V1_TESTS:
            out[key] = (float(row["statistic"]), float(row["p_value"]), int(row["n_obs"]))
    return out


def _weights(conn: Any, books: dict[int, str]) -> dict[tuple[str, str, str], tuple[float, ...]]:
    out: dict[tuple[str, str, str], tuple[float, ...]] = {}
    for row in conn.execute("SELECT * FROM book_weight_daily"):
        arm = books[int(row["book_id"])]
        if arm in V1_ARMS:
            key = (arm, str(row["date"]), str(row["holding_key"]))
            out[key] = tuple(float(row[column]) for column in WEIGHT_COLUMNS)
    return out


def _actions(conn: Any, books: dict[int, str]) -> dict[tuple[Any, ...], tuple[float, float]]:
    out: dict[tuple[Any, ...], tuple[float, float]] = {}
    for row in conn.execute(
        "SELECT a.*, g.layer AS alert_layer, g.rule_id AS alert_rule "
        "FROM guard_action a JOIN guard_alert g ON g.id = a.alert_id"
    ):
        arm = books[int(row["book_id"])]
        if arm in V1_ARMS:
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


def _max_error(left: dict[Any, tuple[float, ...]], right: dict[Any, tuple[float, ...]]) -> float:
    errors = [
        abs(a - b)
        for key in set(left) & set(right)
        for a, b in zip(left[key], right[key], strict=True)
    ]
    return max(errors, default=0.0)


def compare(v0_path: Path, v1_path: Path) -> dict[str, Any]:
    left = connect_ro(v0_path)
    right = connect_ro(v1_path)
    try:
        lbooks, rbooks = _books(left), _books(right)
        ls, rs = _summaries(left, lbooks), _summaries(right, rbooks)
        lt, rt = _tests(left), _tests(right)
        lw, rw = _weights(left, lbooks), _weights(right, rbooks)
        la, ra = _actions(left, lbooks), _actions(right, rbooks)
        run = left.execute("SELECT window_id, params_json FROM backtest_run").fetchone()
        v1_run = right.execute("SELECT window_id FROM backtest_run").fetchone()
        if str(run["window_id"]) != str(v1_run["window_id"]):
            raise SystemExit(
                f"{v0_path} is window {run['window_id']}, {v1_path} is {v1_run['window_id']}"
            )
        params = json.loads(str(run["params_json"]))
        return {
            "v0": str(v0_path),
            "v1": str(v1_path),
            "window": str(run["window_id"]),
            "core_weight": params.get("core_weight"),
            "trend_filter": params.get("trend_filter"),
            "v1_arms_present": sorted(set(rbooks.values()) & set(V1_ARMS)),
            "summaries": {
                "equal": ls == rs,
                "rows_v0": len(ls),
                "rows_v1": len(rs),
                "missing": sorted(set(rs) - set(ls)),
                "extra": sorted(set(ls) - set(rs)),
            },
            "tests": {
                "equal": lt == rt,
                "rows_v0": len(lt),
                "rows_v1": len(rt),
            },
            "weights": {
                "equal": lw == rw,
                "rows_v0": len(lw),
                "rows_v1": len(rw),
                "max_abs_error": _max_error(lw, rw),
            },
            "guard_actions": {
                "equal": la == ra,
                "rows_v0": len(la),
                "rows_v1": len(ra),
                "max_abs_error": _max_error(la, ra),
            },
        }
    finally:
        left.close()
        right.close()


def main(v0: str, v1: str, out: str) -> int:
    result = compare(Path(v0).expanduser().resolve(), Path(v1).expanduser().resolve())
    target = Path(out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    ok = all(result[key]["equal"] for key in ("summaries", "tests", "weights", "guard_actions"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
