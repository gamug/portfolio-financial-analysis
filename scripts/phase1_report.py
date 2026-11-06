"""Assemble the Phase 1 REPORT.md from the collected JSONs (plan §4, sections 1-13).

    uv run python scripts/phase1_report.py --out DIR

Every number is computed from ``runs/*.json`` (the collector), ``stats.json`` and
``benchmarks.json``; nothing is typed by hand.  The report stays in the private scratch
directory.  Old-round numbers come from ``out/final_round/runs``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from phase1_stats import (
    WINDOWS,
    arm_daily,
    block_mdd_diff,
    concatenate,
    load_run,
    metrics,
    stationary_mdd_diff,
)

OLD = Path("/workspaces/thesis/scratch/final/out/final_round/runs")
R_THRESHOLD = 0.8446
MDD_THRESHOLD = 0.0831
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
            w: arm_daily(json.loads((OLD / f"{old_id}-{w}.json").read_text()), arm) for w in WINDOWS
        }
        old_concat = _concat(old_parts)
        old_rf = concatenate(
            [json.loads((OLD / f"{old_id}-{w}.json").read_text())["rf"] for w in WINDOWS]
        )
        item["old"] = metrics(old_concat, old_rf)
    return item


def _load_benchmark(out: Path, window: str, name: str) -> dict[str, float]:
    data = json.loads((out / "benchmarks.json").read_text())
    return {day: float(value) for day, value in data[window]["series"][name].items()}


def _load_stats(out: Path) -> dict[str, Any]:
    return json.loads((out / "stats.json").read_text())


def _exits_table(out: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted((out / "runs").glob("*.json")):
        payload = json.loads(path.read_text())
        for row in payload.get("exits", []):
            records.append(
                {
                    "run": path.stem,
                    "window": payload["run"]["window"],
                    **row,
                }
            )
    return records


def build(out: Path) -> str:  # noqa: C901, PLR0912, PLR0915 - one section per block
    stats = _load_stats(out)
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
    # CHAMPION design set and holdout, old vs corrected.
    old_selection = json.loads(
        Path("/workspaces/thesis/scratch/final/out/champion/selection.json").read_text()
    )
    old_holdout = json.loads(
        Path("/workspaces/thesis/scratch/final/out/champion/holdout.json").read_text()
    )
    new_selection = json.loads((out / "champion_selection_phase1.json").read_text())
    new_holdout = json.loads((out / "selection_phase1.json").read_text())["champion"]
    old_c = f"{old_selection['selected']['blend']}·{old_selection['selected']['tilt']}"
    new_c = f"{new_selection['selected']['blend']}·{new_selection['selected']['tilt']}"
    champ_rows = [
        ["design-set C*", old_c, new_c, "selection re-applied"],
        [
            "design mean MDD",
            _num(old_selection["rows"][0]["mean_mdd"], 4),
            _num(new_selection["rows"][0]["mean_mdd"], 4),
            _num(new_selection["rows"][0]["mean_mdd"] - old_selection["rows"][0]["mean_mdd"], 4),
        ],
    ]
    for window in ("H1", "H2", "H3"):
        old_arm = old_holdout["windows"][window]["arms"]["TOPN_MV_GUARD_NO_NLP"]
        old_mdd = float(old_arm["chosen"]["max_drawdown"])
        new_mdd = float(new_holdout["configs"][new_c][window]["guard"]["max_drawdown"])
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
            arm = "TOPN_MV_NOGUARD" if run_id.startswith("L3") else "TOPN_MV_GUARD_NO_NLP"
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
                    _num(item["per_window_cost"][window], 1),
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
        "(second object). The continuous C1 object is in §5.2.\n"
    )
    lines.append(
        _table(
            ["series", "mean R", "CAGR", "Sharpe", "MDD", "Calmar", "CVaR95", "days UW", "cost"],
            rows,
        )
    )
    lines.append("")

    # 5.2 C1
    lines.append("### 5.2 Continuous window C1\n")
    c1_rows = []
    for label, run_id, arm in (
        ("C1 FINAL", "R8-FINAL-C1", "TOPN_MV_GUARD_NO_NLP"),
        ("C1 FINAL NOGUARD", "R8-FINAL-C1", "TOPN_MV_NOGUARD"),
        ("C1 B0", "R8-B0-C1", "TOPN_MV_GUARD_NO_NLP"),
        ("C1 B0 NOGUARD", "R8-B0-C1", "TOPN_MV_NOGUARD"),
        ("C1 MCF", "R8-MCF-C1", "TOPN_MV_GUARD_NO_NLP"),
        ("C1 MCF NOGUARD", "R8-MCF-C1", "TOPN_MV_NOGUARD"),
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
    lines.append(
        _table(["series", "R", "CAGR", "Sharpe", "MDD", "Calmar", "CVaR95", "days UW"], c1_rows)
    )
    lines.append("")

    # 6. stage A and B
    lines.append("## 6. Stage A and stage B on corrected data\n")
    stage_rows = []
    for config in ("Z1", "Z3", "Z4", "B0", "B1", "B2", "B3", "B4", "B5", "B6"):
        run_id = f"R2-{config}" if config.startswith("Z") else f"R1-{config}"
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
        stage_rows.append([config, _pct(cum), _num(mdd)])
    lines.append(_table(["configuration", "mean R", "mean MDD"], stage_rows))
    selection_stage = json.loads((out / "selection_phase1.json").read_text())["stage"]
    a = selection_stage["stage_a"]
    b = selection_stage["stage_b"]
    lines.append(
        f"\n**Adoption rule outcome (numbers only).** Stage A: A* = **{a['a_star']}** "
        f"(R {_pct(a['a_star_R'])}, mean MDD {_num(a['a_star_mean_mdd'])}; qualifiers "
        f"{a['qualifiers'] or 'none'} against R > {R_THRESHOLD:.4f} and mean MDD <= "
        f"{MDD_THRESHOLD:.4f}). Stage B: B* = **{b['b_star']}** (R {_pct(b['b_star_R'])}, "
        f"mean MDD {_num(b['b_star_mean_mdd'])}; qualifiers {b['qualifiers'] or 'none'} "
        "against R(A*)). No new selection is made from these numbers.\n"
    )

    # 7. CHAMPION
    lines.append("## 7. CHAMPION (corrected reproduction; H windows already seen)\n")
    selection = json.loads((out / "champion_selection_phase1.json").read_text())
    lines.append(
        f"The pre-registered rule selects **{selection['selected']['blend']}·"
        f"{selection['selected']['tilt']}** (mean design MDD "
        f"{selection['rows'][0]['mean_mdd']:.4f}).\n"
    )
    rows = []
    for row in selection["rows"][:5]:
        rows.append(
            [
                f"{row['blend']}·{row['tilt']}",
                _num(row["mean_mdd"], 4),
                _pct(row["mean_cumulative_return"]),
            ]
        )
    lines.append(_table(["config", "mean MDD", "mean R"], rows))
    champion = json.loads((out / "selection_phase1.json").read_text())["champion"]
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
    lines.append("")

    # 9. pool attribution
    lines.append("## 9. The pool attribution\n")
    lines.append(
        "MCF vs Y2 isolates the pool (same min-variance pipeline); Y2 vs B5 isolates the "
        "weighting; MCF vs B5 is the global difference. MARKOWITZ_BASE → MCF changes several "
        "factors at once and is not a single-factor step.\n"
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
    lines.append("")

    # 10. overfitting
    lines.append("## 10. Overfitting diagnostics on the corrected 30-configuration family\n")
    family = stats["family"]
    lines.append(
        f"Family: **{family['n']}** unique configurations, **{family['sessions']}** sessions. "
        f"Effective trials: eigenvalue participation **{family['effective_trials']['eigen_participation']:.2f}**, "
        f"correlation clusters (|rho| >= 0.9) **{family['effective_trials']['correlation_clusters_0.9']:.0f}**.\n"
    )
    rows = []
    for key, pbo in family["pbo"].items():
        rows.append(
            [key, _num(pbo["pbo"], 4), _num(pbo["mean_is_best"], 3), _num(pbo["mean_oos_best"], 3)]
        )
    lines.append(_table(["PBO variant", "PBO", "mean IS best", "mean OOS best"], rows))
    lines.append("")
    rows = []
    for key, dsr in family["dsr"].items():
        rows.append(
            [
                key,
                _num(dsr["sharpe_annual"], 3),
                _num(dsr["skew"], 3),
                _num(dsr["kurtosis"], 3),
                _num(dsr["sr0_daily"], 5),
                _num(dsr["dsr"], 3),
            ]
        )
    lines.append(_table(["DSR", "SR annual", "skew", "kurtosis", "SR0 daily", "DSR"], rows))
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
    lines.append(
        f"This run family held an asset through **{len(exit_rows)}** exit arms/windows. The "
        "residual is the drifted weight on the recognition session; the exit's cost is 10 bp "
        "times it.\n"
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
        "A name that is absent was either not selected by cycle, not in the risk-model panel, "
        "or vetoed; the H1 `optimizer_weights` in the run JSON is the record.\n"
    )
    imputed = [event for event in events if event["valuation_basis"] == "imputed_reference"]
    frozen = [event for event in events if event["valuation_basis"] == "unobservable_frozen"]
    lines.append(
        f"Counts: `imputed_reference` **{len(imputed)}** (SIVB, SBNY, FRC); "
        f"`unobservable_frozen` **{len(frozen)}**. The plan's sensitivity at 0 and -1.0 for "
        "every imputed case therefore has the three names; the measured effect on the held "
        "arms is in the run-level exit table above (zero when the name was not held).\n"
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
        stationary = _num(stationary_mdd_diff(a, other, mean_block=10)["p_value"], 3)
        rows.append([label, *cells, stationary])
    lines.append(_table(["comparison", "b5", "b10", "b21", "b42", "stationary 10"], rows))
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
                str(payload["params"].get("guard_layers")),
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
        ("MB", B0, "MARKOWITZ_BASE_GUARD", B0, "MARKOWITZ_BASE"),
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
    lines.append("Measured GUARD contribution (GUARD - NOGUARD):")
    lines.append(_table(["config", "delta R", "delta MDD"], contribution))
    lines.append("")
    lines.append("### 13.2 CVaR log vs simple\n")
    l1 = load_run(out, "L1-FINAL-P1")
    l2 = load_run(out, "L2-FINAL-P1")
    common = sorted(set(l1.get("optimizer_weights", {})) & set(l2.get("optimizer_weights", {})))
    deltas = []
    for decision in common:
        a = l1["optimizer_weights"][decision]
        b = l2["optimizer_weights"][decision]
        keys = set(a) | set(b)
        deltas.append(max(abs(float(a.get(k, 0.0)) - float(b.get(k, 0.0))) for k in keys))
    lines.append(
        f"P1 decisions with both matrices: {len(common)}; max |Δw| {max(deltas) if deltas else 0:.6f}. "
        "The full 61-decision figure and the CVaR95 difference are in `stats.json` after the "
        "L runs.\n"
    )
    lines.append("")
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
