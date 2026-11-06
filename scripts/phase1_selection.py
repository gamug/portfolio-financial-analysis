"""Apply the pre-registered Stage A / Stage B adoption rules and the CHAMPION H test to the
corrected runs, as numbers only (Phase 1 §2 R4/R5; REPORT §6-§7).

    uv run python scripts/phase1_selection.py --out DIR
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from phase1_stats import WINDOWS, arm_daily, concatenate, load_run, metrics

R_THRESHOLD = 0.8446  # the Stage A/B rule's absolute R bar (final-round plan §2)
MDD_THRESHOLD = 0.0831
TIE = 0.001


def _concat(
    out: Path, run_id: str, arm: str, windows: tuple[str, ...] = WINDOWS
) -> tuple[dict[str, float], dict[str, float]]:
    parts = []
    rf_parts = []
    for window in windows:
        payload = load_run(out, f"{run_id}-{window}")
        parts.append(arm_daily(payload, arm))
        rf_parts.append(payload["rf"])
    return concatenate(parts), concatenate(rf_parts)


def _mean_mdd(out: Path, run_id: str, arm: str) -> float:
    values = []
    for window in WINDOWS:
        payload = load_run(out, f"{run_id}-{window}")
        values.append(metrics(arm_daily(payload, arm), payload["rf"])["max_drawdown"])
    return float(np.mean(values))


def _stage(out: Path) -> dict[str, Any]:
    z_configs = ["Z1", "Z2", "Z3", "Z4"]
    z_rows = []
    for config in z_configs:
        run_id = f"R2-{config}"
        concat, rf = _concat(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        item = metrics(concat, rf)
        z_rows.append(
            {
                "config": config,
                "R": item["cumulative_return"],
                "mean_mdd": _mean_mdd(out, run_id, "TOPN_MV_GUARD_NO_NLP"),
            }
        )
    z0_concat, z0_rf = _concat(out, "R1-B0", "TOPN_MV_GUARD_NO_NLP")
    z0 = metrics(z0_concat, z0_rf)
    z0_row = {
        "config": "Z0 (=B0)",
        "R": z0["cumulative_return"],
        "mean_mdd": _mean_mdd(out, "R1-B0", "TOPN_MV_GUARD_NO_NLP"),
    }
    a_qualifiers = [
        row for row in z_rows if row["R"] > R_THRESHOLD and row["mean_mdd"] <= MDD_THRESHOLD
    ]
    if a_qualifiers:
        best = max(a_qualifiers, key=lambda row: (row["R"], -row["mean_mdd"]))
        a_star = best["config"]
    else:
        best = z0_row
        a_star = "Z0"
    a_run = "R1-B0" if a_star == "Z0" else f"R2-{a_star}"
    a_concat, a_rf = _concat(out, a_run, "TOPN_MV_GUARD_NO_NLP")
    a_metrics = metrics(a_concat, a_rf)

    b_rows = []
    for config in ("B1", "B2", "B3", "B4", "B5", "B6"):
        run_id = f"R1-{config}"
        concat, rf = _concat(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        item = metrics(concat, rf)
        b_rows.append(
            {
                "config": config,
                "R": item["cumulative_return"],
                "mean_mdd": _mean_mdd(out, run_id, "TOPN_MV_GUARD_NO_NLP"),
            }
        )
    qualifiers = [
        row
        for row in b_rows
        if row["R"] > a_metrics["cumulative_return"] and row["mean_mdd"] <= MDD_THRESHOLD
    ]
    if qualifiers:
        best_b = max(qualifiers, key=lambda row: (row["R"], -row["mean_mdd"]))
        b_star = best_b["config"]
    else:
        best_b = None
        b_star = a_star
    b_run = (
        "R1-B0"
        if b_star == "Z0"
        else (f"R2-{b_star}" if b_star.startswith("Z") else f"R1-{b_star}")
    )
    b_concat, b_rf = _concat(out, b_run, "TOPN_MV_GUARD_NO_NLP")
    b_metrics = metrics(b_concat, b_rf)
    return {
        "stage_a": {
            "z0": z0_row,
            "rows": z_rows,
            "a_star": a_star,
            "a_star_R": a_metrics["cumulative_return"],
            "a_star_mean_mdd": _mean_mdd(out, a_run, "TOPN_MV_GUARD_NO_NLP"),
            "qualifiers": [row["config"] for row in a_qualifiers] if a_qualifiers else [],
        },
        "stage_b": {
            "rows": b_rows,
            "b_star": b_star,
            "b_star_R": b_metrics["cumulative_return"],
            "b_star_mean_mdd": _mean_mdd(out, b_run, "TOPN_MV_GUARD_NO_NLP"),
            "qualifiers": [row["config"] for row in qualifiers] if qualifiers else [],
        },
    }


def _champion(out: Path) -> dict[str, Any]:
    if not (out / "champion_selection_phase1.json").is_file():
        return {"selected": None, "configs": {}, "not_run": True}
    selection = json.loads((out / "champion_selection_phase1.json").read_text())
    selected = f"{selection['selected']['blend']}{selection['selected']['tilt']}"
    configs = [("B0T0", "B0T0")]
    if selected != "B0T0":
        configs.append((selected, selected))
    if "B3R2" not in {config for config, _ in configs}:
        configs.append(("B3R2", "B3R2"))
    result: dict[str, Any] = {"selected": selected, "configs": {}}
    for label, config in configs:
        per_window = {}
        for window in ("H1", "H2", "H3"):
            payload = load_run(out, f"R5-{config}-{window}")
            per_window[window] = {
                "guard": metrics(arm_daily(payload, "TOPN_MV_GUARD_NO_NLP"), payload["rf"]),
                "noguard": metrics(arm_daily(payload, "TOPN_MV_NOGUARD"), payload["rf"]),
            }
        result["configs"][label] = per_window
    # Paired HAC test of C* - B0T0 on the concatenated H windows.
    if selected != "B0T0":
        a = _concat(out, f"R5-{selected}", "TOPN_MV_GUARD_NO_NLP", ("H1", "H2", "H3"))[0]
        b = _concat(out, "R5-B0T0", "TOPN_MV_GUARD_NO_NLP", ("H1", "H2", "H3"))[0]
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
        result["hac_test"] = {
            "mean_daily": mean,
            "t": mean / se if se > 0 else 0.0,
            "p_two_sided": 2.0 * (1.0 - _norm_cdf(abs(mean / se))) if se > 0 else 1.0,
            "n": n,
        }
    return result


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    payload = {"stage": _stage(out), "champion": _champion(out)}
    (out / "selection_phase1.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(
        json.dumps(
            {
                "A*": payload["stage"]["stage_a"]["a_star"],
                "B*": payload["stage"]["stage_b"]["b_star"],
                "C*": payload["champion"]["selected"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
