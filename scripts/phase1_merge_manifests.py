"""Merge the per-block run manifests and indices into the canonical Phase-1 files.

    uv run python scripts/phase1_merge_manifests.py --out DIR

The blocks run concurrently with block-specific files (``runs_manifest_R2.csv``); this script
deduplicates by ``run_id`` (a successful row wins over a failed collect), recomputes the output
sha256 from the JSON that exists, and writes ``runs_manifest.csv`` and ``run_index.json``.
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
    "template_sha256",
    "output_sha256",
    "exit",
    "seconds",
    "diagnostic_only",
    "output_json",
    "log",
    "db_kept",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(  # noqa: C901 - one merge path
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(out.glob("runs_manifest*.csv")):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if not row.get("run_id"):
                    continue
                run_id = row["run_id"]
                previous = rows.get(run_id)
                if previous is None or (previous.get("exit") != "0" and row.get("exit") == "0"):
                    rows[run_id] = row
    for _run_id, row in rows.items():
        json_path = Path(row.get("output_json") or "")
        if json_path.is_file():
            row["exit"] = "0"
            row["output_sha256"] = _sha256(json_path)
    target = out / "runs_manifest.csv"
    with target.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for run_id in sorted(rows):
            writer.writerow(rows[run_id])
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(out.glob("run_index*.json")):
        for row in json.loads(path.read_text()):
            run_id = row.get("run_id") or f"{row['block']}-{row['config']}-{row['window']}"
            previous = index.get(run_id)
            if previous is None or (previous.get("exit") != 0 and row.get("exit") == 0):
                index[run_id] = {**row, "run_id": run_id}
    for _run_id, row in index.items():
        json_path = Path(row.get("output_json") or "")
        if json_path.is_file() and row.get("exit") != 0:
            row["exit"] = 0
            row["seconds"] = row.get("seconds", 0.0)
    (out / "run_index.json").write_text(
        json.dumps(sorted(index.values(), key=lambda row: row["run_id"]), indent=1) + "\n"
    )
    print(json.dumps({"manifest_rows": len(rows), "index_rows": len(index)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
