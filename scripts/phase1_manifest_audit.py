"""Audit the Phase-1 run manifest without re-running anything.

    uv run python scripts/phase1_manifest_audit.py --out DIR

The collector's canonical ``runs_manifest.csv`` is the input.  The audit adds the per-run
``config_sha256`` (the effective parameters stored in ``runs/<run_id>.json``), replaces the
old ``db_kept`` path with the boolean ``db_retained``, and marks the eight rows whose
collector was killed before it flushed its log/index as ``not recorded`` instead of inventing
a duration.  It also writes ``MANIFEST_NOTES.md`` beside the CSV.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

FIELDS = [
    "run_id",
    "block",
    "config",
    "window",
    "cost_bps",
    "params_sha256",
    "config_sha256",
    "template_sha256",
    "output_sha256",
    "exit",
    "seconds",
    "diagnostic_only",
    "control",
    "output_json",
    "log",
    "db_retained",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload["params"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _notes(rows: list[dict[str, str]], retained: list[str]) -> str:
    recorded = [float(row["seconds"]) for row in rows if row["seconds"] not in ("not recorded", "")]
    phase1 = [row for row in rows if row["block"] not in {"R9", "R10"}]
    f2 = [row for row in rows if row["block"] == "R9"]
    r10 = [row for row in rows if row["block"] == "R10"]

    def _seconds(block_rows: list[dict[str, str]]) -> float:
        return sum(
            float(row["seconds"])
            for row in block_rows
            if row["seconds"] not in ("not recorded", "")
        )

    return f"""# Phase-1/F2-1/F2-2 run manifest — field notes

The canonical CSV is `runs_manifest.csv`; this note is generated with it by
`scripts/phase1_manifest_audit.py`.  The audit reads the existing collector output and never
re-runs a backtest.

## Columns

- `params_sha256`: sha256 of `src/cycle/backtest/params.frozen.toml`, the frozen parameter
  file.  It is **one value for all {len(rows)} rows** and therefore does **not** identify a
  run's configuration.
- `config_sha256`: sha256 of that run's effective `params` object in
  `runs/<run_id>.json`, canonical JSON (`sort_keys=True`, `separators=(",", ":")`).  This is
  the per-run configuration hash; it includes `filter_frequency` for F2-1 and
  `cov_estimator` for F2-2.
- `seconds`: the engine subprocess's wall-clock duration measured by
  `phase1_run.run_one` (start immediately before the backtest subprocess, stop immediately
  after it).  It excludes the reflink template copy, the JSON collection step, and queue
  wait between jobs.  Eight Phase-1 rows were written by a killed block whose writer never
  flushed its log/index (`R2-X2-H3`, `R2-X3-H1`, `R2-X3-H2`, `R2-X3-H3`, `R2-X3-P1`,
  `R2-X3-P2`, `R2-X3-P3`, `R2-X4-P1`); their JSON mtimes are the resumed collector's write
  times (one is earlier than `created_at`), so no duration is recoverable and `seconds` is
  `not recorded`.  The same eight have `log = not recorded`.  Every F2-1 and F2-2 row has a
  recorded duration and log.
- `db_retained`: true only when the row's DB exists in `dbs/`.  {len(retained)} files are
  present: the eight Phase-1 files (including `R1-B0-P2.db` from the earlier fix round).  The
  seven R9 monthly DBs that F2-1 kept are no longer present in `dbs/`; their JSONs and REPORT
  section 14 remain.  The R10 MARKOWITZ_SAMPLE and control copies were deleted after
  collection.  The earlier L1/L2 rows point at deleted copies and are false.
- `control`: true for the R10 MARKOWITZ_SAMPLE rows (a covariance-regularisation control, not
  a candidate); false otherwise.
- `exit`: 0 for every row.

## Run-time overrun against the 8 h estimate

Phase 1's 327 runs recorded **{_seconds(phase1) / 3600:.1f} h** of engine time and ran
2026-11-06T04:50:10Z to 17:42:11Z (**12 h 52 min** wall clock), over the plan's 8 h estimate.
The boundary was crossed at about 12:50Z while the R2 block and the later R8 C1 continuation
were still running.  The overrun is the actual compute volume, not a single incident: R2
alone records 15.8 h over 142 runs, and R8 C1 adds 0.63 h after 16:10Z.  `PROGRESS.md`
records the contention diagnosis.

F2-1's 15 runs recorded **{_seconds(f2) / 3600:.2f} h** of engine time and took **34 min 19 s**
wall clock with one worker, below its 1 h 51 min preflight estimate.  F2-2's 7 runs recorded
**{_seconds(r10) / 3600:.2f} h** of engine time (about 17 min wall clock), below its 51 min
preflight estimate.  All {len(rows)} rows now in this manifest sum to
**{sum(recorded) / 3600:.1f} h** of recorded engine time.

## Counts

- rows: {len(rows)}
- exit = 0: {sum(1 for row in rows if row["exit"] == "0")}
- control = true: {sum(1 for row in rows if row["control"] == "True")}
- db_retained = true: {sum(1 for row in rows if row["db_retained"] == "True")}
- seconds = not recorded: {sum(1 for row in rows if row["seconds"] == "not recorded")}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    source = out / "runs_manifest.csv"
    if not source.is_file():
        raise SystemExit(f"missing {source}")
    with source.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("empty manifest")
    for row in rows:
        json_path = Path(row.get("output_json") or "")
        if not json_path.is_file():
            raise SystemExit(f"{row['run_id']}: missing {json_path}")
        payload = json.loads(json_path.read_text())
        row["config_sha256"] = _config_sha256(payload)
        row["output_sha256"] = _sha256(json_path)
        if not row.get("seconds"):
            row["seconds"] = "not recorded"
        if not row.get("log"):
            row["log"] = "not recorded"
        row["diagnostic_only"] = (
            "True" if str(row.get("diagnostic_only", "")).lower() == "true" else "False"
        )
        row["control"] = "True" if row.get("block") == "R10" else "False"
        db_path = out / "dbs" / f"{row['run_id']}.db"
        row["db_retained"] = "True" if db_path.is_file() else "False"
        row.pop("db_kept", None)
    retained = sorted(row["run_id"] for row in rows if row["db_retained"] == "True")
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: item["run_id"]):
            writer.writerow(row)
    (out / "MANIFEST_NOTES.md").write_text(_notes(rows, retained))
    print(
        json.dumps(
            {
                "rows": len(rows),
                "retained": len(retained),
                "not_recorded": sum(1 for row in rows if row["seconds"] == "not recorded"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
