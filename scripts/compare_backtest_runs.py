"""Compare two sets of backtest run copies (aggregates and event keys only).

    uv run python scripts/compare_backtest_runs.py <old_dir> <new_dir> [run ...]

Read-only. For every run name it matches the two copies by the stable keys (arm + segment,
arm + date, test identity, and the joined GUARD event), never by a row id, and reports:

* whether ``backtest_summary``, ``backtest_test`` and ``book_daily_performance`` are exactly
  equal, column by column and bit for bit;
* which GUARD ``alert``/``action`` rows were added or removed, classified by ``rule_id`` and
  ``status`` (the round A-1 expectation: only ``L1_REENTRY`` / ``NOT_HELD`` added).

A difference in any of the first three sets, or a GUARD difference outside that class, is printed
as a failure with its matched key; the exit status is 1. No per-asset price or return is read:
the event key carries the alert's ``asset_id``, but no price or return column is selected.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

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
PERFORMANCE_COLUMNS = (
    "gross_return",
    "turnover",
    "cost_return",
    "net_return",
    "nav",
    "drawdown",
    "cash_weight",
    "n_equity",
    "benchmark_return",
)
TEST_COLUMNS = ("statistic", "p_value", "n_obs", "params_json")
#: ``old_dir new_dir`` before the optional run names.
_MIN_ARGUMENTS = 2


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _books(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["arm_code"]): int(row["id"])
        for row in conn.execute("SELECT id, arm_code FROM strategy_book").fetchall()
    }


def _summary(conn: sqlite3.Connection, books: dict[str, int]) -> dict[tuple[str, str], tuple]:
    out: dict[tuple[str, str], tuple] = {}
    for arm, book_id in books.items():
        for row in conn.execute(
            "SELECT * FROM backtest_summary WHERE book_id = ?", (book_id,)
        ).fetchall():
            out[(arm, str(row["segment"]))] = tuple(row[column] for column in SUMMARY_COLUMNS)
    return out


def _performance(conn: sqlite3.Connection, books: dict[str, int]) -> dict[tuple[str, str], tuple]:
    out: dict[tuple[str, str], tuple] = {}
    for arm, book_id in books.items():
        for row in conn.execute(
            "SELECT * FROM book_daily_performance WHERE book_id = ?", (book_id,)
        ).fetchall():
            out[(arm, str(row["date"]))] = tuple(row[column] for column in PERFORMANCE_COLUMNS)
    return out


def _tests(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str], tuple]:
    out: dict[tuple[str, str, str, str], tuple] = {}
    for row in conn.execute("SELECT * FROM backtest_test").fetchall():
        key = (str(row["segment"]), str(row["arm_a"]), str(row["arm_b"]), str(row["test"]))
        out[key] = tuple(row[column] for column in TEST_COLUMNS)
    return out


def _guard(conn: sqlite3.Connection, books: dict[str, int]) -> Counter[tuple]:
    """Every joined alert/action as a stable tuple, counted (an identical row cannot repeat)."""
    book_to_arm = {book_id: arm for arm, book_id in books.items()}
    out: Counter[tuple] = Counter()
    for row in conn.execute(
        "SELECT x.book_id AS book_id, a.date AS date, a.layer AS layer, a.asset_id AS asset_id, "
        "a.rule_id AS rule_id, a.value AS value, a.threshold AS threshold, a.state AS state, "
        "x.decision_date AS decision_date, x.effective_date AS effective_date, "
        "x.status AS status, x.weight_before AS weight_before, x.weight_after AS weight_after "
        "FROM guard_alert a JOIN guard_action x ON x.alert_id = a.id"
    ).fetchall():
        key = (
            book_to_arm[int(row["book_id"])],
            str(row["date"]),
            int(row["layer"]),
            None if row["asset_id"] is None else int(row["asset_id"]),
            str(row["rule_id"]),
            None if row["value"] is None else float(row["value"]),
            None if row["threshold"] is None else float(row["threshold"]),
            str(row["state"]),
            str(row["decision_date"]),
            str(row["effective_date"]),
            str(row["status"]),
            None if row["weight_before"] is None else float(row["weight_before"]),
            None if row["weight_after"] is None else float(row["weight_after"]),
        )
        out[key] += 1
    return out


def _diff(old: dict[Any, tuple], new: dict[Any, tuple], columns: tuple[str, ...]) -> dict[str, Any]:
    missing = sorted(set(old) - set(new), key=str)
    extra = sorted(set(new) - set(old), key=str)
    changed = []
    for key in sorted(set(old) & set(new), key=str):
        for index, column in enumerate(columns):
            if old[key][index] != new[key][index]:
                changed.append(
                    {
                        "key": list(key) if isinstance(key, tuple) else key,
                        "column": column,
                        "old": old[key][index],
                        "new": new[key][index],
                    }
                )
    return {
        "n_old": len(old),
        "n_new": len(new),
        "missing": [list(key) if isinstance(key, tuple) else key for key in missing],
        "extra": [list(key) if isinstance(key, tuple) else key for key in extra],
        "changed": changed,
        "identical": not missing and not extra and not changed,
    }


def compare(old_path: Path, new_path: Path) -> dict[str, Any]:
    old = _connect(old_path)
    new = _connect(new_path)
    try:
        old_books, new_books = _books(old), _books(new)
        old_guard, new_guard = _guard(old, old_books), _guard(new, new_books)
        added = new_guard - old_guard
        removed = old_guard - new_guard
        added_kinds = Counter((key[4], key[10]) for key in added.elements())
        removed_kinds = Counter((key[4], key[10]) for key in removed.elements())
        expected = (
            Counter({("L1_REENTRY", "NOT_HELD"): sum(added_kinds.values())}) if added else Counter()
        )
        reentry_kinds = {("L1_REENTRY", "NOT_HELD"), ("L1_REENTRY", "ARM_DISABLED")}
        return {
            "run": old_path.stem,
            "summary": _diff(_summary(old, old_books), _summary(new, new_books), SUMMARY_COLUMNS),
            "tests": _diff(_tests(old), _tests(new), TEST_COLUMNS),
            "performance": _diff(
                _performance(old, old_books),
                _performance(new, new_books),
                PERFORMANCE_COLUMNS,
            ),
            "guard": {
                "n_old": sum(old_guard.values()),
                "n_new": sum(new_guard.values()),
                "added": sum(added.values()),
                "removed": sum(removed.values()),
                "added_kinds": {f"{k[0]}/{k[1]}": v for k, v in sorted(added_kinds.items())},
                "removed_kinds": {f"{k[0]}/{k[1]}": v for k, v in sorted(removed_kinds.items())},
                "only_expected_added": not removed and added_kinds == expected,
                # The shadow GUARD never applies anything, so its re-entry rows carry the arm's
                # fixed ARM_DISABLED status; on the applied GUARD arm the same event is NOT_HELD.
                "only_reentry_rows": not removed and set(added_kinds) <= reentry_kinds,
            },
        }
    finally:
        old.close()
        new.close()


def main(argv: list[str]) -> int:
    if len(argv) < _MIN_ARGUMENTS:
        print(__doc__, file=sys.stderr)
        return 2
    old_dir, new_dir = Path(argv[0]), Path(argv[1])
    names = argv[2:] or sorted(
        path.stem for path in old_dir.glob("*.db") if (new_dir / path.name).is_file()
    )
    failed = False
    for name in names:
        result = compare(old_dir / f"{name}.db", new_dir / f"{name}.db")
        print(json.dumps(result, sort_keys=True))
        failed |= not (
            result["summary"]["identical"]
            and result["tests"]["identical"]
            and result["performance"]["identical"]
            and result["guard"]["only_expected_added"]
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
