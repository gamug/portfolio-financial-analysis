"""Export a set of backtest run copies to the thesis-facing results file.

``python -m cycle.backtest export --db run1.db --db run2.db \
    --out-md docs/backtest_results.md --out-json docs/backtest_results.json``

Read-only. It reads only ``backtest_run``, ``strategy_book``, ``backtest_summary``,
``backtest_test``, the run's ``params_json``, the portfolio-level columns of
``book_daily_performance`` (``benchmark_return`` for the benchmark, and the dates of a segment)
and the run copy's ``risk_free_rate`` rows for the run's curve (A-1.2's amendment of
FINISH_REPO §9.1: the same rows the run used, so the benchmark's Sharpe subtracts the same
daily rate as the arms). No per-asset row is read or written: every number is a portfolio
aggregate.

Per run, segment and arm it writes the ``backtest_summary`` columns; per test the statistic, the
p-value, ``n_obs`` and the test's parameters; per run the formation date, the warm-up refusals,
the input sha256, the params sha256 and the code version. The ``SP500_EW_INTERNAL`` benchmark is
summarized with :func:`cycle.backtest.stats.summarize` over the same segment dates.

The benchmark's ex-post Sharpe subtracts the run's own daily ``risk_free_rate`` (curve from the
run's ``params_json``, annualized rate / 252, carried forward) -- the same rows and rule
``driver.load_rf`` used for the arms' ``backtest_summary``.
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cycle.backtest import stats
from kg_schema import connect_ro

#: The order the markdown groups the runs in (FINISH_REPO section 9.1).
GROUP_ORDER = ("main", "robustness", "w6", "excl", "other")
#: The order the segments are listed in.
SEGMENT_ORDER = ("P1", "P3", "P2", "P2a", "P2b")
#: The benchmark's name (engine.BENCHMARK; not imported to keep this module DB-only).
BENCHMARK = "SP500_EW_INTERNAL"
#: The summary columns the md shows, with their header labels and decimal places.
SUMMARY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("cumulative_return", "Cumulative", 4),
    ("annualized_return", "Ann. return", 4),
    ("annualized_vol", "Ann. vol", 4),
    ("ex_post_sharpe", "Sharpe", 3),
    ("max_drawdown", "MDD", 4),
    ("total_turnover", "Turnover", 4),
    ("total_cost_bps", "Cost bps", 1),
    ("n_rebalances", "Rebal.", 0),
    ("n_guard_actions", "GUARD actions", 0),
)


class ExportError(RuntimeError):
    """A database the export cannot describe."""


@dataclass(frozen=True)
class RunSection:
    """One run copy's aggregates."""

    name: str
    group: str
    window: str
    backtest_run_id: int
    status: str
    formation_date: str | None
    warmup_refused: list[dict[str, str]]
    decisions_refused: list[dict[str, str]]
    input_db_sha256: str | None
    params_sha256: str | None
    code_version: str | None
    summaries: dict[str, dict[str, dict[str, float | None]]]  # segment -> arm -> columns
    dates: dict[str, tuple[str, str]]  # segment -> (from, to)
    benchmark: dict[str, dict[str, float | None]]  # segment -> summarize()
    tests: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "window": self.window,
            "backtest_run_id": self.backtest_run_id,
            "status": self.status,
            "formation_date": self.formation_date,
            "warmup_refused": self.warmup_refused,
            "decisions_refused": self.decisions_refused,
            "input_db_sha256": self.input_db_sha256,
            "params_sha256": self.params_sha256,
            "code_version": self.code_version,
            "segments": {
                segment: {
                    "date_from": self.dates[segment][0],
                    "date_to": self.dates[segment][1],
                    "arms": arms,
                    "benchmark": self.benchmark.get(segment),
                }
                for segment, arms in self.summaries.items()
            },
            "tests": self.tests,
        }


def run_group(name: str) -> str:
    """The markdown group a run copy belongs to, from its file stem."""
    lowered = name.lower()
    for prefix, group in (
        ("rule_", "rule"),
        ("llm_", "llm"),
        ("w6_", "w6"),
        ("excl_", "excl"),
    ):
        if lowered.startswith(prefix):
            return group
    return "other"


