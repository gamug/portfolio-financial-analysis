"""Generate docs/backtest_selection_summary.md (plan §4) from the run JSONs.

    uv run python scripts/backtest_selection_summary.py --out <final-round out> \
        --docs docs/backtest_selection_summary.md

Read-only over the per-run JSONs. Aggregates only: no per-asset value and no SPY price.
The script first reproduces reviewer A's §10 figures from the round-4 series, raising if a
point estimate misses the shown value by more than 0.05, and only then adds this round's
configurations.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from cycle.backtest import selection as sel

FINAL = Path("/workspaces/thesis/scratch/final")
OUT = FINAL / "out"
WINDOWS = sel.WINDOWS
GUARD = "TOPN_MV_GUARD_NO_NLP"
_TOL = 0.05
_ROUND24_TRIALS = 21
_DECLARED_TRIALS = 55
NOGUARD = "TOPN_MV_NOGUARD"
SPY_PATH = FINAL / "data/spy/spy_tr.json"

# Reviewer A's §10 point estimates (plan table) and test table.
REVIEWER = {
    "Y2": dict(
        cagr=7.20,
        vol=8.83,
        sharpe=0.38,
        sortino=0.53,
        calmar=0.41,
        mdd=17.55,
        days=630,
        cvar=1.29,
        beta=0.44,
        alpha=-0.59,
        t=-0.28,
        up=50,
        down=49,
    ),
    "Y2 NOGUARD": dict(
        cagr=7.96,
        vol=10.18,
        sharpe=0.41,
        sortino=0.57,
        calmar=0.44,
        mdd=18.00,
        days=629,
        cvar=1.49,
        beta=0.52,
        alpha=-0.46,
        t=-0.20,
        up=57,
        down=56,
    ),
    "X5": dict(
        cagr=7.06,
        vol=9.11,
        sharpe=0.36,
        sortino=0.49,
        calmar=0.41,
        mdd=17.37,
        days=631,
        cvar=1.33,
        beta=0.44,
        alpha=-0.67,
        t=-0.29,
        up=51,
        down=50,
    ),
    "V3": dict(
        cagr=5.73,
        vol=8.82,
        sharpe=0.23,
        sortino=0.31,
        calmar=0.33,
        mdd=17.28,
        days=655,
        cvar=1.38,
        beta=0.31,
        alpha=-0.79,
        t=-0.25,
        up=43,
        down=43,
    ),
    "v1 (V0) GUARD": dict(
        cagr=5.99,
        vol=11.78,
        sharpe=0.22,
        sortino=0.30,
        calmar=0.24,
        mdd=25.10,
        days=659,
        cvar=1.69,
        beta=0.48,
        alpha=-1.77,
        t=-0.46,
        up=47,
        down=46,
    ),
    "MARKOWITZ_BASE": dict(
        cagr=8.56,
        vol=10.98,
        sharpe=0.44,
        sortino=0.62,
        calmar=0.52,
        mdd=16.46,
        days=561,
        cvar=1.54,
        beta=0.36,
        alpha=1.63,
        t=0.39,
        up=34,
        down=29,
    ),
    "SP500_EW_INTERNAL": dict(
        cagr=9.43,
        vol=16.30,
        sharpe=0.39,
        sortino=0.56,
        calmar=0.43,
        mdd=22.10,
        days=488,
        cvar=2.26,
        beta=0.86,
        alpha=-1.36,
        t=-0.43,
        up=84,
        down=85,
    ),
    "SPY": dict(
        cagr=12.21,
        vol=17.27,
        sharpe=0.52,
        sortino=0.75,
        calmar=0.50,
        mdd=24.50,
        days=488,
        cvar=2.47,
        beta=1.00,
        alpha=0.00,
        t=0.00,
        up=100,
        down=100,
    ),
}
REVIEWER_TESTS = {
    "Y2 NOGUARD": dict(mean=-0.83, hac=0.300, jk=0.671, dmdd=-0.45, boot=0.816, detectable=2.3),
    "X5": dict(mean=0.10, hac=0.839, jk=0.731, dmdd=0.17, boot=0.912, detectable=1.4),
    "V3": dict(mean=1.38, hac=0.517, jk=0.523, dmdd=0.27, boot=0.934, detectable=5.9),
    "v1 (V0) GUARD": dict(mean=0.82, hac=0.794, jk=0.545, dmdd=-7.55, boot=0.110, detectable=8.8),
    "MARKOWITZ_BASE": dict(mean=-1.48, hac=0.682, jk=0.876, dmdd=1.09, boot=0.871, detectable=10.1),
    "SPY": dict(mean=-5.67, hac=0.202, jk=0.563, dmdd=-6.95, boot=0.204, detectable=12.4),
}
REVIEWER_OVERFIT = dict(
    pbo=0.893,
    is_sharpe=0.66,
    oos_sharpe=0.02,
    emax21=1.922,
    luck21=0.88,
    btl21=3.69,
    emax55=2.313,
    luck55=1.06,
    btl55=5.35,
    dsr21=0.679,
    dsr55=0.651,
    rc_mkt_mean=0.38,
    rc_mkt_p=0.772,
    rc_mkt_p_iid=0.752,
    rc_spy_mean=-3.81,
    rc_spy_p=0.947,
    rc_spy_p_iid=0.924,
    wf_ranks=(1, 1, 21, 13),
)


def _run(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def _load_chain(root: Path, config: str, arm: str) -> tuple[list[str], np.ndarray, np.ndarray]:
    runs = [_run(root / f"{config}_{window}.json") for window in WINDOWS]
    return sel.chain(runs, arm)


def _series(
    paths: dict[str, tuple[Path, str, str]],
) -> dict[str, tuple[list[str], np.ndarray, np.ndarray]]:
    out: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
    for name, (root, config, arm) in paths.items():
        out[name] = _load_chain(root, config, arm)
    return out


def _spy(dates: list[str]) -> np.ndarray:
    raw = json.loads(SPY_PATH.read_text())
    return np.asarray(
        [float(raw[day]["tr"]) if raw.get(day, {}).get("tr") is not None else 0.0 for day in dates],
        dtype=np.float64,
    )


def _benchmark(dates: list[str]) -> np.ndarray:
    values: dict[str, float] = {}
    for window in WINDOWS:
        values.update(_run(OUT / "v4x/runs" / f"Y2_{window}.json")["benchmark"])
    return np.asarray([float(values[day]) for day in dates], dtype=np.float64)


def _format(value: float, *, percent: bool = False, digits: int = 2) -> str:
    if percent:
        return f"{value * 100:.{digits}f}%"
    return f"{value:.{digits}f}"


def _reproduce(series: dict[str, tuple[list[str], np.ndarray, np.ndarray]]) -> list[str]:
    dates, _, _ = series["Y2"]
    spy = _spy(dates)
    ew = _benchmark(dates)
    lines = [
        "### (f1) Reviewer A's §10 point estimates, recomputed",
        "",
        "| series | CAGR | Vol | Sharpe | Sortino | Calmar | MDD | days UW | CVaR95 | Beta | Alpha/yr (t OLS / t NW) | up/down (geo) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    failures: list[str] = []
    for name, expected in REVIEWER.items():
        _, returns, own_rf = series[name]
        metrics = sel.performance(returns, own_rf, spy, ew)
        alpha_t = f"{metrics['alpha'] * 100:.2f}% ({metrics['alpha_t_ols']:.2f} / {metrics['alpha_t_nw']:.2f})"
        lines.append(
            f"| {name} | {metrics['cagr'] * 100:.2f}% | {metrics['vol'] * 100:.2f}% | "
            f"{metrics['sharpe']:.2f} | {metrics['sortino']:.2f} | {metrics['calmar']:.2f} | "
            f"{metrics['mdd'] * 100:.2f}% | {metrics['longest_under_water']} | "
            f"{metrics['cvar95'] * 100:.2f}% | {metrics['beta']:.2f} | {alpha_t} | "
            f"{metrics['up_capture_geo'] * 100:.0f}% / {metrics['down_capture_geo'] * 100:.0f}% "
            f"(arith {metrics['up_capture_arith'] * 100:.0f}% / {metrics['down_capture_arith'] * 100:.0f}%) |"
        )
        checks = {
            "cagr": (metrics["cagr"] * 100, expected["cagr"]),
            "vol": (metrics["vol"] * 100, expected["vol"]),
            "sharpe": (metrics["sharpe"], expected["sharpe"]),
            "sortino": (metrics["sortino"], expected["sortino"]),
            "calmar": (metrics["calmar"], expected["calmar"]),
            "mdd": (metrics["mdd"] * 100, expected["mdd"]),
            "days": (float(metrics["longest_under_water"]), float(expected["days"])),
            "cvar": (metrics["cvar95"] * 100, expected["cvar"]),
            "beta": (metrics["beta"], expected["beta"]),
            "alpha": (metrics["alpha"] * 100, expected["alpha"]),
            "up": (round(metrics["up_capture_geo"] * 100), expected["up"]),
            "down": (round(metrics["down_capture_geo"] * 100), expected["down"]),
        }
        for key, (got, want) in checks.items():
            tolerance = 0.05 if key not in ("up", "down", "days") else 0.5
            if abs(got - want) > tolerance:
                failures.append(f"{name}.{key}: got {got:.4f}, reviewer {want}")
    lines += [
        "",
        "Capture is the geometric (compound) mean on SPY-up / SPY-down sessions; the arithmetic",
        "mean is shown in parentheses. The reviewer's figures reproduce with the geometric mean.",
        "The alpha t is shown both plain OLS (the reviewer's §10 convention) and Newey-West (this",
        "report's convention).",
        "",
    ]
    if failures:
        raise SystemExit("§10 reproduction failed: " + "; ".join(failures))
    return lines


def _tests_reproduction(series: dict[str, tuple[list[str], np.ndarray, np.ndarray]]) -> list[str]:
    _, y2, rf = series["Y2"]
    lines = [
        "### (f2) Reviewer A's §10 tests, recomputed (Y2 against each series)",
        "",
        "| Y2 vs | mean diff (pp/yr) | HAC p | JK-Memmel p | ΔMDD (pp) | bootstrap p | min. detectable (pp/yr) |",
        "|---|---|---|---|---|---|---|",
    ]
    failures: list[str] = []
    for name, expected in REVIEWER_TESTS.items():
        _, other, _ = series[name]
        table = sel.test_table(y2, other, rf)
        lines.append(
            f"| {name} | {table['mean_diff_annual'] * 100:+.2f} | {table['hac_p']:.3f} | "
            f"{table['jk_p']:.3f} | {table['mdd_diff'] * 100:+.2f} | {table['bootstrap_p']:.3f} | "
            f"±{table['min_detectable_annual'] * 100:.1f} |"
        )
        for key, got, want in (
            ("mean", table["mean_diff_annual"] * 100, expected["mean"]),
            ("dmdd", table["mdd_diff"] * 100, expected["dmdd"]),
            ("detectable", table["min_detectable_annual"] * 100, expected["detectable"]),
        ):
            if abs(got - want) > _TOL:
                failures.append(f"{name}.{key}: got {got:.3f}, reviewer {want}")
        for key, got, want in (
            ("hac", table["hac_p"], expected["hac"]),
            ("jk", table["jk_p"], expected["jk"]),
            ("boot", table["bootstrap_p"], expected["boot"]),
        ):
            if abs(got - want) > _TOL:
                failures.append(f"{name}.{key}: got {got:.3f}, reviewer {want}")
    lines.append("")
    if failures:
        raise SystemExit("§10 test reproduction failed: " + "; ".join(failures))
    return lines


def _trial_matrix(
    series: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
    extra: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
) -> tuple[list[str], np.ndarray, np.ndarray]:
    dates, _, rf = series["Y2"]
    names = [name for name in extra]
    columns = []
    for name in names:
        other_dates, returns, _ = extra[name] if name in extra else series[name]
        if other_dates != dates:
            raise SystemExit(f"trial {name} is not aligned with the chained calendar")
        columns.append(returns - rf)
    return names, rf, np.column_stack(columns)


def _overfitting(
    series: dict[str, tuple[list[str], np.ndarray, np.ndarray]],
    trial_names: list[str],
    trial_matrix: np.ndarray,
    rf: np.ndarray,
    final_name: str,
) -> tuple[list[str], dict[str, Any]]:
    dates, _, _ = series["Y2"]
    pbo = sel.cscv_pbo(trial_matrix)
    pbo_calmar = sel.cscv_pbo(trial_matrix, metric="calmar")
    sr_std = math.sqrt(sel._sr_variance(trial_matrix))
    emax = {n: sel.expected_max_sharpe(n, 1.0) for n in (len(trial_names), 55)}
    luck = {n: value / math.sqrt(len(dates) / 252) for n, value in emax.items()}
    btl = {n: value**2 for n, value in emax.items()}
    final_returns = series[final_name][1]
    final_rf = series[final_name][2]
    dsr = {
        n: sel.deflated_sharpe(final_returns - final_rf, n_trials=n, sr_std=sr_std)
        for n in (len(trial_names), 55)
    }
    spy = _spy(dates)
    _, markowitz, _ = series["MARKOWITZ_BASE"]
    rc_mkt = sel.reality_check(trial_matrix - (markowitz - rf)[:, None], q=0.1)
    rc_mkt_iid = sel.reality_check(trial_matrix - (markowitz - rf)[:, None], q=1.0)
    rc_spy = sel.reality_check(trial_matrix - (spy - rf)[:, None], q=0.1)
    rc_spy_iid = sel.reality_check(trial_matrix - (spy - rf)[:, None], q=1.0)
    lines = [
        "### (e) Overfitting diagnostics",
        "",
        f"The trial set T has **{len(trial_names)}** configurations: {', '.join(trial_names)}.",
        "All metrics use the six windows' chained daily net returns; Sharpe is of excess returns.",
        "",
        "| diagnostic | value |",
        "|---|---|",
        f"| PBO (CSCV, S = 16, Sharpe) | {pbo['pbo']:.3f} |",
        f"| mean annualised Sharpe of the in-sample best | {pbo['mean_is_sharpe']:.2f} in-sample / {pbo['mean_oos_sharpe']:.2f} out-of-sample |",
        f"| PBO (secondary, Calmar) | {pbo_calmar['pbo']:.3f} |",
        f"| E[max] N = {len(trial_names)} / 55 | {emax[len(trial_names)]:.3f} / {emax[55]:.3f} |",
        f"| implied annualised luck threshold | {luck[len(trial_names)]:.2f} / {luck[55]:.2f} |",
        f"| MinBTL for annualised SR 1 | {btl[len(trial_names)]:.2f} / {btl[55]:.2f} years |",
        f"| DSR of {final_name} (N = {len(trial_names)}) | SRhat {dsr[len(trial_names)]['sr_daily']:.5f} daily, "
        f"skew {dsr[len(trial_names)]['skew']:.3f}, kurtosis {dsr[len(trial_names)]['kurtosis']:.3f}, "
        f"SR0 {dsr[len(trial_names)]['sr_zero_daily']:.5f}, DSR {dsr[len(trial_names)]['dsr']:.3f} |",
        f"| DSR of {final_name} (N = 55) | SR0 {dsr[55]['sr_zero_daily']:.5f}, DSR {dsr[55]['dsr']:.3f} |",
        f"| White Reality Check vs MARKOWITZ_BASE | best {trial_names[int(rc_mkt['best_model'])]}, "
        f"mean {rc_mkt['best_mean_annual'] * 100:+.2f} pp/yr, p {rc_mkt['p_value']:.3f} "
        f"(q = 1: {rc_mkt_iid['p_value']:.3f}) |",
        f"| White Reality Check vs SPY | best {trial_names[int(rc_spy['best_model'])]}, "
        f"mean {rc_spy['best_mean_annual'] * 100:+.2f} pp/yr, p {rc_spy['p_value']:.3f} "
        f"(q = 1: {rc_spy_iid['p_value']:.3f}) |",
        "",
    ]
    diagnostics = dict(
        pbo=pbo,
        pbo_calmar=pbo_calmar,
        sr_std=sr_std,
        emax=emax,
        luck=luck,
        btl=btl,
        dsr=dsr,
        rc_mkt=rc_mkt,
        rc_mkt_iid=rc_mkt_iid,
        rc_spy=rc_spy,
        rc_spy_iid=rc_spy_iid,
        n_trials=len(trial_names),
        trial_names=trial_names,
    )
    return lines, diagnostics


def _window_lengths() -> list[int]:
    lengths = []
    for window in WINDOWS:
        run = _run(OUT / "v4x/runs" / f"Y2_{window}.json")
        lengths.append(len(run["daily"][GUARD]))
    return lengths


def _walk_forward(
    series: dict[str, tuple[list[str], np.ndarray, np.ndarray]], paths: dict[str, tuple[Path, str]]
) -> list[dict[str, Any]]:
    metrics: dict[str, list[dict[str, float]]] = {}
    for name, (root, config) in paths.items():
        rows = []
        for window in WINDOWS:
            run = _run(root / f"{config}_{window}.json")
            summary = next(
                row
                for row in run["summaries"]
                if str(row["segment"]) == window and row["arm"] == GUARD
            )
            rows.append(
                {
                    "cumulative_return": float(summary["cumulative_return"]),
                    "max_drawdown": float(summary["max_drawdown"]),
                }
            )
        metrics[name] = rows
    return sel.walk_forward(metrics)


def main() -> int:  # noqa: C901, PLR0912, PLR0915 - one linear report builder
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT / "final_round"))
    parser.add_argument("--docs", default="docs/backtest_selection_summary.md")
    args = parser.parse_args()
    out = Path(args.out).expanduser().resolve()
    star = json.loads((out / "a_star.json").read_text())

    paths: dict[str, tuple[Path, str, str]] = {
        "v1 (V0) GUARD": (OUT / "v2/runs", "V0", GUARD),
        "V3": (OUT / "v2/runs", "V3", GUARD),
        "X5": (OUT / "v3x/runs", "X5", GUARD),
        "Y2": (OUT / "v4x/runs", "Y2", GUARD),
        "Y2 NOGUARD": (OUT / "v4x/runs", "Y2", NOGUARD),
        "MARKOWITZ_BASE": (OUT / "v4x/runs", "Y2", "MARKOWITZ_BASE"),
        "MARKOWITZ_BASE_GUARD": (OUT / "v4x/runs", "Y2", "MARKOWITZ_BASE_GUARD"),
        "SP500_EW_INTERNAL": (OUT / "v4x/runs", "Y2", "__benchmark__"),
        "A*": (out / "runs", star["config"], GUARD),
        "FINAL": (out / "runs", "FINAL", GUARD),
        "FINAL NOGUARD": (out / "runs", "FINAL", NOGUARD),
    }
    series: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
    for name, (root, config, arm) in paths.items():
        if arm == "__benchmark__":
            dates, _, rf = _load_chain(root, config, GUARD)
            series[name] = (dates, _benchmark(dates), rf)
        else:
            series[name] = _load_chain(root, config, arm)
    dates, _, rf = series["Y2"]
    series["SPY"] = (dates, _spy(dates), rf)

    lines = [
        "# Final round — selection summary (generated)",
        "",
        "Generated by `scripts/backtest_selection_summary.py` from the run JSONs. Aggregates only:",
        "no per-asset value, no weight series and no SPY price. All six windows have been seen;",
        "every result is in-sample.",
        "",
        "## (f) Reproduction of reviewer A's §10 figures, before this round's configurations",
        "",
        *_reproduce(series),
        *_tests_reproduction(series),
        "",
        "### (f3) Reviewer A's overfitting diagnostics, recomputed on the 21 round-2..4 configs",
        "",
    ]
    # The extra 21 series are loaded from the round-2/3/4 JSONs in their own roots.
    round24 = (
        {f"V{i}": (OUT / "v2/runs", f"V{i}") for i in range(6)}
        | {f"X{i}": (OUT / "v3x/runs", f"X{i}") for i in range(1, 12)}
        | {f"Y{i}": (OUT / "v4x/runs", f"Y{i}") for i in range(1, 5)}
    )
    extra: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
    for name, (root, config) in round24.items():
        extra[name] = _load_chain(root, config, GUARD)
    trial_names, rf, trial_matrix = _trial_matrix(series, extra)
    overfit_lines, diagnostics = _overfitting(series, trial_names, trial_matrix, rf, "Y2")
    lines += overfit_lines
    wf_paths = {name: (OUT / "v2/runs", name) for name in [f"V{i}" for i in range(6)]}
    wf_paths |= {name: (OUT / "v3x/runs", name) for name in [f"X{i}" for i in range(1, 12)]}
    wf_paths |= {name: (OUT / "v4x/runs", name) for name in [f"Y{i}" for i in range(1, 5)]}
    wf = _walk_forward(series, wf_paths)
    lines += [
        "| after window | chosen | mean cumulative | mean MDD | next window | rank in it |",
        "|---|---|---|---|---|---|",
    ]
    for row in wf:
        lines.append(
            f"| {row['after_window']} | {row['chosen']} | {row['mean_cumulative']:.4f} | "
            f"{row['mean_mdd']:.4f} | {row['next_window']} | {row['next_rank']} of "
            f"{row['n_configurations']} |"
        )
    lines.append("")
    checks = [
        (diagnostics["pbo"]["pbo"], REVIEWER_OVERFIT["pbo"], "PBO"),
        (diagnostics["pbo"]["mean_is_sharpe"], REVIEWER_OVERFIT["is_sharpe"], "IS Sharpe"),
        (diagnostics["pbo"]["mean_oos_sharpe"], REVIEWER_OVERFIT["oos_sharpe"], "OOS Sharpe"),
        (diagnostics["emax"][_ROUND24_TRIALS], REVIEWER_OVERFIT["emax21"], "E[max] 21"),
        (diagnostics["emax"][_DECLARED_TRIALS], REVIEWER_OVERFIT["emax55"], "E[max] 55"),
        (diagnostics["dsr"][_ROUND24_TRIALS]["dsr"], REVIEWER_OVERFIT["dsr21"], "DSR 21"),
        (diagnostics["dsr"][_DECLARED_TRIALS]["dsr"], REVIEWER_OVERFIT["dsr55"], "DSR 55"),
        (diagnostics["rc_mkt"]["p_value"], REVIEWER_OVERFIT["rc_mkt_p"], "RC mkt p"),
        (diagnostics["rc_spy"]["p_value"], REVIEWER_OVERFIT["rc_spy_p"], "RC SPY p"),
    ]
    failures = [
        f"{label}: {got:.4f} vs {want}" for got, want, label in checks if abs(got - want) > _TOL
    ]
    ranks = tuple(row["next_rank"] for row in wf)
    if ranks != REVIEWER_OVERFIT["wf_ranks"]:
        failures.append(f"walk-forward ranks {ranks} vs {REVIEWER_OVERFIT['wf_ranks']}")
    if failures:
        raise SystemExit("§10 overfitting reproduction failed: " + "; ".join(failures))
    lines += [
        "The reproduction matches reviewer A within the declared tolerances (PBO "
        f"{diagnostics['pbo']['pbo']:.3f} vs {REVIEWER_OVERFIT['pbo']:.3f}; walk-forward ranks "
        f"{ranks}).",
        "",
        "## This round's configurations",
        "",
        "### (a) Per window: cumulative / MDD",
        "",
    ]
    all_series = [name for name in series]
    header = "| series | " + " | ".join(WINDOWS) + " | mean cumulative | mean MDD | R |"
    lines += [header, "|" + "---|" * (len(WINDOWS) + 4)]
    for name in all_series:
        _, returns, _ = series[name]
        # Split the chained series back into the six windows by their known lengths.
        pieces = []
        start = 0
        for length in _window_lengths():
            piece = returns[start : start + length]
            start += length
            equity = np.concatenate([[1.0], np.cumprod(1.0 + piece)])
            mdd, _ = sel._drawdown(equity)
            pieces.append(f"{equity[-1] - 1.0:.4f} / {mdd:.4f}")
        mean_cum = float(np.mean([float(p.split(" / ")[0]) for p in pieces]))
        mean_mdd = float(np.mean([float(p.split(" / ")[1]) for p in pieces]))
        ratio = mean_cum / mean_mdd if mean_mdd else float("nan")
        lines.append(
            f"| {name} | "
            + " | ".join(pieces)
            + f" | {mean_cum:.4f} | {mean_mdd:.4f} | {ratio:.4f} |"
        )
    lines += ["", "### (b) Full period", ""]
    lines += [
        "| series | CAGR | Vol | Sharpe | Sortino | Calmar | MDD | max days UW | CVaR95 | Beta | Alpha/yr (t NW) | IR vs SPY | IR vs EW | up/down (geo) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    full: dict[str, dict[str, float | int]] = {}
    for name in all_series:
        _, returns, own_rf = series[name]
        metrics = sel.performance(returns, own_rf, series["SPY"][1], series["SP500_EW_INTERNAL"][1])
        full[name] = metrics
        lines.append(
            f"| {name} | {metrics['cagr'] * 100:.2f}% | {metrics['vol'] * 100:.2f}% | "
            f"{metrics['sharpe']:.2f} | {metrics['sortino']:.2f} | {metrics['calmar']:.2f} | "
            f"{metrics['mdd'] * 100:.2f}% | {metrics['longest_under_water']} | "
            f"{metrics['cvar95'] * 100:.2f}% | {metrics['beta']:.2f} | "
            f"{metrics['alpha'] * 100:+.2f}% ({metrics['alpha_t_nw']:.2f}) | "
            f"{metrics['ir_spy']:.2f} | {metrics['ir_ew']:.2f} | "
            f"{metrics['up_capture_geo'] * 100:.0f}% / {metrics['down_capture_geo'] * 100:.0f}% |"
        )
    lines += ["", "### (c) Full-period tests, FINAL against every other series", ""]
    lines += [
        "| FINAL vs | mean diff (pp/yr) | HAC p | JK-Memmel p | ΔMDD (pp) | bootstrap p | min. detectable (pp/yr) |",
        "|---|---|---|---|---|---|---|",
    ]
    final_returns = series["FINAL"][1]
    final_rf = series["FINAL"][2]
    for name in all_series:
        if name == "FINAL":
            continue
        table = sel.test_table(final_returns, series[name][1], final_rf)
        lines.append(
            f"| {name} | {table['mean_diff_annual'] * 100:+.2f} | {table['hac_p']:.3f} | "
            f"{table['jk_p']:.3f} | {table['mdd_diff'] * 100:+.2f} | {table['bootstrap_p']:.3f} | "
            f"±{table['min_detectable_annual'] * 100:.1f} |"
        )
    lines += ["", "### (d) Pareto table on {CAGR, vol, MDD, CVaR95, Sharpe}", ""]
    metric_keys = [
        ("cagr", True),
        ("vol", False),
        ("mdd", False),
        ("cvar95", False),
        ("sharpe", True),
    ]
    lines += ["| FINAL vs | wins | loses | dominates |", "|---|---|---|---|"]
    wins_count = 0
    for name in all_series:
        if name == "FINAL":
            continue
        wins, loses = [], []
        for key, higher in metric_keys:
            final_value = float(full["FINAL"][key])
            other_value = float(full[name][key])
            if (higher and final_value > other_value) or (not higher and final_value < other_value):
                wins.append(key)
            elif (higher and final_value < other_value) or (
                not higher and final_value > other_value
            ):
                loses.append(key)
        dominates = "FINAL" if not loses else (name if not wins else "neither")
        if not loses:
            wins_count += 1
        lines.append(
            f"| {name} | {', '.join(wins) or '-'} | {', '.join(loses) or '-'} | {dominates} |"
        )
    lines += ["", "### This round's trial count", ""]
    lines += [
        f"|T| with this round's configurations = **{diagnostics['n_trials']}**. The declared count of",
        "configurations tried after v1 is **55**: 45 after round 4 + 4 stage-A + 6 stage-B.",
        "",
    ]
    # Add the diagnostics over the extended trial set.
    extra_trials: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
    for config in ("Z1", "Z2", "Z3", "Z4"):
        path = out / "runs" / f"{config}_P1.json"
        if path.is_file():
            extra_trials[config] = _load_chain(out / "runs", config, GUARD)
    for config in ("B1", "B2", "B3", "B4", "B5", "B6"):
        path = out / "runs" / f"{config}_P1.json"
        if path.is_file():
            extra_trials[config] = _load_chain(out / "runs", config, GUARD)
    if extra_trials:
        z0 = _load_chain(out / "runs", "Z0", GUARD)[1]
        if "Z2" in extra_trials and np.array_equal(extra_trials["Z2"][1], z0):
            extra_trials.pop("Z2")
        extended = dict(extra)
        extended.update(extra_trials)
        names_ext, rf_ext, matrix_ext = _trial_matrix(series, extended)
        ext_lines, ext_diag = _overfitting(series, names_ext, matrix_ext, rf_ext, "FINAL")
        lines += ["### (e2) Overfitting diagnostics over the extended trial set", "", *ext_lines]
        lines += [
            "| diagnostic | round-2..4 (21) | extended |",
            "|---|---|---|",
            f"| PBO | {diagnostics['pbo']['pbo']:.3f} | {ext_diag['pbo']['pbo']:.3f} |",
            f"| mean IS / OOS Sharpe | {diagnostics['pbo']['mean_is_sharpe']:.2f} / "
            f"{diagnostics['pbo']['mean_oos_sharpe']:.2f} | {ext_diag['pbo']['mean_is_sharpe']:.2f} / "
            f"{ext_diag['pbo']['mean_oos_sharpe']:.2f} |",
            f"| E[max] N | {diagnostics['emax'][_ROUND24_TRIALS]:.3f} / "
            f"{diagnostics['emax'][_DECLARED_TRIALS]:.3f} | "
            f"{ext_diag['emax'][len(names_ext)]:.3f} / {ext_diag['emax'][_DECLARED_TRIALS]:.3f} |",
            f"| DSR FINAL (N = |T|) | {diagnostics['dsr'][_ROUND24_TRIALS]['dsr']:.3f} | "
            f"{ext_diag['dsr'][len(names_ext)]['dsr']:.3f} |",
            "",
        ]
    Path(args.docs).expanduser().resolve().write_text("\n".join(lines) + "\n")
    (out / "selection_summary.json").write_text(
        json.dumps(
            {
                "reviewer_point_estimates": REVIEWER,
                "reviewer_tests": REVIEWER_TESTS,
                "reviewer_overfitting": REVIEWER_OVERFIT,
                "overfitting_round24": diagnostics,
                "walk_forward": wf,
                "full_period": full,
                "trial_names": trial_names,
            },
            indent=1,
            default=str,
        )
        + "\n"
    )
    print(json.dumps({"docs": str(args.docs), "n_trials": len(trial_names)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
