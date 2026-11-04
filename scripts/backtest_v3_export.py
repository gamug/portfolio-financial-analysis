"""Generate the round-3 grid, the adoption table and the results doc (V3 plan §4).

    uv run python scripts/backtest_v3_export.py --runs <dir> --out <dir> --docs <path>

Read-only over the 72 per-run JSONs (``scripts/backtest_v3_collect.py``) and the six identity
JSONs. Portfolio aggregates only: no per-asset value and no SPY price/return series is emitted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cycle.backtest import stats

WINDOWS = ("P1", "P3", "P2", "H1", "H2", "H3")
SEGMENT_ORDER = ("P1", "P2", "P2a", "P2b", "P3", "H1", "H2", "H3")
CONFIGS = {
    "X0": (30, "spy", 1.0),
    "X1": (30, "spy", 0.5),
    "X2": (30, "base", 1.0),
    "X3": (30, "base", 0.5),
    "X4": (60, "spy", 1.0),
    "X5": (60, "spy", 0.5),
    "X6": (60, "base", 1.0),
    "X7": (60, "base", 0.5),
    "X8": (100, "spy", 1.0),
    "X9": (100, "spy", 0.5),
    "X10": (100, "base", 1.0),
    "X11": (100, "base", 0.5),
}
ARMS = (
    "MARKOWITZ_BASE",
    "MARKOWITZ_BASE_GUARD",
    "TOPN_MV_NOGUARD",
    "TOPN_MV_GUARD_NO_NLP",
)
GUARD = "TOPN_MV_GUARD_NO_NLP"
V3_R = 0.6549
V3_MDD_LIMIT = 0.0831
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
    x0 = {window: _summary(runs[f"X0_{window}"], window)[GUARD] for window in WINDOWS}
    for config, (pool_m, core_source, filter_exit) in CONFIGS.items():
        mdd = []
        cumulative = []
        per_window = {}
        for window in WINDOWS:
            guard = _summary(runs[f"{config}_{window}"], window)[GUARD]
            mdd.append(float(guard["max_drawdown"]))
            cumulative.append(float(guard["cumulative_return"]))
            per_window[window] = {
                "max_drawdown": float(guard["max_drawdown"]),
                "cumulative_return": float(guard["cumulative_return"]),
            }
        mean_mdd = sum(mdd) / len(mdd)
        mean_cumulative = sum(cumulative) / len(cumulative)
        second = [
            sum(
                1
                for row in runs[f"{config}_{window}"]["params"]["pool_decisions"] or []
                if row["second_pass"]
            )
            for window in WINDOWS
        ]
        rows.append(
            {
                "configuration": config,
                "pool_m": pool_m,
                "core_source": core_source,
                "filter_exit": filter_exit,
                "mean_mdd": mean_mdd,
                "mean_cumulative": mean_cumulative,
                "R": mean_cumulative / mean_mdd if mean_mdd else None,
                "k_lower_mdd_than_x0": sum(
                    1
                    for window in WINDOWS
                    if per_window[window]["max_drawdown"] < float(x0[window]["max_drawdown"])
                ),
                "second_pass_decisions": sum(second),
                "per_window": per_window,
            }
        )
    eligible = [
        row
        for row in rows
        if row["configuration"] != "X0"
        and row["R"] is not None
        and row["R"] > V3_R
        and row["mean_mdd"] <= V3_MDD_LIMIT
    ]
    adopted = None
    if eligible:
        best = max(row["R"] for row in eligible)
        tied = [row for row in eligible if best - row["R"] <= R_TIE]
        adopted = min(tied, key=lambda row: row["mean_mdd"])["configuration"]
    return {
        "rule": "R > 0.6549 and mean MDD <= 0.0831; highest R among improvements; ties within "
        "0.001 in R -> lower mean MDD; otherwise V3 stays",
        "declared_configurations_tried_after_v1": 17,
        "measured_configurations_tried_after_v1": 36,
        "qualifying": [row["configuration"] for row in sorted(eligible, key=lambda row: -row["R"])],
        "adopted": adopted,
        "rows": rows,
    }


def _cross_tests(runs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for config in CONFIGS:
        for window in WINDOWS:
            a = runs[f"{config}_{window}"]["daily"][GUARD]
            b = runs[f"X0_{window}"]["daily"][GUARD]
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


def _fmt(value: float | None, places: int) -> str:
    return "" if value is None else f"{value:.{places}f}"


def _grid_md(  # noqa: C901, PLR0912 - one linear document builder
    runs: dict[str, dict[str, Any]],
    config_table: dict[str, Any],
    cross: list[dict[str, Any]],
    identity: dict[str, dict[str, Any]],
) -> str:
    identity_ok = all(row["ok"] for row in identity.values())
    adopted = config_table["adopted"]
    lines = [
        "# System experiments round 3 -- results (generated)",
        "",
        "Generated by `scripts/backtest_v3_export.py` from the 72 run copies; do not edit by",
        "hand. Aggregates only: no per-asset value and no SPY price/return series appears here.",
        "",
        "## 0. Method and declarations",
        "",
        "- Plan: `docs/backtest_v3_experiments_plan.md` (commit `2dbae65`), §7 readings `c1154b0`.",
        "- Fixed as in v2: LLM scores `scored_llm.db` sha256 `2187c36b...`, blend B0, tilt T0,",
        "  frozen GUARD `params.frozen.toml` sha256 `ee22f352...`, core_weight 0.5, trend filter",
        "  on, 12 warm-up steps, one fresh rule-filled copy per run. No model call was made.",
        "- **Every result is in-sample**: all six windows were seen before round 3.",
        f"- X0 identity: {'all six windows reproduce v2 V3 exactly' if identity_ok else 'FAILED'}",
        "  (summaries, tests, weights and GUARD actions; max |delta| 0.0).",
        "- Declared count: **17** configurations tried after v1 (6 in v2 + 11 new here), as",
        "  the plan states. **Discrepancy:** v2's own record is a 25-configuration 5 x 5 grid",
        "  (`docs/champion_results.md`), so the measured cumulative count is 25 + 11 = **36**.",
        "  The plan's 17 is reported as declared; the adoption rule and the choice do not depend",
        "  on it (see PROGRESS.md, OPEN QUESTIONS).",
        f"- Adoption: {'**' + adopted + '**' if adopted else 'none qualifies; **V3 stays the system configuration** (tried and not adopted).'}",
        "",
        "## 1. Per configuration (TOPN_MV_GUARD_NO_NLP, six windows)",
        "",
        "| config | pool | core | exit | mean MDD | mean cumulative | R | k (lower MDD than X0) | second-pass decisions |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in config_table["rows"]:
        mark = " **adopted**" if row["configuration"] == adopted else ""
        lines.append(
            f"| {row['configuration']}{mark} | {row['pool_m']} | {row['core_source']} | "
            f"{row['filter_exit']:.1f} | {row['mean_mdd']:.6f} | "
            f"{row['mean_cumulative']:.6f} | {_fmt(row['R'], 4)} | "
            f"{row['k_lower_mdd_than_x0']} | {row['second_pass_decisions']} |"
        )
    lines += [
        "",
        f"Adoption rule: R > 0.6549 and mean MDD <= 0.0831; among the qualifying configurations "
        f"({', '.join(config_table['qualifying'])}) the highest R is adopted (ties within 0.001 "
        f"in R -> lower mean MDD).",
        "",
        "## 2. Every configuration x window x arm",
        "",
        "| config | window | segment | arm | cumulative | ann. return | ann. vol | Sharpe | MDD | turnover | cost bps | rebal. | GUARD actions | names held (min/max; 0 = cash) |",
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
    lines += [
        "",
        "## 3. Tests per window (TOPN_MV_GUARD_NO_NLP)",
        "",
        "### 3(i) vs X0 -- HAC mean difference and paired bootstrap delta MDD",
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
        "## 4. The filter at every decision date",
        "",
        "| config | window | cash dates | invested | not evaluable |",
        "|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            states = runs[f"{config}_{window}"]["params"]["trend_filter_dates"] or []
            cash = [row["date"] for row in states if row["state"] == "cash"]
            lines.append(
                f"| {config} | {window} | {', '.join(cash) or '-'} | "
                f"{sum(1 for row in states if row['state'] == 'invested')} | "
                f"{sum(1 for row in states if row['state'] == 'not_evaluable')} |"
            )
    lines += [
        "",
        "## 5. The pool's second pass, per decision",
        "",
        "| config | window | decisions | second pass | first-pass names | final names |",
        "|---|---|---|---|---|---|",
    ]
    for config in CONFIGS:
        for window in WINDOWS:
            records = runs[f"{config}_{window}"]["params"]["pool_decisions"] or []
            second = sum(1 for row in records if row["second_pass"])
            first = sorted({int(row["first_pass_n"]) for row in records})
            final = sorted({int(row["final_n"]) for row in records})
            lines.append(
                f"| {config} | {window} | {len(records)} | {second} | "
                f"{'/'.join(str(v) for v in first) or '-'} | "
                f"{'/'.join(str(v) for v in final) or '-'} |"
            )
    lines += [
        "",
        "## 6. Identity check against v2 V3",
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
    config_table = _config_table(runs)
    cross = _cross_tests(runs)
    identity = {
        window: json.loads((identity_dir / f"identity_{window}.json").read_text())
        for window in WINDOWS
    }
    payload = {
        "plan": "docs/backtest_v3_experiments_plan.md",
        "scored_llm_sha256": "2187c36bae42296b84d63031e9058df5aba997a77da2a4aa6665279452e184ca",
        "declared_configurations_tried_after_v1": 17,
        "configurations": {
            f"{config}_{window}": {
                "config": config,
                "window": window,
                "pool_m": runs[f"{config}_{window}"]["params"]["pool_m"],
                "core_source": runs[f"{config}_{window}"]["params"]["core_source"],
                "filter_exit": runs[f"{config}_{window}"]["params"]["filter_exit"],
                "trend_filter_dates": runs[f"{config}_{window}"]["params"]["trend_filter_dates"],
                "pool_decisions": runs[f"{config}_{window}"]["params"]["pool_decisions"],
                "summaries": runs[f"{config}_{window}"]["summaries"],
                "tests": runs[f"{config}_{window}"]["tests"],
                "equity": runs[f"{config}_{window}"]["equity"],
            }
            for config in CONFIGS
            for window in WINDOWS
        },
        "per_configuration": config_table,
        "cross_tests_vs_x0": cross,
        "identity": identity,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grid.json").write_text(json.dumps(payload, indent=1) + "\n")
    (out_dir / "choice.json").write_text(json.dumps(config_table, indent=1) + "\n")
    markdown = _grid_md(runs, config_table, cross, identity)
    (out_dir / "grid.md").write_text(markdown + "\n")
    Path(args.docs).expanduser().resolve().write_text(markdown + "\n")
    print(
        json.dumps(
            {
                "adopted": config_table["adopted"],
                "best_R": max(
                    (row["R"] for row in config_table["rows"] if row["R"] is not None),
                    default=None,
                ),
                "grid": str(out_dir / "grid.json"),
                "docs": args.docs,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