def _summary_row(row: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for column in (
        "cumulative_return",
        "annualized_return",
        "annualized_vol",
        "ex_post_sharpe",
        "max_drawdown",
        "total_turnover",
        "total_cost_bps",
        "n_rebalances",
        "n_guard_actions",
    ):
        value = row[column]
        out[column] = None if value is None else float(value)
    return out


def _risk_free_series(conn: Any, curve: str) -> tuple[list[str], list[float]]:
    """The run's ``risk_free_rate`` rows for *curve*, as (dates, daily fractions).

    This is ``driver.load_rf``'s rule: every row of the curve, the annualized rate / 252, and
    a session with no row of its own carries the last observation.
    """
    rows = conn.execute(
        "SELECT rate_date, annualized_rate FROM risk_free_rate WHERE curve = ? ORDER BY rate_date",
        (curve,),
    ).fetchall()
    if not rows:
        raise ExportError(f"risk_free_rate has no {curve} rows")
    return (
        [str(r["rate_date"]) for r in rows],
        [float(r["annualized_rate"]) / 252.0 for r in rows],
    )


def _benchmark_summary(
    conn: Any,
    book_id: int,
    date_from: str,
    date_to: str,
    rf: tuple[Sequence[str], Sequence[float]],
) -> dict[str, float | None]:
    """``stats.summarize`` over the benchmark column of one book's segment, with the run's RF."""
    rf_dates, rf_daily = rf
    rows = conn.execute(
        "SELECT date, benchmark_return FROM book_daily_performance "
        "WHERE book_id = ? AND date >= ? AND date <= ? ORDER BY date",
        (book_id, date_from, date_to),
    ).fetchall()
    values = [float(r["benchmark_return"]) for r in rows if r["benchmark_return"] is not None]
    if len(values) != len(rows) or not values:
        raise ExportError(
            f"book {book_id}: the benchmark column is missing between {date_from} and {date_to}"
        )
    rf_values: list[float] = []
    for row in rows:
        index = bisect_right(rf_dates, str(row["date"])) - 1
        if index < 0:
            raise ExportError(
                f"risk_free_rate has no observation on or before {row['date']} (book {book_id})"
            )
        rf_values.append(float(rf_daily[index]))
    zeros = [0.0] * len(values)
    summary: dict[str, float | None] = dict(stats.summarize(values, rf_values, zeros, zeros))
    return summary


def read_run(db_path: str | Path, *, name: str | None = None) -> RunSection:
    """One run copy's aggregates; the latest completed ``backtest_run`` row of the database."""
    path = Path(db_path)
    if not path.is_file():
        raise ExportError(f"database not found: {path}")
    conn = connect_ro(path)
    try:
        run = conn.execute("SELECT * FROM backtest_run ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            raise ExportError(f"{path}: no backtest_run row")
        if str(run["status"]) != "completed":
            raise ExportError(f"{path}: the latest run is {run['status']}, not completed")
        params = json.loads(str(run["params_json"]))
        rf_dates, rf_daily = _risk_free_series(conn, str(params.get("rf_curve") or "FF_RF_DAILY"))
        books = {
            str(row["arm_code"]): int(row["id"])
            for row in conn.execute(
                "SELECT id, arm_code FROM strategy_book WHERE backtest_run_id = ?",
                (int(run["id"]),),
            ).fetchall()
        }
        if not books:
            raise ExportError(f"{path}: run {run['id']} has no strategy_book row")
        summaries: dict[str, dict[str, dict[str, float | None]]] = {}
        dates: dict[str, tuple[str, str]] = {}
        benchmark: dict[str, dict[str, float | None]] = {}
        for book_id in books.values():
            for row in conn.execute(
                "SELECT s.*, b.arm_code AS arm_code FROM backtest_summary s "
                "JOIN strategy_book b ON b.id = s.book_id WHERE s.book_id = ?",
                (book_id,),
            ).fetchall():
                segment = str(row["segment"])
                summaries.setdefault(segment, {})[str(row["arm_code"])] = _summary_row(row)
                dates.setdefault(segment, (str(row["date_from"]), str(row["date_to"])))
        first_book = next(iter(books.values()))
        for segment, (date_from, date_to) in dates.items():
            # The benchmark is one series: take it from any book (the driver writes it on each).
            benchmark[segment] = _benchmark_summary(
                conn, first_book, date_from, date_to, (rf_dates, rf_daily)
            )
        ordered = {s: summaries[s] for s in SEGMENT_ORDER if s in summaries}
        for segment, arms in summaries.items():
            ordered.setdefault(segment, arms)
        tests = [
            {
                "segment": str(row["segment"]),
                "arm_a": str(row["arm_a"]),
                "arm_b": str(row["arm_b"]),
                "test": str(row["test"]),
                "statistic": float(row["statistic"]),
                "p_value": float(row["p_value"]),
                "n_obs": int(row["n_obs"]),
                "params": json.loads(str(row["params_json"] or "{}")),
            }
            for row in conn.execute(
                "SELECT * FROM backtest_test WHERE backtest_run_id = ? "
                "ORDER BY segment, arm_a, arm_b, test",
                (int(run["id"]),),
            ).fetchall()
        ]
        return RunSection(
            name=name or path.stem,
            group=run_group(name or path.stem),
            window=str(run["window_id"]),
            backtest_run_id=int(run["id"]),
            status=str(run["status"]),
            formation_date=None if run["formation_date"] is None else str(run["formation_date"]),
            warmup_refused=list(params.get("warmup_refused") or []),
            decisions_refused=list(params.get("decisions_refused") or []),
            input_db_sha256=run["input_db_sha256"],
            params_sha256=params.get("params_sha256"),
            code_version=run["code_version"],
            summaries=ordered,
            dates={s: dates[s] for s in ordered},
            benchmark=benchmark,
            tests=tests,
        )
    finally:
        conn.close()


def _grouped(runs: Sequence[RunSection]) -> list[tuple[str, list[RunSection]]]:
    """Runs by group, the main run first, then robustness, W = 6, ``excl_*`` (section 9.1)."""
    by_group: dict[str, list[RunSection]] = {}
    for run in runs:
        by_group.setdefault(run.group, []).append(run)
    main_kind = "llm" if "llm" in by_group else "rule"
    order = [main_kind, "rule" if main_kind == "llm" else "llm", "w6", "excl", "other"]
    out: list[tuple[str, list[RunSection]]] = []
    labels = {
        main_kind: "Main run",
        "w6": "Warm-up W = 6 (T5)",
        "excl": "L-43 exclusions",
        "other": "Other runs",
    }
    labels["rule" if main_kind == "llm" else "llm"] = "Robustness"
    seen: set[str] = set()
    for group in [*order, *GROUP_ORDER]:
        if group in seen or group not in by_group:
            continue
        seen.add(group)
        runs_in_group = sorted(by_group[group], key=lambda r: (r.window, r.name))
        out.append((labels.get(group, group), runs_in_group))
    return out


def _fmt(value: float | None, places: int) -> str:
    if value is None:
        return "—"
    if places == 0:
        return str(round(value))
    return f"{value:.{places}f}"


def render_markdown(runs: Sequence[RunSection]) -> str:  # noqa: C901 - one document, one layout
    """The thesis-facing tables; aggregates only, never a per-asset row."""
    lines = [
        "# Backtest results",
        "",
        "Generated by `python -m cycle.backtest export` from the run copies; do not edit by hand.",
        "Every number is a portfolio aggregate: no file here carries a single asset's price or",
        "return. Method and limitations: `docs/backtest.md`.",
        "",
        "Segments: P1 / P3 / P2 are the windows; P2a (to 2025-05-06) and P2b (after) are reporting",
        "cuts of the one continuous P2 run. The benchmark rows are `SP500_EW_INTERNAL` (an equal-",
        "weight member portfolio, context only).",
        "",
        "**Risk-free rate in the Sharpe column.** Every row, the benchmark included, subtracts the",
        "run's daily `FF_RF_DAILY` rate: the arms as `backtest_summary` stores them, the benchmark",
        "from the run copy's own `risk_free_rate` rows (same curve, same carry-forward rule).",
        "",
    ]
    for label, group_runs in _grouped(runs):
        lines += [f"## {label}", ""]
        for run in group_runs:
            lines += [
                f"### {run.name} — window {run.window}, run {run.backtest_run_id}",
                "",
                f"- formation date: {run.formation_date or '—'}",
                f"- input database sha256: `{run.input_db_sha256}`",
                f"- params sha256: `{run.params_sha256}`",
                f"- code version: `{run.code_version}`",
                f"- warm-up refusals: {len(run.warmup_refused)}",
                f"- pre-formation decision refusals: {len(run.decisions_refused)}",
                "",
            ]
            for refusal in run.warmup_refused:
                lines.append(f"  - warm-up {refusal.get('decision_date')}: {refusal.get('reason')}")
            for refusal in run.decisions_refused:
                lines.append(
                    f"  - decision {refusal.get('decision_date')}: {refusal.get('reason')}"
                )
            if run.warmup_refused or run.decisions_refused:
                lines.append("")
            header = (
                "| Segment | Arm | Dates | "
                + " | ".join(column[1] for column in SUMMARY_COLUMNS)
                + " |"
            )
            rule = "|" + "---|" * (3 + len(SUMMARY_COLUMNS))
            lines += [header, rule]
            for segment, arms in run.summaries.items():
                date_from, date_to = run.dates[segment]
                for arm_code in sorted(arms):
                    row = arms[arm_code]
                    cells = " | ".join(
                        _fmt(row.get(column[0]), column[2]) for column in SUMMARY_COLUMNS
                    )
                    lines.append(f"| {segment} | {arm_code} | {date_from}..{date_to} | {cells} |")
                bench = run.benchmark.get(segment)
                if bench is not None:
                    cells = " | ".join(
                        _fmt(bench.get(column[0]), column[2]) for column in SUMMARY_COLUMNS[:5]
                    )
                    lines.append(
                        f"| {segment} | {BENCHMARK} | {date_from}..{date_to} | {cells} | "
                        "— | — | — | — |"
                    )
            lines.append("")
            if run.tests:
                lines += [
                    "| Segment | A | B | Test | Statistic | p | n |",
                    "|---|---|---|---|---|---|---|",
                ]
                for test in run.tests:
                    lines.append(
                        f"| {test['segment']} | {test['arm_a']} | {test['arm_b']} | "
                        f"{test['test']} | {test['statistic']:.4f} | {test['p_value']:.4f} | "
                        f"{test['n_obs']} |"
                    )
                lines.append("")
    return "\n".join(lines) + "\n"


def build(databases: Sequence[str | Path]) -> tuple[dict[str, Any], str]:
    """The export's JSON object and its markdown, from the given run copies."""
    runs = [read_run(path) for path in databases]
    payload: dict[str, Any] = {
        "generated_by": "python -m cycle.backtest export",
        "benchmark": BENCHMARK,
        "benchmark_sharpe_note": (
            "SP500_EW_INTERNAL's ex-post Sharpe subtracts the run's daily FF_RF_DAILY rate, the "
            "same rows and carry-forward rule as the strategy arms"
        ),
        "runs": [run.as_dict() for run in runs],
    }
    return payload, render_markdown(runs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cycle.backtest export",
        description="Export backtest run copies to the results markdown and JSON.",
    )
    parser.add_argument(
        "--db", action="append", required=True, help="a run copy (repeat for several runs)"
    )
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--out-json", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload, markdown = build(args.db)
    except ExportError as exc:
        print(f"cycle.backtest export: {exc}", file=sys.stderr)
        return 2
    Path(args.out_json).write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    Path(args.out_md).write_text(markdown)
    print(
        json.dumps(
            {
                "runs": [run["name"] for run in payload["runs"]],
                "out_md": args.out_md,
                "out_json": args.out_json,
            },
            sort_keys=True,
        )
    )
    return 0
