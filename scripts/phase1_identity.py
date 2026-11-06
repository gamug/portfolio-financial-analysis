"""Phase 1 identity checks: Z2 vs Z0 (= B0) and the corrected FINAL's arm identities.

    uv run python scripts/phase1_identity.py --out DIR

Z2 ([1, 4], fixed) is expected to equal Z0/B0 ([1, 3, 4], fixed) when layer 3 never acts; any
difference is reported, not hidden.  The comparison uses the collectors' summaries, tests and
daily net-return series, keyed by window (never by row id across databases).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from phase1_stats import WINDOWS, arm_daily, load_run

IDENTITY_TOL = 1e-12
ARMS = ("MARKOWITZ_BASE", "MARKOWITZ_BASE_GUARD", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP")


def _diff(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    days = sorted(set(a) & set(b))
    values = np.asarray([a[day] - b[day] for day in days], dtype=float)
    return {
        "n": float(len(days)),
        "max_abs": float(np.max(np.abs(values))) if len(values) else 0.0,
        "mean_abs": float(np.mean(np.abs(values))) if len(values) else 0.0,
    }


def _summary_map(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["arm"], row["segment"]): row for row in payload["summaries"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    result: dict[str, Any] = {}
    for window in WINDOWS:
        z2 = load_run(out, f"R2-Z2-{window}")
        z0 = load_run(out, f"R1-B0-{window}")
        arm_result: dict[str, Any] = {}
        for arm in ARMS:
            if arm not in z2["daily"] or arm not in z0["daily"]:
                continue
            arm_result[arm] = {
                "daily": _diff(arm_daily(z2, arm), arm_daily(z0, arm)),
                "summary_equal": _summary_map(z2).get((arm, window))
                == _summary_map(z0).get((arm, window)),
                "n_guard_actions_z2": _summary_map(z2)
                .get((arm, window), {})
                .get("n_guard_actions"),
                "n_guard_actions_z0": _summary_map(z0)
                .get((arm, window), {})
                .get("n_guard_actions"),
            }
        result[window] = arm_result
    (out / "identity.json").write_text(json.dumps(result, indent=1) + "\n")
    identical = all(
        arm["daily"]["max_abs"] <= IDENTITY_TOL and arm["summary_equal"]
        for window in result.values()
        for arm in window.values()
    )
    print(json.dumps({"Z2_equals_Z0": identical}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
