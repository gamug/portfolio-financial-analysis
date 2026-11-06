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


def _mean_cum(out: Path, run_id: str, arm: str) -> float:
    values = []
    for window in WINDOWS:
        payload = load_run(out, f"{run_id}-{window}")
        values.append(metrics(arm_daily(payload, arm), payload["rf"])["cumulative_return"])
    return float(np.mean(values))


def _mean_mdd(out: Path, run_id: str, arm: str) -> float:
    values = []
    for window in WINDOWS:
        payload = load_run(out, f"{run_id}-{window}")
        values.append(metrics(arm_daily(payload, arm), payload["rf"])["max_drawdown"])
    return float(np.mean(values))


def _stage(out: Path) -> dict[str, Any]:
    """The pre-registered rule on the corrected panel.

    ``R`` is the ratio mean cumulative return / mean MDD over the six windows (the final-round
    plan's R; Y2's old value was 0.8446).  The threshold is the corrected Y2 ratio.
    """
    z_rows = []
    for config in ("Z1", "Z2", "Z3", "Z4"):
        run_id = f"R2-{config}"
        mean_cum = _mean_cum(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        mean_mdd = _mean_mdd(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        z_rows.append(
            {
                "config": config,
                "mean_cumulative_return": mean_cum,
                "mean_mdd": mean_mdd,
                "R": mean_cum / mean_mdd if mean_mdd > 0 else 0.0,
            }
        )
    z0_mean_cum = _mean_cum(out, "R1-B0", "TOPN_MV_GUARD_NO_NLP")
    z0_mean_mdd = _mean_mdd(out, "R1-B0", "TOPN_MV_GUARD_NO_NLP")
    z0_row = {
        "config": "Z0 (=B0)",
        "mean_cumulative_return": z0_mean_cum,
        "mean_mdd": z0_mean_mdd,
        "R": z0_mean_cum / z0_mean_mdd if z0_mean_mdd > 0 else 0.0,
    }
    a_qualifiers = [
        row for row in z_rows if row["R"] > z0_row["R"] and row["mean_mdd"] <= MDD_THRESHOLD
    ]
    if a_qualifiers:
        best = max(a_qualifiers, key=lambda row: (row["R"], -row["mean_mdd"]))
        a_star = best["config"]
    else:
        a_star = "Z0"
    a_run = "R1-B0" if a_star == "Z0" else f"R2-{a_star}"
    a_mean_cum = _mean_cum(out, a_run, "TOPN_MV_GUARD_NO_NLP")
    a_mean_mdd = _mean_mdd(out, a_run, "TOPN_MV_GUARD_NO_NLP")
    a_ratio = a_mean_cum / a_mean_mdd if a_mean_mdd > 0 else 0.0

    b_rows = []
    for config in ("B1", "B2", "B3", "B4", "B5", "B6"):
        run_id = f"R1-{config}"
        mean_cum = _mean_cum(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        mean_mdd = _mean_mdd(out, run_id, "TOPN_MV_GUARD_NO_NLP")
        b_rows.append(
            {
                "config": config,
                "mean_cumulative_return": mean_cum,
                "mean_mdd": mean_mdd,
                "R": mean_cum / mean_mdd if mean_mdd > 0 else 0.0,
            }
        )
    b_qualifiers = [
        row for row in b_rows if row["R"] > a_ratio and row["mean_mdd"] <= MDD_THRESHOLD
    ]
    if b_qualifiers:
        best_b = max(b_qualifiers, key=lambda row: (row["R"], -row["mean_mdd"]))
        b_star = best_b["config"]
    else:
        b_star = a_star
    b_run = (
        "R1-B0"
        if b_star == "Z0"
        else (f"R2-{b_star}" if b_star.startswith("Z") else f"R1-{b_star}")
    )
    b_mean_cum = _mean_cum(out, b_run, "TOPN_MV_GUARD_NO_NLP")
    b_mean_mdd = _mean_mdd(out, b_run, "TOPN_MV_GUARD_NO_NLP")
    b_ratio = b_mean_cum / b_mean_mdd if b_mean_mdd > 0 else 0.0
    return {
        "stage_a": {
            "z0": z0_row,
            "rows": z_rows,
            "a_star": a_star,
            "a_star_R": a_ratio,
            "a_star_mean_cumulative_return": a_mean_cum,
            "a_star_mean_mdd": a_mean_mdd,
            "threshold_ratio": z0_row["R"],
            "qualifiers": [row["config"] for row in a_qualifiers],
        },
        "stage_b": {
            "rows": b_rows,
            "b_star": b_star,
            "b_star_R": b_ratio,
            "b_star_mean_cumulative_return": b_mean_cum,
            "b_star_mean_mdd": b_mean_mdd,
            "qualifiers": [row["config"] for row in b_qualifiers],
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
