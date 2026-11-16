"""Apply CHAMPION's pre-registered selection rule to the corrected R4 grid (Phase 1 §2 R4).

    uv run python scripts/phase1_champion_select.py --out DIR

Reads the 25 ``R4-<blend><tilt>-<window>.json`` collectors, takes the four design numbers
{P1, P2a} x {TOPN_MV_NOGUARD, TOPN_MV_GUARD_NO_NLP}, and writes
``champion_selection_phase1.json``: the lowest mean maximum drawdown wins, ties within 0.01 pp
go to the higher mean cumulative return.  No new rule and no new configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BLENDS = ("B0", "B1", "B2", "B3", "B4")
TILTS = ("T0", "R1", "R2", "R3", "R4")
ARMS = ("TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP")
SEGMENTS = ("P1", "P2a")
TIE = 0.0001  # 0.01 pp


def _numbers(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, float]]:
    out: dict[tuple[str, str], dict[str, float]] = {}
    for row in payload["summaries"]:
        if row["segment"] in SEGMENTS and row["arm"] in ARMS:
            out[(row["segment"], row["arm"])] = {
                "max_drawdown": float(row["max_drawdown"]),
                "cumulative_return": float(row["cumulative_return"]),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    rows = []
    for blend in BLENDS:
        for tilt in TILTS:
            config = f"{blend}{tilt}"
            per: dict[tuple[str, str], dict[str, float]] = {}
            for window in ("P1", "P3", "P2"):
                path = out / "runs" / f"R4-{config}-{window}.json"
                if not path.is_file():
                    raise SystemExit(f"missing {path}")
                per.update(_numbers(json.loads(path.read_text())))
            mdd = [per[key]["max_drawdown"] for key in per]
            cum = [per[key]["cumulative_return"] for key in per]
            missing = {(segment, arm) for segment in SEGMENTS for arm in ARMS} - set(per)
            if missing:
                raise SystemExit(f"missing design numbers for {config}: {sorted(missing)}")
            rows.append(
                {
                    "blend": blend,
                    "tilt": tilt,
                    "mean_mdd": sum(mdd) / len(mdd),
                    "mean_cumulative_return": sum(cum) / len(cum),
                    "per_number": {
                        segment: {arm: per[(segment, arm)] for arm in ARMS} for segment in SEGMENTS
                    },
                }
            )
    rows.sort(key=lambda row: (row["mean_mdd"], -row["mean_cumulative_return"]))
    best = rows[0]["mean_mdd"]
    tied = [row for row in rows if row["mean_mdd"] <= best + TIE]
    selected = max(tied, key=lambda row: row["mean_cumulative_return"])
    payload = {
        "rule": (
            "lowest mean maximum drawdown over {P1,P2a} x {NOGUARD,GUARD}; ties within "
            "0.01 pp to the higher mean cumulative return"
        ),
        "configurations_tried": len(rows),
        "selected": {"blend": selected["blend"], "tilt": selected["tilt"]},
        "rows": rows,
    }
    target = out / "champion_selection_phase1.json"
    target.write_text(json.dumps(payload, indent=1) + "\n")
    print(json.dumps({"selected": payload["selected"], "mean_mdd": selected["mean_mdd"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
