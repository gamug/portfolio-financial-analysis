"""Numerical diagnostics for the R10 MARKOWITZ_SAMPLE control.

    uv run python scripts/markowitz_sample_diagnostics.py --out DIR

Reads ``runs/R10-MARKOWITZ_SAMPLE-*.json`` (the collector's per-decision covariance and solver
records), writes ``markowitz_sample_diagnostics.json`` and prints a summary.  The hard-stop
conditions are: a non-finite or > 1e12 condition number, an infeasible/failed status, a solver
fallback, any recorded relaxation, or a covariance-estimator substitution.  ``optimal_inaccurate``
is counted as non-optimal but is not by itself a hard stop (the solver did converge).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

CONDITION_LIMIT = 1e12
WINDOWS = ("P1", "H1", "P3", "H2", "P2", "H3", "C1")
ARMS = ("MARKOWITZ_BASE", "MARKOWITZ_BASE_GUARD")


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    conditions = [
        float(row["condition_number"]) for row in rows if row.get("condition_number") is not None
    ]
    return {
        "decisions": len(rows),
        "n_assets_min": min((int(row["n_assets"]) for row in rows), default=0),
        "n_assets_max": max((int(row["n_assets"]) for row in rows), default=0),
        "condition_min": min(conditions, default=None),
        "condition_median": statistics.median(conditions) if conditions else None,
        "condition_max": max(conditions, default=None),
        "non_optimal": sum(not row["optimal"] for row in rows),
        "optimal_inaccurate": sum(row["status"] == "optimal_inaccurate" for row in rows),
        "status_other": sorted({row["status"] for row in rows if not row["optimal"]}),
        "solver_fallbacks": sum(bool(row["solver_fallback"]) for row in rows),
        "relaxations": sum(bool(row["relaxations"]) for row in rows),
        "substitutions": sum(bool(row["substitution"]) for row in rows),
    }


def _bad(row: dict[str, Any]) -> str | None:  # noqa: PLR0911 - one reason per check
    if not row.get("condition_finite"):
        return "singular covariance"
    condition = row.get("condition_number")
    if condition is not None and float(condition) > CONDITION_LIMIT:
        return "ill-conditioned covariance"
    if row["status"] not in ("optimal", "optimal_inaccurate"):
        return "infeasible or failed solve"
    if row["solver_fallback"]:
        return "solver fallback"
    if row["relaxations"]:
        return "relaxation"
    if row["substitution"]:
        return "covariance substitution"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    payload: dict[str, Any] = {"condition_limit": CONDITION_LIMIT, "runs": {}, "bad": []}
    for window in WINDOWS:
        path = out / "runs" / f"R10-MARKOWITZ_SAMPLE-{window}.json"
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        data = json.loads(path.read_text())
        run = {"window": window, "arms": {}}
        for arm in ARMS:
            rows = data["numerical_diagnostics"][arm]
            run["arms"][arm] = {
                "summary": _summary(rows),
                "decisions": rows,
            }
            for row in rows:
                reason = _bad(row)
                if reason is not None:
                    payload["bad"].append(
                        {
                            "window": window,
                            "arm": arm,
                            "decision_date": row["decision_date"],
                            "reason": reason,
                            **row,
                        }
                    )
        payload["runs"][window] = run
    (out / "markowitz_sample_diagnostics.json").write_text(json.dumps(payload, indent=1) + "\n")
    print(
        json.dumps(
            {
                "windows": len(payload["runs"]),
                "decisions": sum(
                    run["arms"]["MARKOWITZ_BASE"]["summary"]["decisions"]
                    for run in payload["runs"].values()
                ),
                "bad": len(payload["bad"]),
                "condition_max": max(
                    (
                        run["arms"]["MARKOWITZ_BASE"]["summary"]["condition_max"]
                        for run in payload["runs"].values()
                        if run["arms"]["MARKOWITZ_BASE"]["summary"]["condition_max"] is not None
                    ),
                    default=None,
                ),
            }
        )
    )
    return 2 if payload["bad"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
