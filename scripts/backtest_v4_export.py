"""Generate the round-4 grid, the adoption table and the results doc (V4 plan §4).

    uv run python scripts/backtest_v4_export.py --runs <dir> --out <dir> --docs <path>

Read-only over the 30 per-run JSONs and the six identity JSONs. Portfolio aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cycle.backtest import stats

WINDOWS = ("P1", "P3", "P2", "H1", "H2", "H3")
SEGMENT_ORDER = ("P1", "P2", "P2a", "P2b", "P3", "H1", "H2", "H3")
#: id -> (stop_rule, satellite_weighting); Y4's stop_rule is read from its run params.
CONFIGS = {
    "Y0": ("kl", "minvar"),
    "Y1": ("bp3", "minvar"),
    "Y2": ("bp4", "minvar"),
    "Y3": ("kl", "rp"),
    "Y4": ("bp*", "rp"),
}
ARMS = (
    "MARKOWITZ_BASE",
    "MARKOWITZ_BASE_GUARD",
    "TOPN_MV_NOGUARD",
    "TOPN_MV_GUARD_NO_NLP",
)
GUARD = "TOPN_MV_GUARD_NO_NLP"
ADOPT_R = 0.8014
ADOPT_MDD = 0.0831
R_TIE = 0.001


def _load(runs: Path) -> dict[str, dict[str, Any]]:
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


def _config_table(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    y0 = {window: _summary(runs[f"Y0_{window}"], window)[GUARD] for window in WINDOWS}
    for config, (stop_rule, weighting) in CONFIGS.items():
        resolved_stop = str(runs["Y4_P1"]["params"]["stop_rule"]) if config == "Y4" else stop_rule
        mdd, cumulative, per_window = [], [], {}
        bp = {
            "entries": 0,
            "stop_outs": 0,
            "bp3": 0,
            "bp4": 0,
            "fallbacks": 0,
            "missing_bars": 0,
        }
        sessions_out: list[float] = []
        rp_names, rp_max_weight, rp_rrc = [], [], []
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            guard = _summary(run, window)[GUARD]
            mdd.append(float(guard["max_drawdown"]))
            cumulative.append(float(guard["cumulative_return"]))
            per_window[window] = {
                "max_drawdown": float(guard["max_drawdown"]),
                "cumulative_return": float(guard["cumulative_return"]),
            }
            report = run["params"].get("bp") or {}
            for key in bp:
                bp[key] += int(report.get(key, 0) or 0)
            if report.get("mean_sessions_out") is not None:
                sessions_out.append(float(report["mean_sessions_out"]))
            for record in run["params"].get("rp") or []:
                rp_names.append(int(record["names"]))
                if record.get("max_weight") is not None:
                    rp_max_weight.append(float(record["max_weight"]))
                if record.get("max_min_rrc") is not None:
                    rp_rrc.append(float(record["max_min_rrc"]))
        mean_mdd = sum(mdd) / len(mdd)
        mean_cum = sum(cumulative) / len(cumulative)
        rows.append(
            {
                "configuration": config,
                "stop_rule": resolved_stop,
                "satellite_weighting": weighting,
                "mean_mdd": mean_mdd,
                "mean_cumulative": mean_cum,
                "R": mean_cum / mean_mdd if mean_mdd else None,
                "k_lower_mdd_than_y0": sum(
                    1
                    for window in WINDOWS
                    if per_window[window]["max_drawdown"] < float(y0[window]["max_drawdown"])
                ),
                "bp": bp,
                "mean_sessions_out": (
                    sum(sessions_out) / len(sessions_out) if sessions_out else None
                ),
                "rp_names": (min(rp_names), max(rp_names)) if rp_names else None,
                "rp_max_weight": max(rp_max_weight) if rp_max_weight else None,
                "rp_max_min_rrc": max(rp_rrc) if rp_rrc else None,
                "per_window": per_window,
            }
        )
    eligible = [
        row
        for row in rows
        if row["configuration"] != "Y0"
        and row["R"] is not None
        and row["R"] > ADOPT_R
        and row["mean_mdd"] <= ADOPT_MDD
    ]
    adopted = None
    if eligible:
        best = max(row["R"] for row in eligible)
        tied = [row for row in eligible if best - row["R"] <= R_TIE]
        adopted = min(tied, key=lambda row: row["mean_mdd"])["configuration"]
    return {
        "rule": f"R > {ADOPT_R} and mean MDD <= {ADOPT_MDD}; highest R among improvements; "
        f"ties within {R_TIE} in R -> lower mean MDD; otherwise X5 stays",
        "declared_configurations_tried_after_v1": 45,
        "qualifying": [row["configuration"] for row in sorted(eligible, key=lambda r: -r["R"])],
        "adopted": adopted,
        "rows": rows,
    }


def _cross_tests(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for config in CONFIGS:
        for window in WINDOWS:
            a = runs[f"{config}_{window}"]["daily"][GUARD]
            b = runs[f"Y0_{window}"]["daily"][GUARD]
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


def _grid_table(runs: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "| config | window | segment | arm | cumulative | ann. return | ann. vol | Sharpe | MDD | turnover | cost bps | rebal. | GUARD actions | names held (min/max) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
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


def _grid_md(  # noqa: C901, PLR0912 - one linear document builder
    runs: dict[str, dict[str, Any]],
    table: dict[str, Any],
    cross: list[dict[str, Any]],
    identity: dict[str, dict[str, Any]],
    defective: dict[str, Any] | None = None,
) -> str:
    identity_ok = all(row["ok"] for row in identity.values())
    adopted = table["adopted"]
    lines = [
        "# System experiments round 4 -- results (generated)",
        "",
        "Generated by `scripts/backtest_v4_export.py` from the 30 run copies; do not edit by",
        "hand. Aggregates only: no per-asset value, no SPY price series, no price level.",
        "",
        "## 0. Method and declarations",
        "",
        "- Plan: `docs/backtest_v4_experiments_plan.md` (first commit), §7 readings and the §5",
        "  amendment (`cfe96c9`).",
        "- Fixed: LLM scores `scored_llm.db` sha256 `2187c36b...`, blend B0, tilt T0, frozen",
        "  GUARD `ee22f352...`, pool 60, SPY core 0.5, trend filter on, exit 0.5, 12 warm-up",
        "  steps, one fresh copy per run. No model call was made.",
        "- **Every result is in-sample**: all six windows were seen before round 4.",
        f"- Y0 identity: {'all six windows reproduce round-3 X5 exactly' if identity_ok else 'FAILED'}",
        "  (summaries, tests, weights and GUARD actions; max |delta| 0.0).",
        "- Declared count: the round-3 ledger (41 distinct configurations with LLM scores) +",
        "  4 new here (Y1-Y4) = **45**.",
        f"- Adoption: {'**' + adopted + '**' if adopted else 'none qualifies; **X5 stays the system configuration** (tried and not adopted).'}",
        "",
        "## 1. Per configuration (TOPN_MV_GUARD_NO_NLP, six windows)",
        "",
        "| config | stop | weighting | mean MDD | mean cumulative | R | k (lower MDD than Y0) | entries | stop-outs | mean sessions out | BP3 | BP4 | fallbacks | missing bars | rp names | rp max weight | rp max/min RRC |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in table["rows"]:
        mark = " **adopted**" if row["configuration"] == adopted else ""
        rp_names = f"{row['rp_names'][0]}-{row['rp_names'][1]}" if row["rp_names"] else "-"
        lines.append(
            f"| {row['configuration']}{mark} | {row['stop_rule']} | "
            f"{row['satellite_weighting']} | {row['mean_mdd']:.6f} | "
            f"{row['mean_cumulative']:.6f} | {row['R']:.4f} | "
            f"{row['k_lower_mdd_than_y0']} | {row['bp']['entries']} | {row['bp']['stop_outs']} | "
            f"{'-' if row['mean_sessions_out'] is None else f'{row["mean_sessions_out"]:.2f}'} | "
            f"{row['bp']['bp3']} | {row['bp']['bp4']} | {row['bp']['fallbacks']} | "
            f"{row['bp']['missing_bars']} | {rp_names} | "
            f"{'-' if row['rp_max_weight'] is None else f'{row["rp_max_weight"]:.4f}'} | "
            f"{'-' if row['rp_max_min_rrc'] is None else f'{row["rp_max_min_rrc"]:.4f}'} |"
        )
    lines += [
        "",
        f"Adoption rule: R > {ADOPT_R} and mean MDD <= {ADOPT_MDD}; among the qualifying "
        f"configurations ({', '.join(table['qualifying']) or 'none'}) the highest R is adopted "
        "(ties within 0.001 in R -> lower mean MDD).",
        "",
        "## 2. Every configuration x window x arm",
        "",
        *_grid_table(runs),
        "",
        "## 3. Tests per window (TOPN_MV_GUARD_NO_NLP)",
        "",
        "### 3(i) vs Y0",
        "",
        "| config | window | n | HAC stat | HAC p | delta MDD | bootstrap p |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in cross:
        lines.append(
            f"| {row['configuration']} | {row['window']} | {row['n_obs']} | "
            f"{row['hac_mean_diff']['statistic']:.3f} | {row['hac_mean_diff']['p_value']:.3f} | "
            f"{row['bootstrap_mdd']['statistic']:+.4f} | "
            f"{row['bootstrap_mdd']['p_value']:.3f} |"
        )
    lines += [
        "",
        "### 3(ii) vs MARKOWITZ_BASE (in-run)",
        "",
        "| config | window | n | HAC stat | HAC p | delta MDD | bootstrap p |",
        "|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            hac = _test(
                runs[f"{config}_{window}"], window, GUARD, "MARKOWITZ_BASE", "hac_mean_diff"
            )
            boot = _test(
                runs[f"{config}_{window}"], window, GUARD, "MARKOWITZ_BASE", "bootstrap_mdd"
            )
            if hac is None or boot is None:
                continue
            lines.append(
                f"| {config} | {window} | {hac['n_obs']} | {hac['statistic']:.3f} | "
                f"{hac['p_value']:.3f} | {boot['statistic']:+.4f} | {boot['p_value']:.3f} |"
            )
    lines += [
        "",
        "## 4. Basing Points per configuration and window",
        "",
        "| config | window | stop rule | stop-outs | mean sessions out | BP3 | BP4 | fallbacks | missing bars | distance cells |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            report = run["params"].get("bp") or {}
            cells = run["params"].get("distance_cells") or {}
            cell_text = ", ".join(f"{cell}:{count}" for cell, count in sorted(cells.items()))
            lines.append(
                f"| {config} | {window} | {run['params']['stop_rule']} | "
                f"{report.get('stop_outs', 0)} | "
                f"{'-' if report.get('mean_sessions_out') is None else f'{report["mean_sessions_out"]:.2f}'} | "
                f"{report.get('bp3', 0)} | {report.get('bp4', 0)} | "
                f"{report.get('fallbacks', 0)} | {report.get('missing_bars', 0)} | "
                f"{cell_text or '-'} |"
            )
    lines += [
        "",
        "## 5. Risk parity per decision (Y3, Y4)",
        "",
        "| config | window | decision | names | max weight | max/min RRC |",
        "|---|---|---|---|---|---|",
    ]
    for config in ("Y3", "Y4"):
        for window in WINDOWS:
            for record in runs[f"{config}_{window}"]["params"].get("rp") or []:
                lines.append(
                    f"| {config} | {window} | {record['decision_date']} | {record['names']} | "
                    f"{record.get('max_weight', '')} | {record.get('max_min_rrc', '')} |"
                )
    lines += [
        "",
        "## 6. Y0's layer-1 actions on the SPY core (what bp3/bp4 remove)",
        "",
        "| window | core stop/re-entry dates |",
        "|---|---|",
    ]
    for window in WINDOWS:
        actions = runs[f"Y0_{window}"]["params"].get("core_stop_actions") or []
        lines.append(
            f"| {window} | {len(actions)} ({actions[0] if actions else '-'} .. {actions[-1] if actions else '-'}) |"
        )
    lines += [
        "",
        "## 7. The filter and the pool, per configuration",
        "",
        "| config | window | filter cash dates | second pass | final names |",
        "|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            run = runs[f"{config}_{window}"]
            states = run["params"].get("trend_filter_dates") or []
            cash = [row["date"] for row in states if row["state"] == "cash"]
            records = run["params"].get("pool_decisions") or []
            second = sum(1 for row in records if row["second_pass"])
            final = sorted({int(row["final_n"]) for row in records})
            lines.append(
                f"| {config} | {window} | {', '.join(cash) or '-'} | {second} | "
                f"{'/'.join(str(v) for v in final) or '-'} |"
            )
    lines += [
        "",
        "## 8. Identity check against round-3 X5",
        "",
        "| window | DB summaries | DB tests | DB weights | DB actions | JSON summaries | JSON tests | JSON daily |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for window in WINDOWS:
        row = identity[window]
        db = row["db_vs_db"]
        js = row["json_vs_stored"]
        lines.append(
            f"| {window} | {db['summaries']['equal']} | {db['tests']['equal']} | "
            f"{db['weights']['equal']} | {db['guard_actions']['equal']} | "
            f"{js['summaries']['equal']} | {js['tests']['equal']} | {js['daily']['equal']} |"
        )
    lines.append("")
    if defective:
        lines += [
            f"## 9. Defective runs ({defective['commit']}, superseded)",
            "",
            "**Not entering adoption.** These Y1/Y2/Y4 runs used a defective implementation: the",
            "Basing Points machine was given the entry date for every session of its tracking",
            "history, so no rule-3 point in (s0, e] was recognized. They are not new",
            "configurations in the count ledger and are reported only for the record.",
            "",
            "| config | stop | weighting | mean MDD | mean cumulative | R | k | stop-outs | fallbacks |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for config in ("Y1", "Y2", "Y4"):
            row = defective["runs"].get(config)
            if not row:
                continue
            lines.append(
                f"| {config} | {row['stop_rule']} | {row['satellite_weighting']} | "
                f"{row['mean_mdd']:.6f} | {row['mean_cumulative']:.6f} | {row['R']:.4f} | "
                f"{row['k_lower_mdd_than_y0']} | {row['bp']['stop_outs']} | "
                f"{row['bp']['fallbacks']} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    runs_dir = Path(args.runs).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    identity_dir = Path(args.identity).expanduser().resolve()
    runs = _load(runs_dir)
    table = _config_table(runs)
    cross = _cross_tests(runs)
    identity = {
        window: json.loads((identity_dir / f"identity_{window}.json").read_text())
        for window in WINDOWS
    }
    defective_path = out_dir / "defective_runs.json"
    defective = json.loads(defective_path.read_text()) if defective_path.is_file() else None
    payload = {
        "plan": "docs/backtest_v4_experiments_plan.md",
        "scored_llm_sha256": "2187c36bae42296b84d63031e9058df5aba997a77da2a4aa6665279452e184ca",
        "declared_configurations_tried_after_v1": 45,
        "configurations": {
            f"{config}_{window}": {
                "config": config,
                "window": window,
                "stop_rule": runs[f"{config}_{window}"]["params"]["stop_rule"],
                "satellite_weighting": runs[f"{config}_{window}"]["params"]["satellite_weighting"],
                "bp": runs[f"{config}_{window}"]["params"].get("bp"),
                "distance_cells": runs[f"{config}_{window}"]["params"].get("distance_cells"),
                "rp": runs[f"{config}_{window}"]["params"].get("rp"),
                "core_stop_actions": runs[f"{config}_{window}"]["params"].get("core_stop_actions"),
                "trend_filter_dates": runs[f"{config}_{window}"]["params"]["trend_filter_dates"],
                "pool_decisions": runs[f"{config}_{window}"]["params"]["pool_decisions"],
                "summaries": runs[f"{config}_{window}"]["summaries"],
                "tests": runs[f"{config}_{window}"]["tests"],
                "equity": runs[f"{config}_{window}"]["equity"],
            }
            for config in CONFIGS
            for window in WINDOWS
        },
        "per_configuration": table,
        "cross_tests_vs_y0": cross,
        "identity": identity,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grid.json").write_text(json.dumps(payload, indent=1) + "\n")
    (out_dir / "choice.json").write_text(json.dumps(table, indent=1) + "\n")
    markdown = _grid_md(runs, table, cross, identity, defective)
    (out_dir / "grid.md").write_text(markdown + "\n")
    Path(args.docs).expanduser().resolve().write_text(markdown + "\n")
    print(
        json.dumps(
            {
                "adopted": table["adopted"],
                "grid": str(out_dir / "grid.json"),
                "docs": args.docs,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
