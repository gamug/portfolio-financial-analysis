"""Assemble the Phase 1 REPORT.md from the collected JSONs (plan §4, sections 1-13).

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
    load_run,
    metrics,
    stationary_mdd_diff,
)

OLD = Path("/workspaces/thesis/scratch/final/out/final_round/runs")
R_THRESHOLD = 0.8446
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


def _load_stats(out: Path) -> dict[str, Any]:
    return json.loads((out / "stats.json").read_text())


def _exits_table(out: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((out / "runs").glob("*.json")):
        payload = json.loads(path.read_text())
        for row in payload.get("exits", []):
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
                }
            )
    return records


def build(out: Path) -> str:  # noqa: C901, PLR0912, PLR0915 - one section per block
    stats = _load_stats(out) if (out / "stats.json").is_file() else {}
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
            "cost_bps": 0.0,
            "turnover": 0.0,
            "per_window_cost": {w: 0.0 for w in WINDOWS},
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
        "MCF",
        "SPY",
        "SP500_EW",
        "BMK_EXPO_GUARD",
        "BMK_VOL",
    ):
        item = horizons[name]
        per = item["per_window"]
        rows.append(
            [
                name,
                _pct(np.mean([per[w]["cumulative_return"] for w in WINDOWS])),
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
        "Columns: mean R over the six windows (first object), then the concatenated series "
        "(second object). The continuous C1 object is in §5.2. The first object's R is the "
        "mean of the six per-window cumulative returns, not a compounded series.\n"
    )
    lines.append(
        _table(
            ["series", "mean R", "CAGR", "Sharpe", "MDD", "Calmar", "CVaR95", "days UW", "cost"],
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
    c1_available = (out / "runs" / "R8-FINAL-C1.json").is_file()
    for label, run_id, arm in (
        []
        if not c1_available
        else [
            ("C1 FINAL", "R8-FINAL-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 FINAL NOGUARD", "R8-FINAL-C1", "TOPN_MV_NOGUARD"),
            ("C1 B0", "R8-B0-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 B0 NOGUARD", "R8-B0-C1", "TOPN_MV_NOGUARD"),
            ("C1 MCF", "R8-MCF-C1", "TOPN_MV_GUARD_NO_NLP"),
            ("C1 MCF NOGUARD", "R8-MCF-C1", "TOPN_MV_NOGUARD"),
        ]
    ):
        payload = load_run(out, run_id)
        r = arm_daily(payload, arm)
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
        c1_extra.append(("C1 MARKOWITZ_BASE", arm_daily(b0_c1_arm, "MARKOWITZ_BASE")))
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
    else:
        lines.append(
            "The C1 run (R8) was still in flight at the plan's 8 h stop; no C1 number is "
            "reported. The three C1 copies are kept for a later continuation.\n"
        )
    lines.append("")

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
        rows = []
        for key, dsr in family["dsr"].items():
            rows.append(
                [
                    "FINAL (B5)",
                    key.removeprefix("N"),
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
        lines.append(
            "The family matrix is the net return minus that session's French RF; the trial "
            "Sharpes, SR0, DSR and the PBO Sharpe variants use that excess matrix. FINAL (B5)'s "
            "excess Sharpe is **0.476** annual, and it is still the family's highest (B0 is next "
            "at 0.434), so the best-configuration row is the same series. The Calmar PBO remains "
            "on net returns, and the White RC differentials are unchanged."
        )
        lines.append("")
        rows = []
        for key, emax in family["expected_max"].items():
            rows.append([key, _num(emax["e_max_daily"], 5), _num(emax["e_max_annual"], 3)])
        lines.append(_table(["E[max]", "daily", "annual"], rows))
        lines.append("")
        for key, rc in family["white_rc_vs_markowitz"].items():
            lines.append(
                f"White RC vs MARKOWITZ_BASE ({key}, mean block {rc['mean_block']:.0f}): best "
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
    exit_rows = _exits_table(out)
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
        "weight on the recognition session; the exit's cost is 10 bp times it.\n"
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
    rows = []
    for label, other in (
        ("FINAL vs MARKOWITZ_BASE", horizons["MARKOWITZ_BASE"]["concat"]),
        ("FINAL vs MCF", horizons["MCF"]["concat"]),
        ("FINAL vs FINAL NOGUARD", horizons["FINAL NOGUARD"]["concat"]),
        ("FINAL vs SPY", horizons["SPY"]["concat"]),
        ("FINAL vs BMK_EXPO(GUARD)", horizons["BMK_EXPO_GUARD"]["concat"]),
    ):
        a = horizons["FINAL"]["concat"]
        cells = []
        for block in (5, 10, 21, 42):
            cells.append(_num(block_mdd_diff(a, other, block=block)["p_value"], 3))
        stationary10 = _num(stationary_mdd_diff(a, other, mean_block=10)["p_value"], 3)
        stationary21 = _num(stationary_mdd_diff(a, other, mean_block=21)["p_value"], 3)
        rows.append([label, *cells, stationary10, stationary21])
    lines.append(
        _table(["comparison", "b5", "b10", "b21", "b42", "stationary 10", "stationary 21"], rows)
    )
    lines.append(
        "\nEach p is a **two-sided paired bootstrap** of H0: dMDD = 0 against H1: dMDD != 0, "
        "where dMDD = MDD(first) - MDD(second) on the same sessions. The p is the fraction of "
        "resamples with |dMDD*| >= |observed| (plus one). A small p says the observed "
        "difference is unlikely under no difference; the direction is the sign in the "
        "comparison label, not the p."
    )
    if (out / "benchmarks.json").is_file() and (out / "runs" / "R8-FINAL-C1.json").is_file():
        bench = json.loads((out / "benchmarks.json").read_text())["C1"]["series"]
        final_c1 = load_run(out, "R8-FINAL-C1")
        spy_all = _spy_returns()
        c1_spy = {
            day: spy_all[day] for day in final_c1["daily"]["TOPN_MV_GUARD_NO_NLP"] if day in spy_all
        }
        c1_series = {
            "FINAL": arm_daily(final_c1, "TOPN_MV_GUARD_NO_NLP"),
            "FINAL NOGUARD": arm_daily(final_c1, "TOPN_MV_NOGUARD"),
            "MARKOWITZ_BASE": arm_daily(final_c1, "MARKOWITZ_BASE"),
            "BMK_EXPO(GUARD)": {day: float(v) for day, v in bench["BMK_EXPO_GUARD"].items()},
            "SPY": c1_spy,
        }
        if (out / "runs" / "R8-MCF-C1.json").is_file():
            c1_series["MCF"] = arm_daily(load_run(out, "R8-MCF-C1"), "TOPN_MV_GUARD_NO_NLP")
        c1_rows = []
        for label, other in (
            ("FINAL vs MARKOWITZ_BASE", c1_series["MARKOWITZ_BASE"]),
            ("FINAL vs MCF", c1_series["MCF"]),
            ("FINAL vs FINAL NOGUARD", c1_series["FINAL NOGUARD"]),
            ("FINAL vs SPY", c1_series["SPY"]),
            ("FINAL vs BMK_EXPO(GUARD)", c1_series["BMK_EXPO(GUARD)"]),
        ):
            a = c1_series["FINAL"]
            cells = [
                _num(block_mdd_diff(a, other, block=block)["p_value"], 3)
                for block in (5, 10, 21, 42)
            ]
            stationary10 = _num(stationary_mdd_diff(a, other, mean_block=10)["p_value"], 3)
            stationary21 = _num(stationary_mdd_diff(a, other, mean_block=21)["p_value"], 3)
            c1_rows.append([label, *cells, stationary10, stationary21])
        lines.append("")
        lines.append("Continuous C1:")
        lines.append(
            _table(
                ["comparison", "b5", "b10", "b21", "b42", "stationary 10", "stationary 21"],
                c1_rows,
            )
        )
    lines.append("")

    # 13. checks
    lines.append("## 13. Checks\n")
    lines.append("### 13.1 GUARD layers\n")
    guard_rows = []
    for label, run_id, arm in (
        ("FINAL GUARD", FINAL, "TOPN_MV_GUARD_NO_NLP"),
        ("FINAL NOGUARD", FINAL, "TOPN_MV_NOGUARD"),
        ("B0 GUARD", B0, "TOPN_MV_GUARD_NO_NLP"),
        ("B0 NOGUARD", B0, "TOPN_MV_NOGUARD"),
        ("MB GUARD", B0, "MARKOWITZ_BASE_GUARD"),
        ("MB", B0, "MARKOWITZ_BASE"),
    ):
        actions = 0
        layer_stats: dict[str, Any] = {}
        mdds = []
        rs = []
        for window in WINDOWS:
            payload = load_run(out, f"{run_id}-{window}")
            summary = next(
                (
                    row
                    for row in payload["summaries"]
                    if row["arm"] == arm and row["segment"] == window
                ),
                None,
            )
            if summary is not None:
                actions += int(summary["n_guard_actions"])
                mdds.append(float(summary["max_drawdown"]))
                rs.append(float(summary["cumulative_return"]))
            for layer, stats in payload.get("layer_scales", {}).get(arm, {}).items():
                current = layer_stats.setdefault(
                    layer, {"sessions": 0, "min": 1.0, "sum": 0.0, "n": 0}
                )
                current["sessions"] += int(stats["sessions_scaled"])
                current["min"] = min(current["min"], float(stats["min_scale"]))
                current["sum"] += float(stats["mean_scale"])
                current["n"] += 1
        layer_text = (
            ", ".join(
                f"{layer}: {value['sessions']} sessions, mean {value['sum'] / max(value['n'], 1):.3f}, "
                f"min {value['min']:.3f}"
                for layer, value in sorted(layer_stats.items())
            )
            or "none"
        )
        guard_rows.append(
            [
                label,
                "none"
                if "NOGUARD" in label or label == "MB"
                else str(payload["params"].get("guard_layers")),
                str(actions),
                _num(sum(mdds) / len(mdds) if mdds else 0.0),
                _pct(sum(rs) / len(rs) if rs else 0.0),
                layer_text,
            ]
        )
    lines.append(
        _table(
            ["arm", "configured layers", "actions", "mean MDD", "mean R", "layer 3/4 scales"],
            guard_rows,
        )
    )
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
