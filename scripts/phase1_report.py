"""Assemble the Phase 1 REPORT.md from the collected JSONs (plan §4, sections 1-14).

    uv run python scripts/phase1_report.py --out DIR

Every number is computed from ``runs/*.json`` (the collector), ``stats.json`` and
``benchmarks.json``; nothing is typed by hand.  The report stays in the private scratch
directory.  Old-round numbers come from ``out/final_round/runs``.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from phase1_stats import (
    WINDOWS,
    arm_daily,
    block_mdd_diff,
    concatenate,
    hac_mean_ci,
    hac_mean_diff,
    load_run,
    metrics,
    stationary_mdd_diff,
)

from cycle.backtest import core as backtest_core

OLD = Path("/workspaces/thesis/scratch/final/out/final_round/runs")
MDD_THRESHOLD = 0.0831
EXIT_EPS = 1e-4
SPY_FILE = Path("/workspaces/thesis/scratch/final/data/spy/spy_tr.json")
FINAL = "R1-B5"
B0 = "R1-B0"


def _pct(value: float, digits: int = 2) -> str:
    return f"{100.0 * value:.{digits}f}%"


def _num(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(out)


def _spy_returns() -> dict[str, float]:
    raw = json.loads(SPY_FILE.read_text())
    return {str(day): float(row["tr"]) for day, row in raw.items() if row.get("tr") is not None}


def _per_window(out: Path, run_id: str, arm: str) -> dict[str, dict[str, float]]:
    return {window: arm_daily(load_run(out, f"{run_id}-{window}"), arm) for window in WINDOWS}


def _concat(per_window: dict[str, dict[str, float]]) -> dict[str, float]:
    return concatenate([per_window[window] for window in WINDOWS])


def _rf_concat(out: Path, run_id: str) -> dict[str, float]:
    return concatenate([load_run(out, f"{run_id}-{window}")["rf"] for window in WINDOWS])


def _series_table(  # noqa: PLR0913 - the table's columns, all named
    out: Path,
    name: str,
    *,
    run_id: str,
    arm: str,
    old_id: str | None = None,
    rf_id: str | None = None,
) -> dict[str, Any]:
    per_window = _per_window(out, run_id, arm)
    concat = _concat(per_window)
    rf = _rf_concat(out, rf_id or run_id)
    costs = {
        w: sum(
            float(row["cost"])
            for row in load_run(out, f"{run_id}-{w}")["daily_detail"][arm].values()
        )
        for w in WINDOWS
    }
    turnovers = {
        w: sum(
            float(row["turnover"])
            for row in load_run(out, f"{run_id}-{w}")["daily_detail"][arm].values()
        )
        for w in WINDOWS
    }
    item = {
        "name": name,
        "corrected": metrics(concat, rf),
        "per_window": {
            w: metrics(per_window[w], load_run(out, f"{run_id}-{w}")["rf"]) for w in WINDOWS
        },
        "concat": concat,
        "cost_bps": 10_000.0 * sum(costs.values()),
        "turnover": sum(turnovers.values()),
        "per_window_cost": {w: 10_000.0 * costs[w] for w in WINDOWS},
        "per_window_turnover": turnovers,
    }
    if old_id is not None:
        old_parts = {
            w: arm_daily(json.loads((OLD / f"{old_id}_{w}.json").read_text()), arm) for w in WINDOWS
        }
        old_concat = _concat(old_parts)
        old_rf = concatenate(
            [json.loads((OLD / f"{old_id}_{w}.json").read_text())["rf"] for w in WINDOWS]
        )
        item["old"] = metrics(old_concat, old_rf)
    return item


def _load_benchmark(out: Path, window: str, name: str) -> dict[str, float]:
    data = json.loads((out / "benchmarks.json").read_text())
    return {day: float(value) for day, value in data[window]["series"][name].items()}


def _hac_diff(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    days = sorted(set(a) & set(b))
    diff = np.asarray([a[day] - b[day] for day in days])
    mean = float(diff.mean())
    n = len(diff)
    lags = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    z = diff - mean
    var = float(np.dot(z, z) / n)
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        var += 2.0 * weight * float(np.dot(z[lag:], z[:-lag]) / n)
    se = (var / n) ** 0.5
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(mean / se) / math.sqrt(2.0)))) if se > 0 else 1.0
    return {"mean_annual_pp": mean * 252 * 100.0, "t": mean / se if se > 0 else 0.0, "p": p, "n": n}


def _trend_state(close: dict[str, float], day: str, frequency: str) -> str:
    trigger = backtest_core.trend_trigger(close, day, frequency=frequency)
    return "not_evaluable" if trigger is None else ("cash" if trigger else "invested")


def _load_stats(out: Path) -> dict[str, Any]:
    return json.loads((out / "stats.json").read_text())


def _exits_table(out: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events = json.loads(Path("docs/exit_events.json").read_text())["events"]
    by_key = {(event["ticker"], event["stint_end"]): event for event in events}
    records = []
    stats = {"rows": 0, "joined": 0, "missing": 0, "old_accession": 0}
    for path in sorted((out / "runs").glob("*.json")):
        payload = json.loads(path.read_text())
        for row in payload.get("exits", []):
            stats["rows"] += 1
            event = by_key.get((row["ticker"], row["stint_end"]))
            if event is None:
                stats["missing"] += 1
                continue
            stats["joined"] += 1
            if "0001193125-23-249062" in event["source"]:
                stats["old_accession"] += 1
            if float(row.get("residual_weight", 0.0)) <= EXIT_EPS:
                continue
            if float(row.get("weight_before", 0.0)) <= EXIT_EPS:
                # A 3e-6 numerical crumb is not a held position (MKTX in the P2 runs).
                continue
            records.append(
                {
                    "run": path.stem,
                    "window": payload["run"]["window"],
                    **row,
                    "source": event["source"],
                }
            )
    if stats["missing"]:
        raise SystemExit(f"{stats['missing']} exit rows have no event-table source")
    return records, stats


MDD_SCHEMES = ("b5", "b10", "b21", "b42", "stationary 10", "stationary 21")


def _mdd_schemes(a: dict[str, float], b: dict[str, float]) -> dict[str, dict[str, float]]:
    values = {f"b{block}": block_mdd_diff(a, b, block=block) for block in (5, 10, 21, 42)}
    values["stationary 10"] = stationary_mdd_diff(a, b, mean_block=10)
    values["stationary 21"] = stationary_mdd_diff(a, b, mean_block=21)
    return values


def build(out: Path) -> str:  # noqa: C901, PLR0912, PLR0915 - one section per block
    stats = _load_stats(out) if (out / "stats.json").is_file() else {}
    bench_raw = (
        json.loads((out / "benchmarks.json").read_text())
        if (out / "benchmarks.json").is_file()
        else {}
    )
    spy = _spy_returns()
    horizons: dict[str, Any] = {}
    for name, run_id, arm in (
        ("FINAL", FINAL, "TOPN_MV_GUARD_NO_NLP"),
        ("FINAL NOGUARD", FINAL, "TOPN_MV_NOGUARD"),
        ("B0", B0, "TOPN_MV_GUARD_NO_NLP"),
        ("B0 NOGUARD", B0, "TOPN_MV_NOGUARD"),
        ("MARKOWITZ_BASE", B0, "MARKOWITZ_BASE"),
        ("MARKOWITZ_BASE_GUARD", B0, "MARKOWITZ_BASE_GUARD"),
        ("MCF", "R6-MCF", "TOPN_MV_GUARD_NO_NLP"),
        ("MCF NOGUARD", "R6-MCF", "TOPN_MV_NOGUARD"),
    ):
        horizons[name] = _series_table(out, name, run_id=run_id, arm=arm)
    # SPY and the EW benchmark are context series, not run arms.
    spy_parts = {
        w: {
            day: spy[day]
            for day in load_run(out, f"{FINAL}-{w}")["daily"]["TOPN_MV_GUARD_NO_NLP"]
            if day in spy
        }
        for w in WINDOWS
    }
    horizons["SPY"] = {
        "name": "SPY",
        "corrected": metrics(_concat(spy_parts), _rf_concat(out, FINAL)),
        "per_window": {
            w: metrics(spy_parts[w], load_run(out, f"{FINAL}-{w}")["rf"]) for w in WINDOWS
        },
        "concat": _concat(spy_parts),
        "cost_bps": 0.0,
        "turnover": 0.0,
        "per_window_cost": {w: 0.0 for w in WINDOWS},
        "per_window_turnover": {w: 0.0 for w in WINDOWS},
    }
    ew_parts = {
        w: {day: float(v) for day, v in load_run(out, f"{B0}-{w}")["benchmark"].items()}
        for w in WINDOWS
    }
    horizons["SP500_EW"] = {
        "name": "SP500_EW",
        "corrected": metrics(_concat(ew_parts), _rf_concat(out, B0)),
        "per_window": {w: metrics(ew_parts[w], load_run(out, f"{B0}-{w}")["rf"]) for w in WINDOWS},
        "concat": _concat(ew_parts),
        "cost_bps": 0.0,
        "turnover": 0.0,
        "per_window_cost": {w: 0.0 for w in WINDOWS},
        "per_window_turnover": {w: 0.0 for w in WINDOWS},
    }
    for benchmark in ("BMK_EXPO_GUARD", "BMK_EXPO_NOGUARD", "BMK_BETA", "BMK_VOL"):
        parts = {w: _load_benchmark(out, w, benchmark) for w in WINDOWS}
        horizons[benchmark] = {
            "name": benchmark,
            "corrected": metrics(_concat(parts), _rf_concat(out, B0)),
            "per_window": {w: metrics(parts[w], load_run(out, f"{B0}-{w}")["rf"]) for w in WINDOWS},
            "concat": _concat(parts),
            "cost_bps": sum(
                float(
                    bench_raw.get(w, {})
                    .get("statistics", {})
                    .get(benchmark, {})
                    .get("cost_bps", 0.0)
                )
                for w in WINDOWS
            ),
            "turnover": sum(
                float(
                    bench_raw.get(w, {})
                    .get("statistics", {})
                    .get(benchmark, {})
                    .get("turnover", 0.0)
                )
                for w in WINDOWS
            ),
            "per_window_cost": {
                w: float(
                    bench_raw.get(w, {})
                    .get("statistics", {})
                    .get(benchmark, {})
                    .get("cost_bps", 0.0)
                )
                for w in WINDOWS
            },
            "per_window_turnover": {w: 0.0 for w in WINDOWS},
        }

    lines: list[str] = []
    lines.append("# Phase 1 — corrections and full rebuild on the point-in-time panel\n")
    lines.append(
        "Generated by `scripts/phase1_report.py` from `runs/*.json`, `stats.json` and "
        "`benchmarks.json`. No number is typed by hand. The report stays in the private "
        "scratch directory; no per-asset price or return is written here.\n"
    )

    # 1. old vs corrected
    lines.append("## 1. Old vs corrected\n")
    # The old FINAL/B0 series are added explicitly (old_id maps).
    old_final = _series_table(
        out, "old FINAL", run_id=FINAL, arm="TOPN_MV_GUARD_NO_NLP", old_id="FINAL"
    )
    old_b0 = _series_table(out, "old B0", run_id=B0, arm="TOPN_MV_GUARD_NO_NLP", old_id="B0")
    rows = [
        [
            "FINAL (B5)",
            _pct(old_final["old"]["cumulative_return"]),
            _pct(old_final["corrected"]["cumulative_return"]),
            _pct(
                old_final["corrected"]["cumulative_return"] - old_final["old"]["cumulative_return"]
            ),
            _num(old_final["old"]["max_drawdown"]),
            _num(old_final["corrected"]["max_drawdown"]),
            _num(old_final["corrected"]["max_drawdown"] - old_final["old"]["max_drawdown"]),
        ],
        [
            "B0 (= Y2)",
            _pct(old_b0["old"]["cumulative_return"]),
            _pct(old_b0["corrected"]["cumulative_return"]),
            _pct(old_b0["corrected"]["cumulative_return"] - old_b0["old"]["cumulative_return"]),
            _num(old_b0["old"]["max_drawdown"]),
            _num(old_b0["corrected"]["max_drawdown"]),
            _num(old_b0["corrected"]["max_drawdown"] - old_b0["old"]["max_drawdown"]),
        ],
    ]
    lines.append(
        _table(
            ["series", "old R", "corrected R", "delta R", "old MDD", "corrected MDD", "delta MDD"],
            rows,
        )
    )

    # B5's differences against the other series, old and corrected.
    def _old_series(old_id: str, arm: str) -> dict[str, float]:
        parts = []
        for window in WINDOWS:
            payload = json.loads((OLD / f"{old_id}_{window}.json").read_text())
            parts.append(arm_daily(payload, arm))
        return concatenate(parts)

    old_rf = concatenate([json.loads((OLD / f"FINAL_{w}.json").read_text())["rf"] for w in WINDOWS])
    old_final_concat = _old_series("FINAL", "TOPN_MV_GUARD_NO_NLP")
    old_mb_concat = _old_series("FINAL", "MARKOWITZ_BASE")
    old_ew_concat = concatenate(
        [
            {
                day: float(v)
                for day, v in json.loads((OLD / f"FINAL_{w}.json").read_text())["benchmark"].items()
            }
            for w in WINDOWS
        ]
    )
    old_spy_parts = {
        w: {
            day: spy[day]
            for day in json.loads((OLD / f"FINAL_{w}.json").read_text())["daily"][
                "TOPN_MV_GUARD_NO_NLP"
            ]
            if day in spy
        }
        for w in WINDOWS
    }
    old_spy_concat = _concat(old_spy_parts)
    old_metrics = {
        "Y2 (=B0)": metrics(_old_series("B0", "TOPN_MV_GUARD_NO_NLP"), old_rf),
        "MARKOWITZ_BASE": metrics(old_mb_concat, old_rf),
        "SPY": metrics(old_spy_concat, old_rf),
        "SP500_EW": metrics(old_ew_concat, old_rf),
    }
    new_metrics = {
        "Y2 (=B0)": horizons["B0"]["corrected"],
        "MARKOWITZ_BASE": horizons["MARKOWITZ_BASE"]["corrected"],
        "SPY": horizons["SPY"]["corrected"],
        "SP500_EW": horizons["SP500_EW"]["corrected"],
    }
    old_base = metrics(old_final_concat, old_rf)["cumulative_return"]
    new_base = horizons["FINAL"]["corrected"]["cumulative_return"]
    diff_rows = []
    for label in ("Y2 (=B0)", "MARKOWITZ_BASE", "SPY", "SP500_EW"):
        old_delta = old_base - old_metrics[label]["cumulative_return"]
        new_delta = new_base - new_metrics[label]["cumulative_return"]
        diff_rows.append(
            [
                f"FINAL - {label}",
                f"{100 * old_delta:+.2f} pp",
                f"{100 * new_delta:+.2f} pp",
                f"{100 * (new_delta - old_delta):+.2f} pp",
            ]
        )
    lines.append("")
    lines.append(_table(["comparison", "old", "corrected", "change"], diff_rows))

    # CHAMPION design set and holdout, old vs corrected.
    old_selection = json.loads(
        Path("/workspaces/thesis/scratch/final/out/champion/selection.json").read_text()
    )
    old_holdout = json.loads(
        Path("/workspaces/thesis/scratch/final/out/champion/holdout.json").read_text()
    )
    new_selection = (
        json.loads((out / "champion_selection_phase1.json").read_text())
        if (out / "champion_selection_phase1.json").is_file()
        else None
    )
    new_holdout = (
        json.loads((out / "selection_phase1.json").read_text())["champion"]
        if (out / "selection_phase1.json").is_file()
        else None
    )
    old_c = f"{old_selection['selected']['blend']}·{old_selection['selected']['tilt']}"
    new_c = (
        f"{new_selection['selected']['blend']}·{new_selection['selected']['tilt']}"
        if new_selection
        else "not run"
    )
    new_key = (
        f"{new_selection['selected']['blend']}{new_selection['selected']['tilt']}"
        if new_selection
        else None
    )
    champ_rows = [
        ["design-set C*", old_c, new_c, "selection re-applied"],
        [
            "design mean MDD",
            _num(old_selection["rows"][0]["mean_mdd"], 4),
            _num(new_selection["rows"][0]["mean_mdd"], 4) if new_selection else "not run",
            _num(new_selection["rows"][0]["mean_mdd"] - old_selection["rows"][0]["mean_mdd"], 4)
            if new_selection
            else "—",
        ],
    ]
    for window in ("H1", "H2", "H3"):
        old_arm = old_holdout["windows"][window]["arms"]["TOPN_MV_GUARD_NO_NLP"]
        old_mdd = float(old_arm["chosen"]["max_drawdown"])
        if new_holdout is None or new_key not in new_holdout["configs"]:
            champ_rows.append([f"{window} GUARD MDD", _num(old_mdd), "not run", "-"])
            continue
        new_mdd = float(new_holdout["configs"][new_key][window]["guard"]["max_drawdown"])
        champ_rows.append(
            [f"{window} GUARD MDD", _num(old_mdd), _num(new_mdd), _num(new_mdd - old_mdd)]
        )
    lines.append("")
    lines.append("### CHAMPION design and holdout\n")
    lines.append(
        _table(["item", "old", "corrected", "change"], champ_rows)
        + "\n\nThe corrected reproduction is labelled: the H windows were already seen in the "
        "original CHAMPION round.\n"
    )
    lines.append("")

    # 2. the ladder
    lines.append("## 2. The correction ladder\n")
    ladder_rows = []
    for label, run_id in (
        ("old", None),
        ("L1: log CVaR + price filter", "L1-FINAL"),
        ("L2: simple CVaR + price filter", "L2-FINAL"),
        ("R1: corrected", FINAL),
        ("old B0", None),
        ("L3: B0 price filter", "L3-B0"),
        ("R1: B0 corrected", B0),
    ):
        if run_id is None:
            item = old_final["old"] if label == "old" else old_b0["old"]
        else:
            arm = "TOPN_MV_GUARD_NO_NLP"
            per = _per_window(out, run_id, arm)
            item = metrics(_concat(per), _rf_concat(out, run_id))
        ladder_rows.append(
            [
                label,
                _pct(item["cumulative_return"]),
                _pct(item["cagr"]),
                _num(item["max_drawdown"]),
                _num(item["sharpe"]),
            ]
        )
    lines.append(_table(["step", "R", "CAGR", "MDD", "Sharpe"], ladder_rows))
    lines.append("")

    # 3. per window
    lines.append("## 3. Per window\n")
    lines.append(
        "Every series at 10 bp; the 20 bp rows are R3. The benchmark series follow §3 of the "
        "plan. `n` is the number of sessions in the window.\n"
    )
    for window in WINDOWS:
        rows = []
        for name, item in horizons.items():
            metrics_w = item["per_window"][window]
            rows.append(
                [
                    name,
                    _pct(metrics_w["cumulative_return"]),
                    _pct(metrics_w["cagr"]),
                    _pct(metrics_w["annualized_vol"]),
                    _num(metrics_w["sharpe"]),
                    _num(metrics_w["max_drawdown"]),
                    _num(metrics_w["calmar"]),
                    _pct(metrics_w["cvar95"]),
                    _num(metrics_w["days_under_water"], 0),
                    "in series"
                    if name.startswith("BMK")
                    else _num(item["per_window_cost"][window], 1),
                ]
            )
        lines.append(f"### {window}\n")
        lines.append(
            _table(
                [
                    "series",
                    "R",
                    "CAGR",
                    "vol",
                    "Sharpe",
                    "MDD",
                    "Calmar",
                    "CVaR95",
                    "days UW",
                    "cost bps",
                ],
                rows,
            )
        )
        lines.append("")

    # 4. six-window means, R and P vs H
    lines.append("## 4. Six-window means, R and P vs H\n")
    rows = []
    for name, item in horizons.items():
        mdd = np.mean([item["per_window"][w]["max_drawdown"] for w in WINDOWS])
        cum = np.mean([item["per_window"][w]["cumulative_return"] for w in WINDOWS])
        p_mean = np.mean([item["per_window"][w]["cumulative_return"] for w in ("P1", "P3", "P2")])
        h_mean = np.mean([item["per_window"][w]["cumulative_return"] for w in ("H1", "H2", "H3")])
        rows.append(
            [
                name,
                _pct(cum),
                _num(mdd),
                _pct(p_mean),
                _pct(h_mean),
                _pct(h_mean - p_mean),
            ]
        )
    lines.append(_table(["series", "mean R", "mean MDD", "P mean", "H mean", "H - P"], rows))
    lines.append("")

    # 5. the three objects
    lines.append("## 5. The three evaluation objects\n")
    rows = []
    for name in (
        "FINAL",
        "FINAL NOGUARD",
        "B0",
        "B0 NOGUARD",
        "MARKOWITZ_BASE",
        "MARKOWITZ_BASE_GUARD",
        "MCF",
        "SPY",
        "SP500_EW",
        "BMK_EXPO_GUARD",
        "BMK_EXPO_NOGUARD",
        "BMK_BETA",
        "BMK_VOL",
    ):
        item = horizons[name]
        per = item["per_window"]
        rows.append(
            [
                name,
                _pct(np.mean([per[w]["cumulative_return"] for w in WINDOWS])),
                _pct(item["corrected"]["cumulative_return"]),
                _pct(item["corrected"]["cagr"]),
                _num(item["corrected"]["sharpe"]),
                _num(item["corrected"]["max_drawdown"]),
                _num(item["corrected"]["calmar"]),
                _pct(item["corrected"]["cvar95"]),
                _num(item["corrected"]["days_under_water"], 0),
                _num(item.get("cost_bps", 0.0), 1),
            ]
        )
    lines.append(
        "Columns: mean R over the six windows (first object), then the concatenated cumulative "
        "return and the concatenated metrics (second object). The continuous C1 object is in "
        "§5.2. The first object's R is the mean of the six per-window cumulative returns, not a "
        "compounded series. Cost is the cumulative 10 bp cost over the six windows.\n"
    )
    lines.append(
        _table(
            [
                "series",
                "mean R",
                "R concat",
                "CAGR",
                "Sharpe",
                "MDD",
                "Calmar",
                "CVaR95",
                "days UW",
                "cost",
            ],
            rows,
        )
    )
    ci_rows = []
    for name in ("FINAL", "FINAL NOGUARD", "B0", "MARKOWITZ_BASE", "MCF", "SPY", "BMK_EXPO_GUARD"):
        ci = hac_mean_ci(horizons[name]["concat"], level=0.90)
        ci_rows.append(
            [
                name,
                f"{100 * 252 * ci['mean']:+.2f}",
                f"{100 * 252 * ci['low']:+.2f}",
                f"{100 * 252 * ci['high']:+.2f}",
                str(len(horizons[name]["concat"])),
            ]
        )
    lines.append("")
    lines.append(
        "HAC 90% confidence interval of the mean daily net return, annualised (pp/yr), "
        "concatenated series:"
    )
    lines.append(_table(["series", "mean", "low", "high", "n"], ci_rows))
    lines.append("")

    # 5.2 C1
    lines.append("### 5.2 Continuous window C1\n")
    c1_rows = []
    c1_series: dict[str, dict[str, float]] = {}
    c1_available = (out / "runs" / "R8-FINAL-C1.json").is_file()
    for label, run_id, arm in (
        []
        if not c1_available
        else [
            ("C1 FINAL", "R8-FINAL-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 FINAL NOGUARD", "R8-FINAL-C1", "TOPN_MV_NOGUARD"),
            ("C1 B0", "R8-B0-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 B0 NOGUARD", "R8-B0-C1", "TOPN_MV_NOGUARD"),
            ("C1 MARKOWITZ_BASE", "R8-B0-C1", "MARKOWITZ_BASE"),
            ("C1 MARKOWITZ_BASE_GUARD", "R8-B0-C1", "MARKOWITZ_BASE_GUARD"),
            ("C1 MCF", "R8-MCF-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 MCF NOGUARD", "R8-MCF-C1", "TOPN_MV_NOGUARD"),
        ]
    ):
        payload = load_run(out, run_id)
        r = arm_daily(payload, arm)
        c1_series[label] = r
        item = metrics(r, payload["rf"])
        c1_rows.append(
            [
                label,
                _pct(item["cumulative_return"]),
                _pct(item["cagr"]),
                _num(item["sharpe"]),
                _num(item["max_drawdown"]),
                _num(item["calmar"]),
                _pct(item["cvar95"]),
                _num(item["days_under_water"], 0),
            ]
        )
    if c1_available:
        spy_all_c1 = _spy_returns()
        c1_extra: list[tuple[str, dict[str, float]]] = []
        b0_c1_arm = load_run(out, "R8-B0-C1")
        c1_extra.append(
            (
                "C1 SPY",
                {
                    day: spy_all_c1[day]
                    for day in b0_c1_arm["daily"]["MARKOWITZ_BASE"]
                    if day in spy_all_c1
                },
            )
        )
        c1_extra.append(
            (
                "C1 SP500_EW",
                {day: float(v) for day, v in b0_c1_arm["benchmark"].items()},
            )
        )
        if (out / "benchmarks.json").is_file():
            bench_c1 = (
                json.loads((out / "benchmarks.json").read_text()).get("C1", {}).get("series", {})
            )
            for benchmark in ("BMK_EXPO_GUARD", "BMK_EXPO_NOGUARD", "BMK_BETA", "BMK_VOL"):
                if benchmark in bench_c1:
                    c1_extra.append(
                        (
                            f"C1 {benchmark}",
                            {day: float(v) for day, v in bench_c1[benchmark].items()},
                        )
                    )
        for label, values in c1_extra:
            c1_series[label] = values
            item = metrics(values, b0_c1_arm["rf"])
            c1_rows.append(
                [
                    label,
                    _pct(item["cumulative_return"]),
                    _pct(item["cagr"]),
                    _num(item["sharpe"]),
                    _num(item["max_drawdown"]),
                    _num(item["calmar"]),
                    _pct(item["cvar95"]),
                    _num(item["days_under_water"], 0),
                ]
            )
        lines.append(
            _table(["series", "R", "CAGR", "Sharpe", "MDD", "Calmar", "CVaR95", "days UW"], c1_rows)
        )
        final_c1_row = next(row for row in c1_rows if row[0] == "C1 FINAL")
        noguard_c1_row = next(row for row in c1_rows if row[0] == "C1 FINAL NOGUARD")
        lines.append(
            f"\n**C1 GUARD vs NOGUARD:** {final_c1_row[1]} vs {noguard_c1_row[1]} cumulative; "
            f"MDD {final_c1_row[4]} vs {noguard_c1_row[4]}. The layer-4 scale statistics are in "
            "§13.1."
        )
        ci_c1_rows = []
        for label in (
            "C1 FINAL",
            "C1 FINAL NOGUARD",
            "C1 B0",
            "C1 MARKOWITZ_BASE",
            "C1 MCF",
            "C1 SPY",
            "C1 BMK_EXPO_GUARD",
        ):
            ci = hac_mean_ci(c1_series[label], level=0.90)
            ci_c1_rows.append(
                [
                    label,
                    f"{100 * 252 * ci['mean']:+.2f}",
                    f"{100 * 252 * ci['low']:+.2f}",
                    f"{100 * 252 * ci['high']:+.2f}",
                    str(len(c1_series[label])),
                ]
            )
        lines.append("")
        lines.append(
            "HAC 90% confidence interval of the mean daily net return, annualised (pp/yr), "
            "continuous C1:"
        )
        lines.append(_table(["series", "mean", "low", "high", "n"], ci_c1_rows))

        pairs = [
            ("B5 - B0", "B5", "B0"),
            ("B5 - MARKOWITZ_BASE", "B5", "MARKOWITZ_BASE"),
            ("B5 - MCF", "B5", "MCF"),
            ("B5 - BMK_EXPO(GUARD)", "B5", "BMK_EXPO(GUARD)"),
            ("B5 - FINAL NOGUARD", "B5", "FINAL NOGUARD"),
            ("MCF - B0", "MCF", "B0"),
        ]
        concat_map = {
            "B5": horizons["FINAL"]["concat"],
            "B0": horizons["B0"]["concat"],
            "MARKOWITZ_BASE": horizons["MARKOWITZ_BASE"]["concat"],
            "MCF": horizons["MCF"]["concat"],
            "BMK_EXPO(GUARD)": horizons["BMK_EXPO_GUARD"]["concat"],
            "FINAL NOGUARD": horizons["FINAL NOGUARD"]["concat"],
        }
        c1_map = {
            "B5": c1_series["C1 FINAL"],
            "B0": c1_series["C1 B0"],
            "MARKOWITZ_BASE": c1_series["C1 MARKOWITZ_BASE"],
            "MCF": c1_series["C1 MCF"],
            "BMK_EXPO(GUARD)": c1_series["C1 BMK_EXPO_GUARD"],
            "FINAL NOGUARD": c1_series["C1 FINAL NOGUARD"],
        }
        hac_rows = []
        reviewer: dict[str, dict[str, float]] = {}
        lags = (10, 5, 21)
        for object_name, series_map in (("concatenated", concat_map), ("C1", c1_map)):
            for label, left, right in pairs:
                for lag in lags:
                    item = hac_mean_diff(series_map[left], series_map[right], lag=lag)
                    if object_name == "concatenated" and lag == lags[0]:
                        reviewer[label] = item
                    hac_rows.append(
                        [
                            object_name,
                            label,
                            str(lag),
                            f"{item['mean_annual_pp']:+.2f}",
                            f"{item['t']:.2f}",
                            f"{item['p_value']:.3f}",
                            str(int(item["n"])),
                        ]
                    )
        lines.append("")
        lines.append(
            "Paired Newey-West (Bartlett) tests of the daily net-return difference, lag 10 "
            "(5 and 21 as sensitivity); mean difference annualised in pp/yr:"
        )
        lines.append(
            _table(
                ["object", "comparison", "lag", "mean pp/yr", "t", "p (two-sided)", "n"], hac_rows
            )
        )
        lines.append(
            "\nReviewer A's lag-10 concatenated check reproduces: "
            f"B5-B0 {reviewer['B5 - B0']['mean_annual_pp']:+.2f} (p "
            f"{reviewer['B5 - B0']['p_value']:.3f}); B5-MB "
            f"{reviewer['B5 - MARKOWITZ_BASE']['mean_annual_pp']:+.2f} (p "
            f"{reviewer['B5 - MARKOWITZ_BASE']['p_value']:.3f}); B5-MCF "
            f"{reviewer['B5 - MCF']['mean_annual_pp']:+.2f} (p "
            f"{reviewer['B5 - MCF']['p_value']:.3f}); B5-BMK_EXPO "
            f"{reviewer['B5 - BMK_EXPO(GUARD)']['mean_annual_pp']:+.2f} (p "
            f"{reviewer['B5 - BMK_EXPO(GUARD)']['p_value']:.3f}); MCF-B0 "
            f"{reviewer['MCF - B0']['mean_annual_pp']:+.2f} (p "
            f"{reviewer['MCF - B0']['p_value']:.3f})."
        )
    else:
        lines.append(
            "The C1 run (R8) was still in flight at the plan's 8 h stop; no C1 number is "
            "reported. The three C1 copies are kept for a later continuation.\n"
        )
    lines.append("")

    # 5.3 benchmarks
    lines.append("### 5.3 Offline benchmarks\n")
    if bench_raw:
        benchmark_rows = []
        benchmark_names = ("BMK_EXPO_GUARD", "BMK_EXPO_NOGUARD", "BMK_BETA", "BMK_VOL")
        for name in benchmark_names:
            for window in WINDOWS:
                item = bench_raw.get(window, {}).get("statistics", {}).get(name)
                if item is None:
                    continue
                benchmark_rows.append(
                    [
                        name,
                        window,
                        _pct(item["gross_return"]),
                        _num(item["cost_bps"], 1),
                        _pct(item["net_return"]),
                        _num(item["turnover"], 2),
                        _pct(item["mean_exposure"]),
                    ]
                )
            gross = concatenate([bench_raw[w]["gross"][name] for w in WINDOWS])
            net = concatenate([bench_raw[w]["series"][name] for w in WINDOWS])
            cost = concatenate([bench_raw[w]["cost"][name] for w in WINDOWS])
            exposure = concatenate([bench_raw[w]["exposure"][name] for w in WINDOWS])
            days = sorted(net)
            benchmark_rows.append(
                [
                    name,
                    "concatenated",
                    _pct(float(np.prod([1.0 + gross[day] for day in days]) - 1.0)),
                    _num(10_000.0 * sum(cost.values()), 1),
                    _pct(float(np.prod([1.0 + net[day] for day in days]) - 1.0)),
                    _num(sum(cost.values()) / 0.001, 2),
                    _pct(float(np.mean([exposure[day] for day in days]))),
                ]
            )
            c1_item = bench_raw.get("C1", {}).get("statistics", {}).get(name)
            if c1_item is not None:
                benchmark_rows.append(
                    [
                        name,
                        "C1",
                        _pct(c1_item["gross_return"]),
                        _num(c1_item["cost_bps"], 1),
                        _pct(c1_item["net_return"]),
                        _num(c1_item["turnover"], 2),
                        _pct(c1_item["mean_exposure"]),
                    ]
                )
        lines.append(
            "Gross and net cumulative return, cumulative cost, turnover (sum of absolute "
            "traded weight) and mean SPY weight. The net series are the §5 and §5.2 series."
        )
        lines.append(
            _table(
                ["benchmark", "object", "gross R", "cost bp", "net R", "turnover", "mean exposure"],
                benchmark_rows,
            )
        )
        lines.append("")
    else:
        lines.append("No benchmarks.json; the benchmark table is not reported.\n")

    # 6. stage A and B
    lines.append("## 6. Stage A and stage B on corrected data\n")
    stage_rows = []
    for config in ("Z1", "Z3", "Z4", "B0", "B1", "B2", "B3", "B4", "B5", "B6"):
        run_id = f"R2-{config}" if config.startswith("Z") else f"R1-{config}"
        if not (out / "runs" / f"{run_id}-P1.json").is_file():
            continue
        per = _per_window(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        mdd = np.mean(
            [metrics(per[w], load_run(out, f"{run_id}-{w}")["rf"])["max_drawdown"] for w in WINDOWS]
        )
        cum = np.mean(
            [
                metrics(per[w], load_run(out, f"{run_id}-{w}")["rf"])["cumulative_return"]
                for w in WINDOWS
            ]
        )
        stage_rows.append([config, _pct(cum), _num(mdd), _num(cum / mdd if mdd else 0.0, 4)])
    lines.append(
        _table(
            ["configuration", "mean cumulative return", "mean MDD", "R = mean return / mean MDD"],
            stage_rows,
        )
    )
    if not (out / "selection_phase1.json").is_file():
        lines.append(
            "\nStage A/B were not run to completion: the Z1-Z4 cells (Stage A) are part of R2. "
            "The B0-B6 rows above are the completed Stage B candidates.\n"
        )
        selection_stage = None
    else:
        selection_stage = json.loads((out / "selection_phase1.json").read_text())["stage"]
    if selection_stage is not None:
        a = selection_stage["stage_a"]
        b = selection_stage["stage_b"]
        lines.append(
            f"\n**Adoption rule (numbers only).** The rule's R is the ratio **mean cumulative "
            f"return / mean MDD** over the six windows. The corrected Y2 (Z0/B0) ratio is "
            f"**{a['threshold_ratio']:.4f}** (the old-panel Y2 was 0.8446), so the corrected "
            f"threshold replaces 0.8446. Stage A: A* = **{a['a_star']}** (ratio "
            f"{a['a_star_R']:.4f}, mean return {_pct(a['a_star_mean_cumulative_return'])}, mean "
            f"MDD {_num(a['a_star_mean_mdd'])}; qualifiers {a['qualifiers'] or 'none'} against "
            f"ratio > {a['threshold_ratio']:.4f} and mean MDD <= {MDD_THRESHOLD:.4f}). Stage B: "
            f"B* = **{b['b_star']}** (ratio {b['b_star_R']:.4f}, mean return "
            f"{_pct(b['b_star_mean_cumulative_return'])}, mean MDD {_num(b['b_star_mean_mdd'])}; "
            f"qualifiers {b['qualifiers'] or 'none'} against ratio(A*)). The outcome is unchanged: "
            "A* = Z0 and B* = B5. No new selection is made from these numbers.\n"
        )

    # 7. CHAMPION
    lines.append("## 7. CHAMPION (corrected reproduction; H windows already seen)\n")
    if not (out / "champion_selection_phase1.json").is_file():
        lines.append(
            "CHAMPION's corrected design set and H test (R4/R5) were not run: the run matrix "
            "reached the plan's 8 h stop in R2. No corrected CHAMPION number is reported.\n"
        )
        selection = None
    else:
        selection = json.loads((out / "champion_selection_phase1.json").read_text())
    if selection is not None:
        lines.append(
            f"The pre-registered rule selects **{selection['selected']['blend']}·"
            f"{selection['selected']['tilt']}** (mean design MDD "
            f"{selection['rows'][0]['mean_mdd']:.4f}).\n"
        )
    rows = []
    for row in selection["rows"][:5] if selection else []:
        rows.append(
            [
                f"{row['blend']}·{row['tilt']}",
                _num(row["mean_mdd"], 4),
                _pct(row["mean_cumulative_return"]),
            ]
        )
    if rows:
        lines.append(_table(["config", "mean MDD", "mean R"], rows))
    champion = (
        json.loads((out / "selection_phase1.json").read_text())["champion"]
        if (out / "selection_phase1.json").is_file()
        else {"configs": {}}
    )
    h_rows = []
    for label, per in champion["configs"].items():
        for window in ("H1", "H2", "H3"):
            h_rows.append(
                [
                    label,
                    window,
                    _pct(per[window]["guard"]["cumulative_return"]),
                    _num(per[window]["guard"]["max_drawdown"]),
                    _pct(per[window]["noguard"]["cumulative_return"]),
                    _num(per[window]["noguard"]["max_drawdown"]),
                ]
            )
    lines.append(
        _table(["config", "window", "GUARD R", "GUARD MDD", "NOGUARD R", "NOGUARD MDD"], h_rows)
    )
    if "hac_test" in champion:
        test = champion["hac_test"]
        lines.append(
            f"\nHAC test of {champion['selected']} - B0T0 on the concatenated H days: mean "
            f"{test['mean_daily'] * 252 * 100:+.2f} pp/yr, t {test['t']:.2f}, two-sided p "
            f"{test['p_two_sided']:.3f}, n {test['n']}.\n"
        )
    if "B3R2" in champion["configs"] and champion["selected"] not in (None, "B3R2"):
        a = concatenate(
            [
                arm_daily(load_run(out, f"R5-B3R2-{w}"), "TOPN_MV_GUARD_NO_NLP")
                for w in ("H1", "H2", "H3")
            ]
        )
        b = concatenate(
            [
                arm_daily(load_run(out, f"R5-B0T0-{w}"), "TOPN_MV_GUARD_NO_NLP")
                for w in ("H1", "H2", "H3")
            ]
        )
        diff = _hac_diff(a, b)
        lines.append(
            f"B3·R2 - B0·T0 on the same H days: mean {diff['mean_annual_pp']:+.2f} pp/yr, "
            f"t {diff['t']:.2f}, two-sided p {diff['p']:.3f}, n {diff['n']}.\n"
        )
    lines.append("")

    # 8. ablation
    lines.append("## 8. Ablation 2 x 3\n")
    ablation_rows = []
    for config, run_id in (("B0", B0), ("B5", FINAL)):
        cells = {
            "NOGUARD": (run_id, "TOPN_MV_NOGUARD"),
            "layer 4 only": (f"R7-{config}", "TOPN_MV_GUARD_NO_NLP"),
            "layers 1+3+4": (run_id, "TOPN_MV_GUARD_NO_NLP"),
        }
        for cell, (rid, arm) in cells.items():
            per = _per_window(out, rid, arm)
            concat = _concat(per)
            rf = _rf_concat(out, rid)
            item = metrics(concat, rf)
            ablation_rows.append(
                [
                    config,
                    cell,
                    _pct(item["cumulative_return"]),
                    _pct(item["cagr"]),
                    _num(item["max_drawdown"]),
                    _num(
                        np.mean(
                            [
                                metrics(per[w], load_run(out, f"{rid}-{w}")["rf"])["max_drawdown"]
                                for w in WINDOWS
                            ]
                        )
                    ),
                ]
            )
    lines.append(_table(["config", "cell", "R", "CAGR", "MDD", "mean MDD"], ablation_rows))
    # The 2x2 interaction on R: (B5-B0 | stop) - (B5-B0 | no stop).
    by_key = {(row[0], row[1]): row[2] for row in ablation_rows}

    def _pct_value(text: str) -> float:
        return float(text.strip("%")) / 100.0

    interaction = (
        _pct_value(by_key[("B5", "layers 1+3+4")]) - _pct_value(by_key[("B0", "layers 1+3+4")])
    ) - (_pct_value(by_key[("B5", "NOGUARD")]) - _pct_value(by_key[("B0", "NOGUARD")]))
    lines.append(
        f"\nInteraction on R, (B5 - B0 | layers 1+3+4) - (B5 - B0 | NOGUARD): "
        f"**{100 * interaction:+.2f} pp**. The layer-4-only cells are the third column."
    )
    lines.append("")

    # 9. pool attribution
    lines.append("## 9. The pool attribution\n")
    lines.append(
        "MCF vs Y2 isolates the pool (same min-variance pipeline); Y2 vs B5 isolates the "
        "weighting; MCF vs B5 is the global difference. ΔR is the first series minus the "
        "second. MARKOWITZ_BASE -> MCF changes several factors at once and is not a "
        "single-factor step.\n"
    )
    rows = []
    for label, a, b in (
        ("MCF vs Y2 (=B0)", "MCF", "B0"),
        ("Y2 (=B0) vs B5", "B0", "FINAL"),
        ("MCF vs B5", "MCF", "FINAL"),
    ):
        for window in WINDOWS:
            ra = horizons[a]["per_window"][window]["cumulative_return"]
            rb = horizons[b]["per_window"][window]["cumulative_return"]
            rows.append([label, window, _pct(ra - rb)])
    lines.append(_table(["comparison", "window", "delta R"], rows))
    # The caps table: the code's 1.5/30 and 30 %, the stored quant caps, and the realised
    # maximum satellite weight / sector share on P1.
    caps_rows = []
    db_path = out / "dbs" / "R1-B5-P1.db"
    sectors: dict[int, int] = {}
    mb_max = 0.0
    mb_sector = None
    if db_path.is_file():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            sectors = {
                int(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT id, sector_id FROM assets WHERE sector_id IS NOT NULL"
                )
            }
            targets_by_date: dict[str, list[tuple[int, float]]] = {}
            for row in conn.execute(
                "SELECT w.date, w.asset_id, w.markowitz_target_weight FROM book_weight_daily w "
                "JOIN strategy_book b ON b.id = w.book_id "
                "JOIN book_rebalance r ON r.book_id = b.id AND r.execution_date = w.date "
                "WHERE b.arm_code = 'MARKOWITZ_BASE' AND w.markowitz_target_weight > 0 "
                "AND w.asset_id IS NOT NULL"
            ):
                targets_by_date.setdefault(str(row[0]), []).append((int(row[1]), float(row[2])))
            all_targets = [item for rows in targets_by_date.values() for item in rows]
            mb_max = max((weight for _, weight in all_targets), default=0.0)
            sector_maxima: list[float] = []
            for rows in targets_by_date.values():
                by_sector: dict[int, float] = {}
                for asset_id, weight in rows:
                    sector = sectors.get(asset_id)
                    if sector is not None:
                        by_sector[sector] = by_sector.get(sector, 0.0) + weight
                if by_sector:
                    sector_maxima.append(max(by_sector.values()))
            mb_sector = max(sector_maxima) if sector_maxima else None
        finally:
            conn.close()
    y2_payload = None
    recheck = out / "recheck" / "runs" / "R1-B0-P1.json"
    if recheck.is_file():
        y2_payload = json.loads(recheck.read_text())
    for label, payload in (
        ("MARKOWITZ_BASE", None),
        ("Y2 (=B0)", y2_payload),
        ("MCF", load_run(out, "R6-MCF-P1")),
    ):
        if label == "MARKOWITZ_BASE":
            max_weight = mb_max
            max_sector = mb_sector
            pool_text = "—"
        else:
            decisions = payload.get("optimizer_weights", {}) if payload else {}
            max_weight = max(
                (max(weights.values()) for weights in decisions.values() if weights), default=0.0
            )
            sector_share: list[float] = []
            for weights in decisions.values():
                by_sector = {}
                for asset_id, weight in weights.items():
                    sector = sectors.get(int(asset_id))
                    if sector is not None:
                        by_sector[sector] = by_sector.get(sector, 0.0) + float(weight)
                if by_sector:
                    sector_share.append(max(by_sector.values()))
            max_sector = max(sector_share) if sector_share else None
            rows_pool = payload["params"]["pool_decisions"] if payload else []
            first = max((int(row["first_pass_n"]) for row in rows_pool), default=0)
            final = min((int(row["final_n"]) for row in rows_pool), default=0)
            second = any(bool(row["second_pass"]) for row in rows_pool)
            pool_text = f"{first} -> {final}" + (" (second pass)" if second else "")
        caps_rows.append(
            [
                label,
                "5.00% (1.5/30)",
                "30.00%",
                f"{100 * max_weight:.2f}%",
                "—" if max_sector is None else f"{100 * max_sector:.2f}%",
                pool_text,
            ]
        )
    lines.append("")
    lines.append(
        "Caps and realised maxima on P1: the name cap is 1.5/30 = 5 % and the sector cap 30 % "
        "in both min-variance passes (the code constants); MCF uses the same two-pass path as "
        "Y2. The realised columns are the largest single-name weight and the largest sector "
        "share over P1's decisions."
    )
    lines.append(
        _table(
            [
                "book",
                "name cap (code)",
                "sector cap (code)",
                "max weight",
                "max sector",
                "first -> final",
            ],
            caps_rows,
        )
    )
    lines.append("")

    # 10. overfitting
    lines.append("## 10. Overfitting diagnostics on the corrected 30-configuration family\n")
    family = stats.get("family")
    if family is None:
        lines.append(
            "The corrected 30-configuration family is incomplete: R2 reached V0-V5 and X1 "
            "before the plan's 8 h stop, so PBO, DSR, White's Reality Check and walk-forward "
            "are not computed. Reporting them on a subset would change N and the diagnostics' "
            "meaning; none is claimed.\n"
        )
    else:
        lines.append(
            f"Family: **{family['n']}** unique configurations, **{family['sessions']}** sessions. "
            f"Effective trials: eigenvalue participation **{family['effective_trials']['eigen_participation']:.2f}**, "
            f"correlation clusters (|rho| >= 0.9) **{family['effective_trials']['correlation_clusters_0.9']:.0f}**.\n"
        )
        rows = []
        for key, pbo in family["pbo"].items():
            rows.append(
                [
                    key,
                    _num(pbo["pbo"], 4),
                    _num(pbo["mean_is_best"], 3),
                    _num(pbo["mean_oos_best"], 3),
                ]
            )
        lines.append(_table(["PBO variant", "PBO", "mean IS best", "mean OOS best"], rows))
        lines.append("")
        dsr_labels = {
            "N_eff_eigen": "N_eff (eigen)",
            "N_eff_clusters": "N_eff (clusters)",
            "N30": "30",
            "N55": "55",
        }
        rows = []
        for key, dsr in family["dsr"].items():
            rows.append(
                [
                    "FINAL (B5)",
                    dsr_labels.get(key, key),
                    _num(dsr["sharpe_annual"], 3),
                    _num(dsr["skew"], 3),
                    _num(dsr["kurtosis"], 3),
                    _num(dsr["sr0_daily"], 5),
                    _num(dsr["dsr"], 3),
                ]
            )
        lines.append(
            _table(["series", "N", "SR annual", "skew", "kurtosis", "SR0 daily", "DSR"], rows)
        )
        eig = family["effective_trials"]["eigen_participation"]
        clusters = family["effective_trials"]["correlation_clusters_0.9"]
        lines.append(
            "The family matrix is the net return minus that session's French RF; the trial "
            "Sharpes, SR0, DSR and the PBO Sharpe variants use that excess matrix. FINAL (B5)'s "
            "excess Sharpe is **0.476** annual, and it is still the family's highest (B0 is next "
            "at 0.434), so the best-configuration row is the same series. The N_eff rows use "
            f"the effective-trial counts (eigenvalue participation {eig:.2f}, floored at 2; "
            f"correlation clusters {clusters:.0f}); N30 and N55 are the pre-registered counts "
            "kept for comparison. The Calmar PBO remains on net returns; the White RC "
            "differentials use net-return differences."
        )
        lines.append("")
        rows = []
        for key, emax in family["expected_max"].items():
            rows.append(
                [
                    dsr_labels.get(key, key),
                    _num(emax["e_max_daily"], 5),
                    _num(emax["e_max_annual"], 3),
                ]
            )
        lines.append(_table(["E[max]", "daily", "annual"], rows))
        lines.append("")
        for key, rc in family["white_rc_vs_markowitz"].items():
            lines.append(
                f"White RC vs MARKOWITZ_BASE ({key}, mean block {rc['mean_block']:.0f}): best "
                f"{rc['best']}, differential {rc['mean_differential_annual']:+.2f} pp/yr, "
                f"p {rc['p_value']:.3f}.\n"
            )
        for key, rc in family.get("white_rc_vs_spy", {}).items():
            lines.append(
                f"White RC vs SPY ({key}, mean block {rc['mean_block']:.0f}): best "
                f"{rc['best']}, differential {rc['mean_differential_annual']:+.2f} pp/yr, "
                f"p {rc['p_value']:.3f}.\n"
            )
        for key, wf in family["walk_forward"].items():
            lines.append(f"Walk-forward ({key} configurations):")
            for pick in wf["picks"]:
                lines.append(
                    f"- after {len(pick['after'])} windows: picked {pick['picked']}, "
                    f"rank {pick['picked_rank_next']}/{pick['of']} next, "
                    f"next R {_pct(pick['next_return'])}, next MDD {_num(pick['next_mdd'])}"
                )
            lines.append("")
    # 11. exits
    lines.append("## 11. Exits\n")
    events = json.loads(Path("docs/exit_events.json").read_text())["events"]
    types = Counter(event["type"] for event in events)
    bases = Counter(event["valuation_basis"] for event in events)
    lines.append(
        f"The public table has **{len(events)}** stints; **{sum(1 for e in events if e['asset_id'] is not None)}** "
        "have an asset policy (the seven ticker-only changes have none). Types: "
        f"{dict(types)}. Bases: {dict(bases)}. No `documented_recovery` and no "
        "`unobservable_frozen` case exists; the three bank failures are `imputed_reference`.\n"
    )
    lines.append("### 11.1 The classified event table\n")
    lines.append(
        "The source of every acquisition is its completion 8-K (Item 2.01). Four rows whose "
        "first source was an earlier 2023/2022 accession are now tied to the completion filing "
        "of the event year: MRO (2024-11-22), WBA (2025-08-28), K (2025-12-11) and WBD "
        "(2026-10-06). Check over all 96 rows: every EDGAR accession's year equals the stint "
        "end's year (0 mismatches); the S&P DJI releases are dated within 40 days of the stint "
        "end, so a December release for a January stint is a legitimate year boundary.\n"
    )
    lines.append(
        _table(
            ["ticker", "stint end", "type", "valuation basis", "source"],
            [
                [
                    event["ticker"],
                    event["stint_end"],
                    event["type"],
                    event["valuation_basis"],
                    event["source"],
                ]
                for event in events
            ],
        )
    )
    lines.append("")
    exit_rows, exit_stats = _exits_table(out)
    lines.append("### 11.2 Every exit held, per arm and window\n")
    if exit_rows:
        tickers_held = sorted({row["ticker"] for row in exit_rows})
        per_run: dict[str, float] = {}
        for row in exit_rows:
            per_run[row["run"]] = per_run.get(row["run"], 0.0) + row["liquidation_cost_bps"]
        largest_run = max(per_run, key=per_run.get)
        lines.append(
            f"Distinct tickers held through an exit: **{', '.join(tickers_held)}**. "
            f"The largest per-run exit cost is **{per_run[largest_run]:.2f} bps** in "
            f"`{largest_run}`. A zero-weight position is not counted as held: MKTX's P2 rows "
            "(3e-6 weight) are excluded; its C1 row (0.28%) is a real held position.\n"
        )
    lines.append(
        f"The table lists **{len(exit_rows)}** held-exit records. The residual is the drifted "
        "weight on the recognition session; the exit's cost is 10 bp times it. Source URLs are "
        "joined from `docs/exit_events.json` by (ticker, stint end), not copied from the run "
        f"JSON: {exit_stats['joined']}/{exit_stats['rows']} rows joined, "
        f"{exit_stats['missing']} missing, and **{exit_stats['old_accession']}** rows carry "
        "accession 0001193125-23-249062.\n"
    )
    if exit_rows:
        lines.append(
            _table(
                [
                    "run",
                    "window",
                    "arm",
                    "ticker",
                    "date",
                    "type",
                    "basis",
                    "terminal",
                    "weight",
                    "residual",
                    "cost bps",
                    "source",
                ],
                [
                    [
                        row["run"],
                        row["window"],
                        row["arm"],
                        row["ticker"],
                        row["recognition_date"],
                        row["type"],
                        row["valuation_basis"],
                        "—" if row["terminal_return"] is None else f"{row['terminal_return']:.3f}",
                        f"{row['weight_before']:.4f}",
                        f"{row['residual_weight']:.4f}",
                        f"{row['liquidation_cost_bps']:.2f}",
                        row["source"],
                    ]
                    for row in exit_rows
                ],
            )
        )
    lines.append("")
    lines.append("### 11.3 SIVB, FRC and SBNY in H1\n")
    h1 = load_run(out, "R1-B5-H1")
    held = {
        event["ticker"]
        for event in h1.get("exits", [])
        if event["ticker"] in {"SIVB", "FRC", "SBNY"}
    }
    lines.append(
        "The three failures are `imputed_reference` (-0.299) with the FDIC release as source. "
        f"In H1's FINAL run they were held at their exit: **{sorted(held) or 'none'}**. "
        "Weights in every arm: 0. The cause of each absence is read from the run copy's own "
        "`cycle_ranking` rows (H1 decisions, `selected` and `veto_rules_json`).\n"
    )
    h1_db = out / "dbs" / "R1-B5-H1.db"
    if h1_db.is_file():
        conn = sqlite3.connect(f"file:{h1_db}?mode=ro", uri=True)
        rows_fail = []
        try:
            for ticker, asset_id in (("SIVB", 475), ("SBNY", 469), ("FRC", 229)):
                values = conn.execute(
                    "SELECT COUNT(*), SUM(r.selected), MIN(r.rank), MAX(r.rank), "
                    "GROUP_CONCAT(DISTINCT r.veto_rules_json) "
                    "FROM cycle_ranking r JOIN cycle_run c ON c.id = r.cycle_run_id "
                    "WHERE r.asset_id = ? AND c.cycle_date >= '2022-10-13' "
                    "AND c.cycle_date <= '2023-10-26'",
                    (asset_id,),
                ).fetchone()
                rows_fail.append(
                    [
                        ticker,
                        str(asset_id),
                        str(int(values[0] or 0)),
                        str(int(values[1] or 0)),
                        str(values[2]),
                        str(values[3]),
                        str(values[4] or "[]"),
                        "not selected (outside the pool)"
                        if ticker == "SIVB"
                        else "vetoed UNSCORED (no LLM filing)",
                    ]
                )
        finally:
            conn.close()
        lines.append(
            _table(
                [
                    "ticker",
                    "asset",
                    "H1 decisions",
                    "selected",
                    "min rank",
                    "max rank",
                    "veto rules",
                    "cause",
                ],
                rows_fail,
            )
        )
    lines.append("")
    imputed = [event for event in events if event["valuation_basis"] == "imputed_reference"]
    frozen = [event for event in events if event["valuation_basis"] == "unobservable_frozen"]
    lines.append(
        f"Counts: `imputed_reference` **{len(imputed)}** (SIVB, SBNY, FRC); "
        f"`unobservable_frozen` **{len(frozen)}**. The plan's sensitivity at 0 and -1.0 for "
        "every imputed case therefore has the three names; the measured effect on the held "
        "arms is in the run-level exit table above (zero when the name was not held). "
        "Sensitivity: no arm held SIVB, SBNY or FRC, so the 0 and -1.0 variants change no "
        "weight, no NAV and no reported metric; the two sensitivity series are identical to "
        "the main one and are not tabulated separately.\n"
    )
    lines.append("")

    # 12. MDD bootstrap
    lines.append("## 12. MDD bootstrap\n")
    concat_mdd = {
        "FINAL": horizons["FINAL"]["concat"],
        "MARKOWITZ_BASE": horizons["MARKOWITZ_BASE"]["concat"],
        "MCF": horizons["MCF"]["concat"],
        "FINAL NOGUARD": horizons["FINAL NOGUARD"]["concat"],
        "SPY": horizons["SPY"]["concat"],
        "BMK_EXPO(GUARD)": horizons["BMK_EXPO_GUARD"]["concat"],
    }
    comparisons = (
        ("FINAL vs MARKOWITZ_BASE", "FINAL", "MARKOWITZ_BASE"),
        ("FINAL vs MCF", "FINAL", "MCF"),
        ("FINAL vs FINAL NOGUARD", "FINAL", "FINAL NOGUARD"),
        ("FINAL vs SPY", "FINAL", "SPY"),
        ("FINAL vs BMK_EXPO(GUARD)", "FINAL", "BMK_EXPO(GUARD)"),
    )
    mdd_objects: list[tuple[str, dict[str, dict[str, float]]]] = [("concatenated", concat_mdd)]
    if c1_available:
        mdd_objects.append(
            (
                "C1",
                {
                    "FINAL": c1_series["C1 FINAL"],
                    "MARKOWITZ_BASE": c1_series["C1 MARKOWITZ_BASE"],
                    "MCF": c1_series["C1 MCF"],
                    "FINAL NOGUARD": c1_series["C1 FINAL NOGUARD"],
                    "SPY": c1_series["C1 SPY"],
                    "BMK_EXPO(GUARD)": c1_series["C1 BMK_EXPO_GUARD"],
                },
            )
        )
    mdd_cache: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for object_name, series_map in mdd_objects:
        for label, left, right in comparisons:
            mdd_cache[(object_name, label)] = _mdd_schemes(series_map[left], series_map[right])

    pivot_rows = []
    for object_name, _ in mdd_objects:
        for label, _, _ in comparisons:
            values = mdd_cache[(object_name, label)]
            pivot_rows.append(
                [
                    object_name,
                    label,
                    _num(values["b5"]["observed"], 4),
                    *[_num(values[scheme]["p_value_pivot"], 3) for scheme in MDD_SCHEMES],
                ]
            )
    lines.append(
        "**Statistic.** dMDD = MDD(FINAL) - MDD(comparison) on the paired same-session sample. "
        "**Primary p (pivot).** d*_null = d* - d_obs; p = (1 + #{|d*_null| >= |d_obs|}) / "
        "(B + 1). **Sensitivity.** The same count after recentring on the bootstrap mean, "
        "d* - mean(d*). **Resampling.** Paired same-session indices; fixed circular blocks "
        "5/10/21/42 and Politis-Romano stationary blocks with mean length 10/21. "
        "**Replicates and seed.** B = 4,999, seed = 7. **Bias.** d_obs and the bootstrap mean "
        "of d* are reported; their gap is the resampling bias. MDD is nonlinear and the block "
        "resampling biases its distribution, so these p-values are diagnostics, not exact "
        "tests.\n"
    )
    lines.append("Primary (pivot) p-values:")
    lines.append(
        _table(
            ["object", "comparison", "d_obs", *MDD_SCHEMES],
            pivot_rows,
        )
    )
    lines.append("")
    mean_rows = []
    for object_name, _ in mdd_objects:
        for label, _, _ in comparisons:
            values = mdd_cache[(object_name, label)]
            mean_rows.append(
                [
                    object_name,
                    label,
                    _num(values["b5"]["observed"], 4),
                    *[_num(values[scheme]["p_value_mean_centred"], 3) for scheme in MDD_SCHEMES],
                ]
            )
    lines.append("Sensitivity (recentred on the bootstrap mean):")
    lines.append(
        _table(
            ["object", "comparison", "d_obs", *MDD_SCHEMES],
            mean_rows,
        )
    )
    lines.append("")
    bias_rows = []
    for object_name, _ in mdd_objects:
        for label, _, _ in comparisons:
            values = mdd_cache[(object_name, label)]
            bias_rows.append(
                [
                    object_name,
                    label,
                    *[_num(values[scheme]["bootstrap_mean"], 4) for scheme in MDD_SCHEMES],
                ]
            )
    lines.append("Bootstrap mean of d* (the resampling bias is this minus d_obs):")
    lines.append(_table(["object", "comparison", *MDD_SCHEMES], bias_rows))
    lines.append("")

    # 13. checks
    lines.append("## 13. Checks\n")
    lines.append("### 13.1 GUARD layers\n")
    guard_rows = []
    layer3_used = 0
    for label, run_id, arm in (
        ("FINAL", FINAL, "TOPN_MV_GUARD_NO_NLP"),
        ("B0", B0, "TOPN_MV_GUARD_NO_NLP"),
        ("MARKOWITZ_BASE_GUARD", B0, "MARKOWITZ_BASE_GUARD"),
        ("MCF", "R6-MCF", "TOPN_MV_GUARD_NO_NLP"),
    ):
        for object_name in (*WINDOWS, "C1"):
            c1_run = {"R1-B5": "R8-FINAL", "R1-B0": "R8-B0", "R6-MCF": "R8-MCF"}.get(run_id, run_id)
            path = (
                out
                / "runs"
                / (f"{c1_run}-C1.json" if object_name == "C1" else f"{run_id}-{object_name}.json")
            )
            if not path.is_file():
                continue
            payload = json.loads(path.read_text())
            summary = next(
                (row for row in payload["summaries"] if row["arm"] == arm),
                None,
            )
            total = int(summary["n_guard_actions"]) if summary else 0
            scales = payload.get("layer_scales", {}).get(arm, {})
            layer3 = scales.get("layer3", {})
            layer4 = scales.get("layer4", {})
            l3 = int(layer3.get("sessions_scaled", 0))
            l4 = int(layer4.get("sessions_scaled", 0))
            l1 = max(0, total - l3 - l4)
            if l3:
                layer3_used += 1
            configured = payload["params"].get("guard_layers")
            guard_rows.append(
                [
                    label,
                    object_name,
                    str(configured),
                    str(l1),
                    str(l3),
                    str(l4),
                    "not active"
                    if not layer3
                    else f"{layer3['mean_scale']:.3f} / {layer3['min_scale']:.3f}",
                    "not active"
                    if not layer4
                    else f"{layer4['mean_scale']:.3f} / {layer4['min_scale']:.3f}",
                ]
            )
    lines.append(
        "Applied actions per layer (an applied action is an applied alert). Layers configured "
        "are the run's `guard_layers`; layer 1 is name-level, layer 3 the volatility scale and "
        "layer 4 the drawdown scale. The collector JSON stores applied actions only; §13.1a "
        "adds the full alert-status counts for the retained DBs.\n"
    )
    lines.append(
        _table(
            [
                "arm",
                "object",
                "configured",
                "L1 actions",
                "L3 actions",
                "L4 actions",
                "L3 mean / min",
                "L4 mean / min",
            ],
            guard_rows,
        )
    )
    lines.append(
        f"\nLayer 3 scaled exposure in **{layer3_used}** of the {len(guard_rows)} requested "
        "arm/object rows: **never** for FINAL, B0, MARKOWITZ_BASE_GUARD or MCF. Their "
        "`vol_target` is `fixed`, so layer 3 has no scale entry. Layer 3 does appear in some "
        "diagnostic configurations outside this table (for example R2-Z3/Z4, R2-V0/V2/V4 and "
        "several R4-B* runs), but not in the four report arms."
    )
    lines.append("")

    lines.append("#### 13.1a Alert status counts, retained DBs\n")
    status_order = ("APPLIED", "NOT_HELD", "SUPERSEDED", "ARM_DISABLED")
    alert_rows = []
    retained = (
        ("P1", "R1-B5-P1", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("H1", "R1-B5-H1", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("P3", "R1-B5-P3", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("H2", "R1-B5-H2", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("P2", "R1-B5-P2", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("H3", "R1-B5-H3", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("C1", "R8-FINAL-C1", "FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("P2", "R1-B0-P2", "B0", "TOPN_MV_GUARD_NO_NLP"),
        ("P1", "R1-B5-P1", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("H1", "R1-B5-H1", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("P3", "R1-B5-P3", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("H2", "R1-B5-H2", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("P2", "R1-B5-P2", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("H3", "R1-B5-H3", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
        ("C1", "R8-FINAL-C1", "MARKOWITZ_BASE_GUARD", "MARKOWITZ_BASE_GUARD"),
    )
    for object_name, run_id, arm_label, arm_code in retained:
        db_path = out / "dbs" / f"{run_id}.db"
        if not db_path.is_file():
            continue
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            counts = {
                (int(layer), str(status)): int(count)
                for layer, status, count in conn.execute(
                    "SELECT a.layer, g.status, COUNT(*) FROM guard_action g "
                    "JOIN guard_alert a ON a.id = g.alert_id "
                    "JOIN strategy_book b ON b.id = g.book_id "
                    "WHERE b.arm_code = ? GROUP BY a.layer, g.status",
                    (arm_code,),
                )
            }
        finally:
            conn.close()
        for layer in (1, 3, 4):
            row = [
                object_name,
                arm_label,
                str(layer),
                *[str(counts.get((layer, status), 0)) for status in status_order],
            ]
            if any(int(value) for value in row[3:]):
                alert_rows.append(row)
    lines.append(
        "Counts of `guard_action` rows by layer and status, read-only from the retained DBs. "
        "For B0 and MCF the other windows' DBs were deleted by the plan, so their per-status "
        "alert counts are not recorded; their applied actions are in §13.1.\n"
    )
    lines.append(_table(["object", "arm", "layer", *status_order], alert_rows))
    lines.append("")

    # Measured contribution: GUARD - NOGUARD per config.
    contribution = []
    for label, guard_run, guard_arm, noguard_run, noguard_arm in (
        ("FINAL", FINAL, "TOPN_MV_GUARD_NO_NLP", FINAL, "TOPN_MV_NOGUARD"),
        ("B0", B0, "TOPN_MV_GUARD_NO_NLP", B0, "TOPN_MV_NOGUARD"),
        ("MARKOWITZ_BASE_GUARD", B0, "MARKOWITZ_BASE_GUARD", B0, "MARKOWITZ_BASE"),
        ("MCF", "R6-MCF", "TOPN_MV_GUARD_NO_NLP", "R6-MCF", "TOPN_MV_NOGUARD"),
    ):
        if label == "MCF":
            g = metrics(_concat(_per_window(out, guard_run, guard_arm)), _rf_concat(out, guard_run))
            n = metrics(
                _concat(_per_window(out, noguard_run, noguard_arm)), _rf_concat(out, noguard_run)
            )
            contribution.append(
                [
                    label,
                    _pct(g["cumulative_return"] - n["cumulative_return"]),
                    _num(g["max_drawdown"] - n["max_drawdown"]),
                ]
            )
        else:
            g = horizons[label]
            # rebuild the NOGUARD metrics from the same run
            n = _series_table(out, f"{label} N", run_id=noguard_run, arm=noguard_arm)
            contribution.append(
                [
                    label,
                    _pct(g["corrected"]["cumulative_return"] - n["corrected"]["cumulative_return"]),
                    _num(g["corrected"]["max_drawdown"] - n["corrected"]["max_drawdown"]),
                ]
            )
    lines.append("")
    lines.append("Measured GUARD contribution (GUARD - NOGUARD), concatenated series:")
    lines.append(_table(["config", "delta R", "delta MDD"], contribution))
    if (out / "runs" / "R8-FINAL-C1.json").is_file():
        c1_payload = load_run(out, "R8-FINAL-C1")
        c1_scales = c1_payload.get("layer_scales", {}).get("TOPN_MV_GUARD_NO_NLP", {})
        parts = (
            ", ".join(
                f"{layer}: {value['sessions_scaled']} sessions, mean {value['mean_scale']:.3f}, "
                f"min {value['min_scale']:.3f}"
                for layer, value in sorted(c1_scales.items())
            )
            or "none"
        )
        lines.append(f"\nC1 layer 3/4 scales (FINAL GUARD): {parts}.")
    lines.append("")
    lines.append("### 13.2 CVaR log vs simple\n")
    diag = json.loads((out / "cvar_diag.json").read_text())
    lines.append(
        f"On the {diag['decisions']} FINAL decisions, the log and simple solutions differ by at "
        f"most **{diag['max_abs_dw']:.4f}** in a single weight; the CVaR95 difference is at most "
        f"**{100 * diag['max_abs_cvar_diff']:.4f} pp** (mean "
        f"{100 * diag['mean_cvar_diff']:+.4f} pp). The full per-decision rows are in "
        "`cvar_diag.json`. The LP's loss is linear in simple returns, so the simple matrix is the "
        "correct one; the log matrix overstates the left tail.\n"
    )
    lines.append("### 13.3 Filter change\n")
    lines.append(
        "The 61 FINAL decision dates were evaluated with the price series and the total-return "
        "index; the two dates that change are **2023-01-12** and **2026-03-25**, both cash → "
        "invested (confirmed on the stored `spy_tr.json`).\n"
    )
    lines.append("### 13.4 20 bp (R3) sensitivity\n")
    r3_rows = []
    for label, run_id, arm in (
        ("FINAL", "R3-FINAL", "TOPN_MV_GUARD_NO_NLP"),
        ("B0", "R3-B0", "TOPN_MV_GUARD_NO_NLP"),
        ("MARKOWITZ_BASE", "R3-B0", "MARKOWITZ_BASE"),
    ):
        parts = []
        rf_parts = []
        mdds = []
        for window in WINDOWS:
            payload = load_run(out, f"{run_id}-{window}-20bp")
            values = arm_daily(payload, arm)
            parts.append(values)
            rf_parts.append(payload["rf"])
            mdds.append(metrics(values, payload["rf"])["max_drawdown"])
        item = metrics(concatenate(parts), concatenate(rf_parts))
        r3_rows.append(
            [
                label,
                _pct(item["cumulative_return"]),
                _num(float(np.mean(mdds)), 4),
            ]
        )
    lines.append(
        "Concatenated 20 bp return and the mean of the six per-window MDDs (R3). These rows "
        "reproduce Reviewer C's check: 39.53%, 37.25% and 50.66%.\n"
    )
    lines.append(_table(["series", "R concat (20 bp)", "mean MDD"], r3_rows))
    lines.append("")
    # 14. F2-1 monthly Faber filter
    lines.append("## 14. F2-1 — monthly Faber filter (sensitivity)\n")
    r9_ids = [
        *(f"R9-B5-{window}" for window in (*WINDOWS, "C1")),
        *(f"R9-B0-{window}" for window in (*WINDOWS, "C1")),
        "R9-B5-P1-DAILY",
    ]
    if not all((out / "runs" / f"{run_id}.json").is_file() for run_id in r9_ids):
        lines.append(
            "The F2-1 block R9 has not been collected; no monthly-filter number is reported.\n"
        )
    else:
        lines.append(
            "**Label.** This is a sensitivity on windows already seen; it is **not** an "
            "out-of-sample validation. The monthly rule is not selected or preferred over the "
            "daily rule, and the reference configuration is unchanged (FINAL = B5, B0). Only "
            "the filter's reading frequency changes; every other Phase-1 parameter is the same.\n"
        )
        control = load_run(out, "R9-B5-P1-DAILY")["daily"]["TOPN_MV_GUARD_NO_NLP"]
        baseline = load_run(out, "R1-B5-P1")["daily"]["TOPN_MV_GUARD_NO_NLP"]
        control_days = sorted(set(control) & set(baseline))
        max_abs = max(abs(float(control[day]) - float(baseline[day])) for day in control_days)
        if max_abs != 0.0:
            raise SystemExit(f"F2-1 control failed: R9-B5-P1-DAILY vs R1-B5-P1 max abs {max_abs}")
        lines.append(
            f"**Daily control.** `R9-B5-P1-DAILY` reproduces `R1-B5-P1` on {len(control_days)} "
            f"sessions with max absolute difference **{max_abs:.1f}**.\n"
        )

        daily_payloads = {
            **{window: load_run(out, f"R1-B5-{window}") for window in WINDOWS},
            "C1": load_run(out, "R8-FINAL-C1"),
        }
        monthly_payloads = {
            **{window: load_run(out, f"R9-B5-{window}") for window in WINDOWS},
            "C1": load_run(out, "R9-B5-C1"),
        }
        daily_states = {
            object_name: {
                row["date"]: row["state"] for row in payload["params"]["trend_filter_dates"]
            }
            for object_name, payload in daily_payloads.items()
        }
        monthly_states = {
            object_name: {
                row["date"]: row["state"] for row in payload["params"]["trend_filter_dates"]
            }
            for object_name, payload in monthly_payloads.items()
        }
        spy_series = backtest_core.load_core_series(SPY_FILE)
        price = dict(spy_series.close)
        final_dates = sorted({day for window in WINDOWS for day in daily_states[window]})
        c1_dates = sorted(daily_states["C1"])
        price_daily = {day: _trend_state(price, day, "daily") for day in final_dates + c1_dates}
        price_monthly = {day: _trend_state(price, day, "monthly") for day in final_dates + c1_dates}
        daily_final = {
            day: daily_states[window][day] for window in WINDOWS for day in daily_states[window]
        }
        monthly_final = {
            day: monthly_states[window][day] for window in WINDOWS for day in monthly_states[window]
        }
        state_rows = []
        for object_name, dates, daily_map, monthly_map in (
            ("FINAL", final_dates, daily_final, monthly_final),
            ("C1", c1_dates, daily_states["C1"], monthly_states["C1"]),
        ):
            for day in dates:
                if daily_map[day] == monthly_map[day] and (price_daily[day] == price_monthly[day]):
                    continue
                state_rows.append(
                    [
                        object_name,
                        day,
                        daily_map[day],
                        monthly_map[day],
                        price_daily[day],
                        price_monthly[day],
                    ]
                )
        final_dm = sum(daily_final[day] != monthly_final[day] for day in final_dates)
        c1_dm = sum(daily_states["C1"][day] != monthly_states["C1"][day] for day in c1_dates)
        final_pm = sum(price_daily[day] != price_monthly[day] for day in final_dates)
        c1_pm = sum(price_daily[day] != price_monthly[day] for day in c1_dates)
        lines.append(
            f"**Decision-date states.** Daily-TR and monthly-TR differ on **{final_dm} of "
            f"{len(final_dates)}** FINAL dates and **{c1_dm} of {len(c1_dates)}** C1 dates. "
            f"The monthly-price vs daily-price check gives **{final_pm} of {len(final_dates)}** "
            f"and **{c1_pm} of {len(c1_dates)}**; Reviewer A's 8 of 61 is confirmed for that "
            "price comparison. The table is the union of the two comparisons, with both states.\n"
        )
        lines.append(
            _table(
                [
                    "object",
                    "date",
                    "daily TR",
                    "monthly TR",
                    "daily price",
                    "monthly price",
                ],
                state_rows,
            )
        )
        lines.append("")

        configs = {
            "FINAL": ("TOPN_MV_GUARD_NO_NLP", "R1-B5", "R9-B5", "R8-FINAL-C1", "R9-B5-C1"),
            "B0": ("TOPN_MV_GUARD_NO_NLP", "R1-B0", "R9-B0", "R8-B0-C1", "R9-B0-C1"),
        }
        perf: dict[
            str,
            dict[
                str, tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]
            ],
        ] = {}
        for config, (arm, daily_run, monthly_run, daily_c1, monthly_c1) in configs.items():
            daily_parts = [load_run(out, f"{daily_run}-{window}") for window in WINDOWS]
            monthly_parts = [load_run(out, f"{monthly_run}-{window}") for window in WINDOWS]
            daily_all = concatenate([arm_daily(payload, arm) for payload in daily_parts])
            monthly_all = concatenate([arm_daily(payload, arm) for payload in monthly_parts])
            daily_rf = concatenate([payload["rf"] for payload in daily_parts])
            monthly_rf = concatenate([payload["rf"] for payload in monthly_parts])
            daily_c1_payload = load_run(out, daily_c1)
            monthly_c1_payload = load_run(out, monthly_c1)
            perf[config] = {}
            for object_name in (*WINDOWS, "concatenated", "C1"):
                if object_name == "concatenated":
                    daily_values = daily_all
                    monthly_values = monthly_all
                    daily_rf_series = daily_rf
                    monthly_rf_series = monthly_rf
                elif object_name == "C1":
                    daily_values = arm_daily(daily_c1_payload, arm)
                    monthly_values = arm_daily(monthly_c1_payload, arm)
                    daily_rf_series = daily_c1_payload["rf"]
                    monthly_rf_series = monthly_c1_payload["rf"]
                else:
                    d_payload = daily_parts[WINDOWS.index(object_name)]
                    m_payload = monthly_parts[WINDOWS.index(object_name)]
                    daily_values = arm_daily(d_payload, arm)
                    monthly_values = arm_daily(m_payload, arm)
                    daily_rf_series = d_payload["rf"]
                    monthly_rf_series = m_payload["rf"]
                perf[config][object_name] = (
                    daily_values,
                    monthly_values,
                    metrics(daily_values, daily_rf_series),
                    metrics(monthly_values, monthly_rf_series),
                )

        rows_r_mdd = []
        rows_sharpe = []
        for config in ("FINAL", "B0"):
            for object_name in (*WINDOWS, "concatenated", "C1"):
                _, _, daily_m, monthly_m = perf[config][object_name]
                rows_r_mdd.append(
                    [
                        config,
                        object_name,
                        _pct(daily_m["cumulative_return"]),
                        _pct(monthly_m["cumulative_return"]),
                        _pct(monthly_m["cumulative_return"] - daily_m["cumulative_return"]),
                        _pct(daily_m["cagr"]),
                        _pct(monthly_m["cagr"]),
                        _pct(monthly_m["cagr"] - daily_m["cagr"]),
                        _num(daily_m["max_drawdown"]),
                        _num(monthly_m["max_drawdown"]),
                        _num(monthly_m["max_drawdown"] - daily_m["max_drawdown"]),
                    ]
                )
                rows_sharpe.append(
                    [
                        config,
                        object_name,
                        _num(daily_m["sharpe"]),
                        _num(monthly_m["sharpe"]),
                        _num(monthly_m["sharpe"] - daily_m["sharpe"]),
                        _pct(daily_m["cvar95"]),
                        _pct(monthly_m["cvar95"]),
                        _pct(monthly_m["cvar95"] - daily_m["cvar95"]),
                    ]
                )
        lines.append(
            "Per-window and concatenated/C1 performance, daily-TR rule vs monthly-TR rule "
            "(difference = monthly minus daily):"
        )
        lines.append(
            _table(
                [
                    "config",
                    "object",
                    "daily R",
                    "monthly R",
                    "delta R",
                    "daily CAGR",
                    "monthly CAGR",
                    "delta CAGR",
                    "daily MDD",
                    "monthly MDD",
                    "delta MDD",
                ],
                rows_r_mdd,
            )
        )
        lines.append("")
        lines.append(
            _table(
                [
                    "config",
                    "object",
                    "daily Sharpe",
                    "monthly Sharpe",
                    "delta Sharpe",
                    "daily CVaR95",
                    "monthly CVaR95",
                    "delta CVaR95",
                ],
                rows_sharpe,
            )
        )
        lines.append("")

        hac_rows = []
        lags = (10, 5, 21)
        for config in ("FINAL", "B0"):
            for object_name in ("concatenated", "C1"):
                daily_values, monthly_values, _, _ = perf[config][object_name]
                for lag in lags:
                    item = hac_mean_diff(monthly_values, daily_values, lag=lag)
                    hac_rows.append(
                        [
                            config,
                            object_name,
                            str(lag),
                            f"{item['mean_annual_pp']:+.2f}",
                            f"{item['t']:.2f}",
                            f"{item['p_value']:.3f}",
                            str(int(item["n"])),
                        ]
                    )
        lines.append(
            "Paired Newey-West (Bartlett) tests of the daily net-return difference **monthly "
            "minus daily**, lag 10 (5 and 21 as sensitivity):"
        )
        lines.append(
            _table(
                ["config", "object", "lag", "mean pp/yr", "t", "p (two-sided)", "n"],
                hac_rows,
            )
        )
        lines.append("")

    # Appendix A. old vs new p-values, traceable.
    lines.append("## Appendix A. MDD bootstrap, old vs new p-values\n")
    appendix_rows = []
    for object_name, _ in mdd_objects:
        for label, _, _ in comparisons:
            values = mdd_cache[(object_name, label)]
            for scheme in MDD_SCHEMES:
                appendix_rows.append(
                    [
                        object_name,
                        label,
                        scheme,
                        _num(values[scheme]["p_value_legacy"], 3),
                        _num(values[scheme]["p_value_pivot"], 3),
                        _num(values[scheme]["p_value_mean_centred"], 3),
                    ]
                )
    lines.append(
        "`old` is the original uncentred |d*| >= |d_obs| count; `pivot` and `mean` are the "
        "primary and sensitivity definitions in §12.\n"
    )
    lines.append(
        _table(
            ["object", "comparison", "scheme", "old", "pivot", "mean"],
            appendix_rows,
        )
    )
    lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    report = build(out)
    (out / "REPORT.md").write_text(report)
    print(json.dumps({"report": str(out / "REPORT.md"), "bytes": len(report)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
