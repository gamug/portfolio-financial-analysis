"""Generate docs/backtest_final_round_results.md (plan §4) from the run JSONs.

    uv run python scripts/backtest_final_export.py --runs <dir> --out <dir> --docs <path>

Read-only over the run JSONs; aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from cycle.backtest import selection as sel
from cycle.backtest import stats

WINDOWS = sel.WINDOWS
ARMS = ("MARKOWITZ_BASE", "MARKOWITZ_BASE_GUARD", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP")
GUARD = "TOPN_MV_GUARD_NO_NLP"
SEGMENTS = ("P1", "P2", "P2a", "P2b", "P3", "H1", "H2", "H3")
STAGE_A = ("Z0", "Z1", "Z2", "Z3", "Z4")
STAGE_B = ("B0", "B1", "B2", "B3", "B4", "B5", "B6")
STAGE_C = ("FINAL",)
STAGE_D = ("FINAL_D20",)
CONTROL = {"A": "Z0", "B": "B0", "C": "B0", "D": "FINAL"}


def _load(runs: Path) -> dict[str, dict[str, dict[str, Any]]]:
    configs = STAGE_A + STAGE_B + STAGE_C + STAGE_D
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for config in configs:
        out[config] = {}
        for window in WINDOWS:
            path = runs / f"{config}_{window}.json"
            if path.is_file():
                out[config][window] = json.loads(path.read_text())
        if not out[config]:
            raise SystemExit(f"no runs for {config} under {runs}")
    return out


def _summary(run: dict[str, Any], segment: str, arm: str) -> dict[str, Any] | None:
    for row in run["summaries"]:
        if str(row["segment"]) == segment and row["arm"] == arm:
            return dict(row)
    return None


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


def _stage(config: str) -> str:
    if config in STAGE_A:
        return "A"
    if config in STAGE_B:
        return "B"
    if config in STAGE_C:
        return "C"
    return "D"


def _metrics(  # noqa: C901, PLR0912 - one linear aggregation over the six windows
    runs: dict[str, dict[str, dict[str, Any]]], config: str, control: str
) -> dict[str, Any]:
    mdd: list[float] = []
    cumulative: list[float] = []
    per_window: dict[str, Any] = {}
    bp = {"entries": 0, "stop_outs": 0, "bp3": 0, "bp4": 0, "fallbacks": 0, "missing_bars": 0}
    sessions_out: list[float] = []
    layers: dict[str, dict[str, Any]] = {
        "layer3": {"sessions_scaled": 0, "mean_sum": 0.0, "min_scale": None},
        "layer4": {"sessions_scaled": 0, "mean_sum": 0.0, "min_scale": None},
    }
    turnover = cost = 0.0
    names: list[int] = []
    max_weight: list[float] = []
    projected = decisions = 0
    for window in WINDOWS:
        run = runs[config][window]
        row = _summary(run, window, GUARD)
        if row is None:
            raise SystemExit(f"{config}_{window}: no {GUARD} summary")
        mdd.append(float(row["max_drawdown"]))
        cumulative.append(float(row["cumulative_return"]))
        per_window[window] = {
            "max_drawdown": float(row["max_drawdown"]),
            "cumulative_return": float(row["cumulative_return"]),
        }
        report = run["params"].get("bp") or {}
        for key in bp:
            bp[key] += int(report.get(key, 0) or 0)
        if report.get("mean_sessions_out") is not None:
            sessions_out.append(float(report["mean_sessions_out"]))
        for layer in ("layer3", "layer4"):
            value = (run.get("layer_scales") or {}).get(GUARD, {}).get(layer)
            if value:
                layers[layer]["sessions_scaled"] += int(value["sessions_scaled"])
                layers[layer]["mean_sum"] += float(value["mean_scale"]) * int(
                    value["sessions_scaled"]
                )
                minimum = float(value["min_scale"])
                if layers[layer]["min_scale"] is None or minimum < layers[layer]["min_scale"]:
                    layers[layer]["min_scale"] = minimum
        turnover += float(row["total_turnover"])
        cost += float(row["total_cost_bps"])
        for record in run["params"].get("weighting") or []:
            decisions += 1
            if record.get("names") is not None:
                names.append(int(record["names"]))
            if record.get("max_weight") is not None:
                max_weight.append(float(record["max_weight"]))
            if record.get("projected"):
                projected += 1
        if not run["params"].get("weighting"):
            held = (run.get("equity") or {}).get(GUARD, {}).get(window)
            if held:
                names.extend([int(held["min"]), int(held["max"])])
    control_mdd: list[float] = []
    for window in WINDOWS:
        control_row = _summary(runs[control][window], window, GUARD)
        if control_row is None:
            raise SystemExit(f"{control}_{window}: no {GUARD} summary")
        control_mdd.append(float(control_row["max_drawdown"]))
    mean_mdd = sum(mdd) / len(mdd)
    mean_cumulative = sum(cumulative) / len(cumulative)
    return {
        "configuration": config,
        "stage": _stage(config),
        "guard_layers": runs[config][WINDOWS[0]]["params"].get("guard_layers"),
        "vol_target": runs[config][WINDOWS[0]]["params"].get("vol_target"),
        "satellite_weighting": runs[config][WINDOWS[0]]["params"].get("satellite_weighting"),
        "cost_bps": runs[config][WINDOWS[0]]["params"].get("cost_bps"),
        "mean_mdd": mean_mdd,
        "mean_cumulative": mean_cumulative,
        "R": mean_cumulative / mean_mdd if mean_mdd else None,
        "k_lower_mdd_than_control": sum(
            1 for value, base in zip(mdd, control_mdd, strict=True) if value < base
        ),
        "bp": bp,
        "mean_sessions_out": (sum(sessions_out) / len(sessions_out) if sessions_out else None),
        "layers": {
            layer: {
                "sessions_scaled": value["sessions_scaled"],
                "mean_scale": (
                    value["mean_sum"] / value["sessions_scaled"]
                    if value["sessions_scaled"]
                    else None
                ),
                "min_scale": value["min_scale"],
            }
            for layer, value in layers.items()
        },
        "guard_turnover": turnover,
        "guard_cost_bps": cost,
        "weighting": {
            "names": (min(names), max(names)) if names else None,
            "max_weight": max(max_weight) if max_weight else None,
            "projected": projected,
            "decisions": decisions,
        },
        "per_window": per_window,
    }


def _cross_tests(
    runs: dict[str, dict[str, dict[str, Any]]], configs: list[str], control: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for config in configs:
        for window in WINDOWS:
            if config not in runs or window not in runs[config]:
                continue
            a = runs[config][window]["daily"][GUARD]
            b = runs[control][window]["daily"][GUARD]
            dates = sorted(set(a) & set(b))
            ra = [a[day] for day in dates]
            rb = [b[day] for day in dates]
            hac_t, hac_p = stats.hac_mean_diff(ra, rb)
            mdd_diff, mdd_p = stats.bootstrap_mdd_diff(ra, rb)
            out.append(
                {
                    "configuration": config,
                    "window": window,
                    "n_obs": len(dates),
                    "hac_mean_diff": {"statistic": hac_t, "p_value": hac_p},
                    "bootstrap_mdd": {"statistic": mdd_diff, "p_value": mdd_p},
                }
            )
    return out


def _grid_table(runs: dict[str, dict[str, dict[str, Any]]]) -> list[str]:
    lines = [
        "| config | window | segment | arm | cumulative | ann. return | ann. vol | Sharpe | MDD | turnover | cost bps | rebal. | GUARD actions | names held (min/max) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for config in STAGE_A + STAGE_B + STAGE_C + STAGE_D:
        for window in WINDOWS:
            if config not in runs or window not in runs[config]:
                continue
            run = runs[config][window]
            for segment in SEGMENTS:
                rows = {
                    row["arm"]: row for row in run["summaries"] if str(row["segment"]) == segment
                }
                if not rows:
                    continue
                for arm in ARMS:
                    if arm not in rows:
                        continue
                    row = rows[arm]
                    held = run.get("equity", {}).get(arm, {}).get(segment, {})
                    lines.append(
                        f"| {config} | {window} | {segment} | {arm} | "
                        f"{row['cumulative_return']:.4f} | {row['annualized_return']:.4f} | "
                        f"{row['annualized_vol']:.4f} | {row['ex_post_sharpe']:.3f} | "
                        f"{row['max_drawdown']:.4f} | {row['total_turnover']:.4f} | "
                        f"{row['total_cost_bps']:.1f} | {row['n_rebalances']} | "
                        f"{row['n_guard_actions']} | "
                        f"{held.get('min', '')}/{held.get('max', '')} |"
                    )
    return lines


def _config_table_md(rows: list[dict[str, Any]], control: str) -> list[str]:
    lines = [
        f"Control: {control}. `k` counts the windows whose MDD is strictly lower than the control's.",
        "",
        "| config | stage | layers | vol target | weighting | mean MDD | mean cumulative | R | k | entries | stop-outs | mean sessions out | BP3 | BP4 | fallbacks | missing bars | layer-3 sessions (mean/min scale) | layer-4 sessions (mean/min scale) | GUARD turnover | GUARD cost bps | names | max weight | projected |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        layer3 = row["layers"]["layer3"]
        layer4 = row["layers"]["layer4"]
        weighting = row["weighting"]
        names = (
            "-" if not weighting["names"] else f"{weighting['names'][0]}-{weighting['names'][1]}"
        )
        lines.append(
            f"| {row['configuration']} | {row['stage']} | {row['guard_layers']} | "
            f"{row['vol_target']} | {row['satellite_weighting']} | {row['mean_mdd']:.6f} | "
            f"{row['mean_cumulative']:.6f} | {row['R']:.4f} | "
            f"{row['k_lower_mdd_than_control']} | {row['bp']['entries']} | "
            f"{row['bp']['stop_outs']} | "
            f"{'-' if row['mean_sessions_out'] is None else f'{row["mean_sessions_out"]:.2f}'} | "
            f"{row['bp']['bp3']} | {row['bp']['bp4']} | {row['bp']['fallbacks']} | "
            f"{row['bp']['missing_bars']} | {layer3['sessions_scaled']} "
            f"({'-' if layer3['mean_scale'] is None else f'{layer3["mean_scale"]:.3f}'} / "
            f"{'-' if layer3['min_scale'] is None else f'{layer3["min_scale"]:.3f}'}) | "
            f"{layer4['sessions_scaled']} "
            f"({'-' if layer4['mean_scale'] is None else f'{layer4["mean_scale"]:.3f}'} / "
            f"{'-' if layer4['min_scale'] is None else f'{layer4["min_scale"]:.3f}'}) | "
            f"{row['guard_turnover']:.2f} | {row['guard_cost_bps']:.1f} | {names} | "
            f"{'-' if weighting['max_weight'] is None else f'{weighting["max_weight"]:.4f}'} | "
            f"{weighting['projected']}/{weighting['decisions']} |"
        )
    return lines


def _tests_md(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| config | window | n | HAC stat vs control | HAC p | ΔMDD vs control | bootstrap p | HAC p vs MARKOWITZ | ΔMDD vs MARKOWITZ | bootstrap p |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['configuration']} | {row['window']} | {row['n_obs']} | "
            f"{row['hac_mean_diff']['statistic']:.3f} | {row['hac_mean_diff']['p_value']:.3f} | "
            f"{row['bootstrap_mdd']['statistic']:+.4f} | {row['bootstrap_mdd']['p_value']:.3f} | "
            f"{row.get('markowitz_hac_p', float('nan')):.3f} | "
            f"{row.get('markowitz_mdd_diff', float('nan')):+.4f} | "
            f"{row.get('markowitz_mdd_p', float('nan')):.3f} |"
        )
    return lines


def _chained_cagr(runs: dict[str, dict[str, dict[str, Any]]], config: str) -> tuple[float, float]:
    returns: list[float] = []
    for window in WINDOWS:
        daily = runs[config][window]["daily"][GUARD]
        returns.extend(daily[day] for day in sorted(daily))
    array = np.asarray(returns, dtype=float)
    equity = np.concatenate([[1.0], np.cumprod(1.0 + array)])
    cagr = float(equity[-1] ** (252.0 / array.size) - 1.0)
    sharpe = float(np.mean(array) / np.std(array, ddof=1) * (252.0**0.5))
    return cagr, sharpe


def _identity_md(verify: Path) -> list[str]:
    lines = [
        "| control | window | DB summaries | DB tests | DB weights (max abs) | DB actions (max abs) | JSON summaries | JSON tests | JSON daily |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for path in sorted(verify.glob("identity_*.json")):
        data = json.loads(path.read_text())
        db = data["db_vs_db"]
        js = data["json_vs_stored"]
        name = path.stem.replace("identity_", "")
        lines.append(
            f"| {name} | {data.get('window', '')} | {db['summaries']['equal']} | "
            f"{db['tests']['equal']} | {db['weights']['equal']} ({db['weights']['max_abs_error']}) | "
            f"{db['guard_actions']['equal']} ({db['guard_actions']['max_abs_error']}) | "
            f"{js['summaries']['equal']} | {js['tests']['equal']} | {js['daily']['equal']} |"
        )
    return lines


def main() -> int:  # noqa: C901 - one linear report builder
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--docs", required=True)
    args = parser.parse_args()
    runs_dir = Path(args.runs).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    runs = _load(runs_dir)
    rows = []
    for config in STAGE_A:
        rows.append(_metrics(runs, config, "Z0"))
    for config in STAGE_B:
        rows.append(_metrics(runs, config, "B0"))
    for config in STAGE_C:
        rows.append(_metrics(runs, config, "B0"))
    for config in STAGE_D:
        rows.append(_metrics(runs, config, "FINAL"))
    stage_a_rows = [row for row in rows if row["stage"] == "A"]
    stage_b_rows = [row for row in rows if row["stage"] == "B"]
    a_star = json.loads((out_dir / "a_star.json").read_text())
    final = json.loads((out_dir / "final.json").read_text())
    cross_a = _cross_tests(runs, [config for config in STAGE_A if config != "Z0"], "Z0")
    cross_b = _cross_tests(runs, [config for config in STAGE_B if config != "B0"], "B0")
    cross_final = _cross_tests(runs, ["FINAL"], "B0")
    for row in cross_a + cross_b + cross_final:
        stored = _test(
            runs[row["configuration"]][row["window"]],
            row["window"],
            GUARD,
            "MARKOWITZ_BASE",
            "hac_mean_diff",
        )
        stored_mdd = _test(
            runs[row["configuration"]][row["window"]],
            row["window"],
            GUARD,
            "MARKOWITZ_BASE",
            "bootstrap_mdd",
        )
        if stored:
            row["markowitz_hac_p"] = stored["p_value"]
        if stored_mdd:
            row["markowitz_mdd_diff"] = stored_mdd["statistic"]
            row["markowitz_mdd_p"] = stored_mdd["p_value"]
    lines = [
        "# Final round — results (generated)",
        "",
        "Generated by `scripts/backtest_final_export.py` from the run copies; do not edit by hand.",
        "Aggregates only: no per-asset value, no weight series, no SPY price.",
        "",
        "## 0. Method and declarations",
        "",
        "- Plan: `docs/backtest_final_round_plan.md` (first commit), §11 readings.",
        "- Fixed: LLM scores `scored_llm.db` sha256 `2187c36b...`, blend B0, tilt T0, frozen GUARD,",
        "  pool 60, SPY core 0.5, trend filter on with exit 0.5, Basing Points bp4, 12 warm-up steps.",
        "- Every result is in-sample: all six windows were seen before this round.",
        f"- A* = **{a_star['config']}** ({a_star['guard_layers']}, {a_star['vol_target']}): R "
        f"{a_star['R']:.4f}, mean MDD {a_star['mean_mdd']:.6f}.",
        f"- FINAL = **{final['config']}** ({final['satellite_weighting']}): R {final['R']:.4f}, "
        f"mean MDD {final['mean_mdd']:.6f}.",
        "- Declared configurations tried after v1: **55** = 45 (after round 4) + 4 (Z1-Z4) + 6 (B1-B6).",
        "",
        "## 1. Stage A — GUARD layers",
        "",
        *_config_table_md(stage_a_rows, "Z0"),
        "",
        "## 2. Stage B — satellite weighting on A*",
        "",
        *_config_table_md(stage_b_rows, "B0"),
        "",
        "## 3. FINAL",
        "",
        *_config_table_md([row for row in rows if row["stage"] in ("C", "D")], "FINAL"),
        "",
        "## 4. Tests per window (TOPN_MV_GUARD_NO_NLP)",
        "",
        "### 4(i) stage A vs Z0 and stage B/FINAL vs B0",
        "",
        *_tests_md(cross_a + cross_b + cross_final),
        "",
        "## 5. Every configuration x window x arm",
        "",
        *_grid_table(runs),
        "",
        "## 6. Stage D — costs",
        "",
        "| configuration | cost bps | mean MDD | mean cumulative | R | full-period Sharpe | full-period CAGR |",
        "|---|---|---|---|---|---|---|",
    ]
    for config in ("FINAL", "FINAL_D20"):
        row = next(item for item in rows if item["configuration"] == config)
        cagr, sharpe = _chained_cagr(runs, config)
        lines.append(
            f"| {config} | {row['cost_bps']:.0f} | {row['mean_mdd']:.6f} | "
            f"{row['mean_cumulative']:.6f} | {row['R']:.4f} | {sharpe:.3f} | {cagr * 100:.2f}% |"
        )
    lines += [
        "",
        "## 7. Identity controls",
        "",
        "Z0 must equal round-4 Y2, and B0 P1 must equal A* P1.",
        "",
        *_identity_md(out_dir / "verify"),
        "",
        "## 8. Data checks (round-4 §5, amended; counts only)",
        "",
    ]
    data_path = out_dir / "data_report.json"
    if data_path.is_file():
        data = json.loads(data_path.read_text())
        lines += [
            "| window | tracking-span pairs | missing | jumps without a SPLIT row |",
            "|---|---|---|---|",
        ]
        for row in data["missing_bars"]:
            lines.append(
                f"| {row['window']} | {row['pairs']} | {row['missing']} | {row['jumps']} |"
            )
        splits = data["known_splits"]
        lines += [
            "",
            f"Known splits: {splits['present']}/{splits['checked']} present; missing "
            f"{splits['missing'] or 'none'}.",
            "",
        ]
        if any(int(row["jumps"]) for row in data["missing_bars"]):
            lines += [
                "The non-zero jump count is a declared genuine market move, not a missing split:",
                "P2 = 1 name-session (CNC, 2025-07-02), held by B5's minimum-CVaR satellite and",
                "never tracked by Y2. There is no SPLIT row on that date (CNC's only split is",
                "2019-02-07), no corporate action, the level persists in the following sessions,",
                "and the date matches the issuer's public withdrawal of its 2025 guidance after",
                "the previous close (Fierce Healthcare, 2025-07-02). No run, resolution or",
                "selection changes. See the plan's §7 entry.",
                "",
            ]
    else:
        lines += ["Data report not found.", ""]
    markdown = "\n".join(lines) + "\n"
    Path(args.docs).expanduser().resolve().write_text(markdown)
    (out_dir / "grid.md").write_text(markdown)
    (out_dir / "grid.json").write_text(
        json.dumps(
            {
                "configurations": {
                    config: {
                        window: {
                            "params": runs[config][window]["params"],
                            "summaries": runs[config][window]["summaries"],
                            "tests": runs[config][window]["tests"],
                            "equity": runs[config][window].get("equity"),
                            "layer_scales": runs[config][window].get("layer_scales"),
                        }
                        for window in runs[config]
                    }
                    for config in runs
                },
                "per_configuration": rows,
                "cross_tests": {"stage_a": cross_a, "stage_b": cross_b, "final": cross_final},
                "a_star": a_star,
                "final": final,
            },
            indent=1,
        )
        + "\n"
    )
    (out_dir / "choice.json").write_text(
        json.dumps({"a_star": a_star, "final": final, "rows": rows}, indent=1) + "\n"
    )
    print(json.dumps({"docs": str(args.docs), "configs": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
