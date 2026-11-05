"""Resolve A* (plan §2) and FINAL (plan §3) from the run JSONs and write the dated choices.

    uv run python scripts/backtest_final_resolve.py --stage a --runs <dir> --out <dir>
    uv run python scripts/backtest_final_resolve.py --stage final --runs <dir> --out <dir>

Read-only over the JSONs; the output keeps aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

WINDOWS = ("P1", "P3", "P2", "H1", "H2", "H3")
GUARD = "TOPN_MV_GUARD_NO_NLP"
R_TIE = 0.001
MDD_LIMIT = 0.0831
STAGE_A = {
    "Z0": ("1,3,4", "fixed"),
    "Z1": ("1", "fixed"),
    "Z2": ("1,4", "fixed"),
    "Z3": ("1,3", "trailing"),
    "Z4": ("1,3,4", "trailing"),
}
STAGE_B = {
    "B0": "minvar",
    "B1": "ew",
    "B2": "vt",
    "B3": "maxdiv",
    "B4": "hrp",
    "B5": "mincvar",
    "B6": "ensemble",
}


def _metrics(runs: Path, config: str) -> dict[str, Any]:
    mdd: list[float] = []
    cumulative: list[float] = []
    per_window: dict[str, Any] = {}
    for window in WINDOWS:
        path = runs / f"{config}_{window}.json"
        if not path.is_file():
            raise SystemExit(f"missing run export {path}")
        run = json.loads(path.read_text())
        row = next(
            item
            for item in run["summaries"]
            if str(item["segment"]) == window and item["arm"] == GUARD
        )
        value_mdd = float(row["max_drawdown"])
        value_cumulative = float(row["cumulative_return"])
        mdd.append(value_mdd)
        cumulative.append(value_cumulative)
        per_window[window] = {
            "max_drawdown": value_mdd,
            "cumulative_return": value_cumulative,
        }
    mean_mdd = sum(mdd) / len(mdd)
    mean_cumulative = sum(cumulative) / len(cumulative)
    return {
        "configuration": config,
        "mean_mdd": mean_mdd,
        "mean_cumulative": mean_cumulative,
        "R": mean_cumulative / mean_mdd if mean_mdd else None,
        "per_window": per_window,
    }


def _choose(rows: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any] | None:
    eligible = [
        row
        for row in rows
        if row["configuration"] != baseline["configuration"]
        and row["R"] is not None
        and baseline["R"] is not None
        and row["R"] > baseline["R"]
        and row["mean_mdd"] <= MDD_LIMIT
    ]
    if not eligible:
        return None
    best = max(row["R"] for row in eligible)
    tied = [row for row in eligible if best - row["R"] <= R_TIE]
    return min(tied, key=lambda row: row["mean_mdd"])


def _write(out: Path, payload: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{payload['stage']}_resolution.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(json.dumps(payload, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("a", "final"), required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    runs = Path(args.runs).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if args.stage == "a":
        rows = [_metrics(runs, config) for config in STAGE_A]
        baseline = rows[0]
        winner = _choose(rows[1:], baseline)
        chosen = winner or baseline
        config = str(chosen["configuration"])
        layers, vol = STAGE_A[config]
        payload = {
            "stage": "a",
            "rule": "R > R(Z0) and mean MDD <= 0.0831; highest R; ties within 0.001 -> lower MDD",
            "baseline": baseline,
            "rows": rows,
            "qualifying": [
                row["configuration"]
                for row in rows[1:]
                if row["R"] > baseline["R"] and row["mean_mdd"] <= MDD_LIMIT
            ],
            "configuration": config,
            "guard_layers": layers,
            "vol_target": vol,
            "metrics": chosen,
        }
        _write(out, payload)
        (out / "a_star.json").write_text(
            json.dumps(
                {
                    "config": config,
                    "guard_layers": layers,
                    "vol_target": vol,
                    "mean_mdd": chosen["mean_mdd"],
                    "mean_cumulative": chosen["mean_cumulative"],
                    "R": chosen["R"],
                },
                indent=1,
            )
            + "\n"
        )
        return 0
    star = json.loads((out / "a_star.json").read_text())
    rows = [_metrics(runs, config) for config in STAGE_B]
    baseline = {
        "configuration": star["config"],
        "mean_mdd": star["mean_mdd"],
        "mean_cumulative": star["mean_cumulative"],
        "R": star["R"],
    }
    winner = _choose(rows[1:], baseline)
    chosen = winner or baseline
    config = str(chosen["configuration"])
    method = STAGE_B.get(config, "minvar")
    payload = {
        "stage": "final",
        "rule": "R > R(A*) and mean MDD <= 0.0831; highest R; ties within 0.001 -> lower MDD",
        "baseline": baseline,
        "rows": rows,
        "qualifying": [
            row["configuration"]
            for row in rows[1:]
            if row["R"] > baseline["R"] and row["mean_mdd"] <= MDD_LIMIT
        ],
        "configuration": config,
        "satellite_weighting": method,
        "metrics": chosen,
    }
    _write(out, payload)
    (out / "final.json").write_text(
        json.dumps(
            {
                "config": config,
                "satellite_weighting": method,
                "mean_mdd": chosen["mean_mdd"],
                "mean_cumulative": chosen["mean_cumulative"],
                "R": chosen["R"],
            },
            indent=1,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
