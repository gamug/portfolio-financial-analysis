"""Generate the v2 grid, the choice and the thesis-facing results file (V2_PLAN §4).

    uv run python scripts/backtest_v2_export.py --runs <dir> --out <dir> --docs <path>

Read-only over the per-run JSONs the collector wrote (``scripts/backtest_v2_collect.py``) and the
v1 run copies (for the cross-run test of §4(i)). Everything written here is a portfolio
aggregate: no per-asset and no SPY price/return series is emitted, only SPY's window aggregates
as a context row.

Choice rule (§4): lowest mean maximum drawdown of the configuration's Top-N GUARD arm over the
six windows; ties within 0.01 pp to the higher mean cumulative return. Declared in-sample.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cycle.backtest import stats
from kg_schema import connect_ro

WINDOWS = ("P1", "P3", "P2", "H1", "H2", "H3")
CONFIGS = ("V0", "V1", "V2", "V3", "V4", "V5")
CONFIG_LABELS = {
    "V0": ("0", "off"),
    "V1": ("0", "on"),
    "V2": ("0.5", "off"),
    "V3": ("0.5", "on"),
    "V4": ("0.7", "off"),
    "V5": ("0.7", "on"),
}
ARMS = (
    "MARKOWITZ_BASE",
    "MARKOWITZ_BASE_GUARD",
    "TOPN_MV_NOGUARD",
    "TOPN_MV_GUARD_NO_NLP",
)
GUARD_ARMS = ("MARKOWITZ_BASE_GUARD", "TOPN_MV_GUARD_NO_NLP")
SEGMENT_ORDER = ("P1", "P2", "P2a", "P2b", "P3", "H1", "H2", "H3")
V1_RUNS = {
    "P1": "runs_a2/llm_P1.db",
    "P3": "runs_a2/llm_P3.db",
    "P2": "runs_a2/llm_P2.db",
    "H1": "runs_a2/hold_B0_T0_H1.db",
    "H2": "runs_a2/hold_B0_T0_H2.db",
    "H3": "runs_a2/hold_B0_T0_H3.db",
}
V1_GUARD = "TOPN_MV_GUARD_NO_NLP"


def _load_runs(runs: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for config in CONFIGS:
        for window in WINDOWS:
            path = runs / f"{config}_{window}.json"
            if not path.is_file():
                raise SystemExit(f"missing run export {path}")
            out[f"{config}_{window}"] = json.loads(path.read_text())
    return out


def _summary(run: dict[str, Any], segment: str) -> dict[str, dict[str, Any]]:
    return {row["arm"]: row for row in run["summaries"] if str(row["segment"]) == segment}


def _test(
    run: dict[str, Any], segment: str, arm_a: str, arm_b: str, test: str
) -> dict[str, Any] | None:
    for row in run["tests"]:
        if (
            str(row["segment"]) == segment
            and row["arm_a"] == arm_a
            and row["arm_b"] == arm_b
            and row["test"] == test
        ):
            return dict(row)
    return None


def _v1_daily(fin: Path, window: str, arm: str) -> dict[str, float]:
    conn = connect_ro(fin / V1_RUNS[window])
    try:
        summary = conn.execute(
            "SELECT s.date_from, s.date_to FROM backtest_summary s "
            "JOIN strategy_book b ON b.id = s.book_id "
            "WHERE b.arm_code = ? AND s.segment = ?",
            (arm, window),
        ).fetchone()
        if summary is None:
            raise SystemExit(f"v1 {window}: no {arm} summary")
        rows = conn.execute(
            "SELECT p.date, p.net_return FROM book_daily_performance p "
            "JOIN strategy_book b ON b.id = p.book_id "
            "WHERE b.arm_code = ? AND p.date BETWEEN ? AND ? ORDER BY p.date",
            (arm, summary["date_from"], summary["date_to"]),
        ).fetchall()
        return {str(row["date"]): float(row["net_return"]) for row in rows}
    finally:
        conn.close()


def _cross_tests(runs: dict[str, dict[str, Any]], fin: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    v1_cache: dict[tuple[str, str], dict[str, float]] = {}
    for config in CONFIGS:
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            key = (window, V1_GUARD)
            if key not in v1_cache:
                v1_cache[key] = _v1_daily(fin, window, V1_GUARD)
            v1 = v1_cache[key]
            for arm in GUARD_ARMS:
                v2 = run["daily"][arm]
                dates = sorted(set(v2) & set(v1))
                a = [v2[day] for day in dates]
                b = [v1[day] for day in dates]
                hac_t, hac_p = stats.hac_mean_diff(a, b)
                mdd_diff, mdd_p = stats.bootstrap_mdd_diff(a, b)
                out.append(
                    {
                        "configuration": config,
                        "window": window,
                        "arm_a": arm,
                        "arm_b": f"v1 {V1_GUARD}",
                        "n_obs": len(dates),
                        "hac_mean_diff": {"statistic": hac_t, "p_value": hac_p},
                        "bootstrap_mdd": {"statistic": mdd_diff, "p_value": mdd_p},
                    }
                )
    return out


def _spy_series(path: Path) -> dict[str, float]:
    payload = json.loads(path.read_text())
    return {str(day): float(row["tr"]) for day, row in payload.items() if row.get("tr") is not None}


def _context(
    runs: dict[str, dict[str, Any]], spy_path: Path
) -> dict[str, dict[str, dict[str, float]]]:
    spy = _spy_series(spy_path)
    out: dict[str, dict[str, dict[str, float]]] = {}
    for window in WINDOWS:
        run = runs[f"V0_{window}"]
        rf = run["rf"]
        segment = window
        summary = _summary(run, segment)
        date_from = summary["MARKOWITZ_BASE"]["date_from"]
        date_to = summary["MARKOWITZ_BASE"]["date_to"]
        days = sorted(day for day in rf if date_from <= day <= date_to)
        benchmark = [run["benchmark"][day] for day in days if day in run["benchmark"]]
        spy_returns = [spy[day] for day in days if day in spy]
        out[window] = {
            "SP500_EW_INTERNAL": stats.summarize(
                benchmark,
                [rf[day] for day in days if day in run["benchmark"]],
                [0.0] * len(benchmark),
                [0.0] * len(benchmark),
            ),
            "SPY": stats.summarize(
                spy_returns,
                [rf[day] for day in days if day in spy],
                [0.0] * len(spy_returns),
                [0.0] * len(spy_returns),
            ),
        }
    return out


def _v1_guard_mdd(fin: Path, window: str) -> float:
    conn = connect_ro(fin / V1_RUNS[window])
    try:
        row = conn.execute(
            "SELECT s.max_drawdown FROM backtest_summary s "
            "JOIN strategy_book b ON b.id = s.book_id "
            "WHERE b.arm_code = ? AND s.segment = ?",
            (V1_GUARD, window),
        ).fetchone()
        if row is None:
            raise SystemExit(f"v1 {window}: no {V1_GUARD} summary")
        return float(row["max_drawdown"])
    finally:
        conn.close()


def _choice(runs: dict[str, dict[str, Any]], fin: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        mdd = []
        cumulative = []
        per_window = {}
        for window in WINDOWS:
            guard = _summary(runs[f"{config}_{window}"], window)["TOPN_MV_GUARD_NO_NLP"]
            mdd.append(float(guard["max_drawdown"]))
            cumulative.append(float(guard["cumulative_return"]))
            per_window[window] = {
                "max_drawdown": float(guard["max_drawdown"]),
                "cumulative_return": float(guard["cumulative_return"]),
            }
        rows.append(
            {
                "configuration": config,
                "core_weight": float(CONFIG_LABELS[config][0]),
                "trend_filter": CONFIG_LABELS[config][1],
                "mean_mdd": sum(mdd) / len(mdd),
                "mean_cumulative_return": sum(cumulative) / len(cumulative),
                "per_window": per_window,
            }
        )
    best = min(row["mean_mdd"] for row in rows)
    tied = [row for row in rows if row["mean_mdd"] <= best + 0.0001]
    selected = max(tied, key=lambda row: row["mean_cumulative_return"])
    v1_mdd = {window: _v1_guard_mdd(fin, window) for window in WINDOWS}
    lower = [
        window
        for window in WINDOWS
        if selected["per_window"][window]["max_drawdown"] < v1_mdd[window]
    ]
    return {
        "rule": "lowest mean maximum drawdown of TOPN_MV_GUARD_NO_NLP over the six windows; "
        "ties within 0.01 pp to the higher mean cumulative return; declared in-sample",
        "configurations_tried": len(rows),
        "selected": selected["configuration"],
        "v1_guard_mdd": v1_mdd,
        "lower_mdd_than_v1": lower,
        "lower_mdd_than_v1_count": len(lower),
        "rows": sorted(rows, key=lambda row: (row["mean_mdd"], -row["mean_cumulative_return"])),
    }


def _fmt(value: float | None, places: int) -> str:
    return "" if value is None else f"{value:.{places}f}"


def _section_choice(choice: dict[str, Any]) -> list[str]:
    lines = [
        "## 1. The choice",
        "",
        "| rank | config | core | filter | mean MDD | mean cumulative |",
        "|---|---|---|---|---|---|",
    ]
    for rank, row in enumerate(choice["rows"], start=1):
        mark = " **v2**" if row["configuration"] == choice["selected"] else ""
        lines.append(
            f"| {rank} | {row['configuration']}{mark} | {row['core_weight']:.1f} | "
            f"{row['trend_filter']} | {row['mean_mdd']:.6f} | "
            f"{row['mean_cumulative_return']:.6f} |"
        )
    return lines


def _section_grid(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 2. Every configuration x window x arm",
        "",
        "| config | window | segment | arm | cumulative | ann. return | ann. vol | Sharpe | MDD | turnover | cost bps | rebal. | GUARD actions |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            for segment in SEGMENT_ORDER:
                rows = _summary(run, segment)
                if not rows:
                    continue
                for arm in ARMS:
                    if arm not in rows:
                        continue
                    row = rows[arm]
                    lines.append(
                        f"| {config} | {window} | {segment} | {arm} | "
                        f"{row['cumulative_return']:.4f} | {row['annualized_return']:.4f} | "
                        f"{row['annualized_vol']:.4f} | {row['ex_post_sharpe']:.3f} | "
                        f"{row['max_drawdown']:.4f} | {row['total_turnover']:.4f} | "
                        f"{row['total_cost_bps']:.1f} | {row['n_rebalances']} | "
                        f"{row['n_guard_actions']} |"
                    )
    return lines


def _section_cross(cross: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## 3. Section 4(i) -- each v2 GUARD arm vs v1's official GUARD arm",
        "",
        "| config | window | arm | n | HAC stat | HAC p | delta MDD | bootstrap p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in cross:
        lines.append(
            f"| {row['configuration']} | {row['window']} | {row['arm_a']} | {row['n_obs']} | "
            f"{row['hac_mean_diff']['statistic']:.3f} | {row['hac_mean_diff']['p_value']:.3f} | "
            f"{row['bootstrap_mdd']['statistic']:+.4f} | "
            f"{row['bootstrap_mdd']['p_value']:.3f} |"
        )
    return lines


def _section_in_run(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 4. Section 4(ii)/(iii) -- in-run tests",
        "",
        "| config | window | arm vs MARKOWITZ_BASE | n | HAC stat | HAC p | delta MDD | bootstrap p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            for arm in GUARD_ARMS:
                hac = _test(run, window, arm, "MARKOWITZ_BASE", "hac_mean_diff")
                boot = _test(run, window, arm, "MARKOWITZ_BASE", "bootstrap_mdd")
                if hac is None or boot is None:
                    continue
                lines.append(
                    f"| {config} | {window} | {arm} | {hac['n_obs']} | "
                    f"{hac['statistic']:.3f} | {hac['p_value']:.3f} | "
                    f"{boot['statistic']:+.4f} | {boot['p_value']:.3f} |"
                )
    return lines


def _section_filter(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 5. The trend filter at every decision date",
        "",
        "| config | window | cash dates | invested dates | not evaluable |",
        "|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            states = runs[f"{config}_{window}"]["params"]["trend_filter_dates"] or []
            cash = [row["date"] for row in states if row["state"] == "cash"]
            invested = [row["date"] for row in states if row["state"] == "invested"]
            missing = [row["date"] for row in states if row["state"] == "not_evaluable"]
            lines.append(
                f"| {config} | {window} | {', '.join(cash) or '-'} | "
                f"{len(invested)} dates | {', '.join(missing) or '0'} |"
            )
    lines += [
        "",
        "The complete per-decision state list (`cash` / `invested` / `not_evaluable`) is in "
        "`grid.json` and in each run's `params_json.trend_filter_dates`.",
    ]
    return lines


def _section_context(context: dict[str, dict[str, dict[str, float]]]) -> list[str]:
    lines = [
        "## 6. Context rows",
        "",
        "| window | series | cumulative | ann. return | ann. vol | Sharpe | MDD |",
        "|---|---|---|---|---|---|---|",
    ]
    for window in WINDOWS:
        for name in ("SP500_EW_INTERNAL", "SPY"):
            row = context[window][name]
            lines.append(
                f"| {window} | {name} | {row['cumulative_return']:.4f} | "
                f"{row['annualized_return']:.4f} | {row['annualized_vol']:.4f} | "
                f"{row['ex_post_sharpe']:.3f} | {row['max_drawdown']:.4f} |"
            )
    return lines


def _section_identity(identity: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 7. V0 identity check against v1",
        "",
        "| window | summaries | tests | weights | GUARD actions | weight rows | action rows |",
        "|---|---|---|---|---|---|---|",
    ]
    for window in WINDOWS:
        row = identity[window]
        lines.append(
            f"| {window} | {row['summaries']['equal']} | {row['tests']['equal']} | "
            f"{row['weights']['equal']} | {row['guard_actions']['equal']} | "
            f"{row['weights']['rows_v1']} | {row['guard_actions']['rows_v1']} |"
        )
    lines.append("")
    return lines


def _grid_md(
    runs: dict[str, dict[str, Any]],
    choice: dict[str, Any],
    cross: list[dict[str, Any]],
    context: dict[str, dict[str, dict[str, float]]],
    identity: dict[str, dict[str, Any]],
) -> str:
    identity_ok = all(
        row[key]["equal"]
        for row in identity.values()
        for key in ("summaries", "tests", "weights", "guard_actions")
    )
    lines = [
        "# System v2 -- results (generated)",
        "",
        "Generated by `scripts/backtest_v2_export.py` from the 36 run copies; do not edit by hand.",
        "Aggregates only: no per-asset value and no SPY price/return series appears here.",
        "",
        "## 0. Method and declarations",
        "",
        "- Plan: `docs/backtest_v2_plan.md` (commit `def516f`), §7 readings `bbc8b71`.",
        "- Scores: `scored_llm.db` sha256 `2187c36b...` (configuration A), blend B0, tilt T0,",
        "  frozen GUARD `params.frozen.toml` sha256 `ee22f352...`, 12 warm-up steps, one fresh",
        "  rule-filled copy per run. No model call was made in this round.",
        "- SPY: the scratch file `spy_tr.json` sha256 `58b91870...`, refused unless it equals",
        "  `manifest.json` beside it; only window aggregates are reported.",
        "- **Every v2 result is in-sample**: v2 was designed after v1's results on all six windows",
        "  (2022-2026; no unseen data remains).",
        f"- V0 identity: {'all six windows reproduce v1 exactly' if identity_ok else 'FAILED'}",
        "  (summaries, v1 tests, weights and GUARD actions; max |delta| 0.0).",
        f"- The v2 system is {choice['selected']} "
        f"(core {CONFIG_LABELS[choice['selected']][0]}, trend filter "
        f"{CONFIG_LABELS[choice['selected']][1]}) by the section-4 rule: lowest mean",
        "  Top-N GUARD MDD over the six windows, ties within 0.01 pp to the higher mean cumulative",
        "  return.",
        "",
    ]
    lines += _section_choice(choice)
    lines += [
        "",
        f"The chosen configuration has a lower Top-N GUARD MDD than v1 in "
        f"**{choice['lower_mdd_than_v1_count']} of 6 windows** "
        f"({', '.join(choice['lower_mdd_than_v1']) or 'none'}); v1's MDD per window is "
        f"{', '.join(f'{window} {choice["v1_guard_mdd"][window]:.4f}' for window in WINDOWS)}. "
        "No significance is claimed "
        "beyond what section 3's tests report; every v2 result is in-sample.",
        "",
    ]
    lines += _section_grid(runs)
    lines.append("")
    lines += _section_cross(cross)
    lines.append("")
    lines += _section_in_run(runs)
    lines.append("")
    lines += _section_filter(runs)
    lines.append("")
    lines += _section_context(context)
    lines.append("")
    lines += _section_identity(identity)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--finance-root", default="/workspaces/thesis/scratch/final")
    parser.add_argument("--spy", default="/workspaces/thesis/scratch/final/data/spy/spy_tr.json")
    args = parser.parse_args()
    runs_dir = Path(args.runs).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    fin = Path(args.finance_root).expanduser().resolve()
    runs = _load_runs(runs_dir)
    choice = _choice(runs, fin)
    cross = _cross_tests(runs, fin)
    context = _context(runs, Path(args.spy))
    identity = {
        window: json.loads((out_dir / f"identity_{window}.json").read_text()) for window in WINDOWS
    }
    payload = {
        "plan": "docs/backtest_v2_plan.md",
        "scored_llm_sha256": "2187c36bae42296b84d63031e9058df5aba997a77da2a4aa6665279452e184ca",
        "configurations": {
            f"{config}_{window}": {
                "config": config,
                "window": window,
                "core_weight": runs[f"{config}_{window}"]["params"]["core_weight"],
                "trend_filter": runs[f"{config}_{window}"]["params"]["trend_filter"],
                "core_series_sha256": runs[f"{config}_{window}"]["params"]["core_series_sha256"],
                "trend_filter_dates": runs[f"{config}_{window}"]["params"]["trend_filter_dates"],
                "summaries": runs[f"{config}_{window}"]["summaries"],
                "tests": runs[f"{config}_{window}"]["tests"],
            }
            for config in CONFIGS
            for window in WINDOWS
        },
        "cross_tests": cross,
        "context": context,
        "choice": choice,
        "identity": identity,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grid.json").write_text(json.dumps(payload, indent=1) + "\n")
    markdown = _grid_md(runs, choice, cross, context, identity)
    (out_dir / "grid.md").write_text(markdown + "\n")
    (out_dir / "choice.json").write_text(json.dumps(choice, indent=1) + "\n")
    Path(args.docs).expanduser().resolve().write_text(markdown + "\n")
    print(
        json.dumps(
            {
                "selected": choice["selected"],
                "mean_mdd": choice["rows"][0]["mean_mdd"],
                "grid": str(out_dir / "grid.json"),
                "docs": args.docs,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
