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


def _config_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload["params"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(  # noqa: C901, PLR0912 - one merge path
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
    for run_id, row in rows.items():
        json_path = Path(row.get("output_json") or "")
        if json_path.is_file():
            row["exit"] = "0"
            row["output_sha256"] = _sha256(json_path)
            row["config_sha256"] = _config_sha256(json.loads(json_path.read_text()))
        row["control"] = "True" if row.get("block") == "R10" else "False"
        row["db_retained"] = "True" if (out / "dbs" / f"{run_id}.db").is_file() else "False"
        row.pop("db_kept", None)
    # Any collector JSON without a manifest row (a block killed before its writer flushed,
    # or a resumed block) is added from the JSON's own params; the sha is recomputed.
    for json_path in sorted((out / "runs").glob("*.json")):
        run_id = json_path.stem
        if run_id in rows:
            continue
        payload = json.loads(json_path.read_text())
        parts = run_id.split("-")
        block, config, window = parts[0], parts[1], parts[2]
        cost = float(payload["params"].get("cost_bps", 10.0))
        rows[run_id] = {
            "run_id": run_id,
            "block": block,
            "config": config,
            "window": window,
            "cost_bps": str(cost),
            "params_sha256": _sha256(
                Path(__file__).resolve().parents[1] / "src/cycle/backtest/params.frozen.toml"
            ),
            "config_sha256": _config_sha256(payload),
            "template_sha256": _sha256(
                Path("/workspaces/thesis/scratch/final/out/pitfix/templates")
                / ("template_C1_pitfix.db" if window == "C1" else f"template_{window}_pitfix.db")
            ),
            "output_sha256": _sha256(json_path),
            "exit": "0",
            "seconds": "",
            "diagnostic_only": str(run_id.endswith("R2-Z2-P1") or "-Z2-" in run_id).lower(),
            "control": "True" if block == "R10" else "False",
            "output_json": str(json_path),
            "log": "",
            "db_retained": "True" if (out / "dbs" / f"{run_id}.db").is_file() else "False",
        }
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
