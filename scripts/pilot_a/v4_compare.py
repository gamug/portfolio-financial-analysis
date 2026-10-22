#!/usr/bin/env python3
"""V4: the system side against the independent side, row by row (T-143 part A).

    .venv/bin/python scripts/pilot_a/v4_compare.py \
        --system /workspaces/thesis/handoff/pilot/v4/v4_system.csv \
        --independent /workspaces/thesis/handoff/pilot/v4/v4_independent.csv \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --out-csv /workspaces/thesis/scratch/t143a/out/v4_compare.csv \
        --out-json /workspaces/thesis/scratch/t143a/out/v4_compare.json

The acceptance is **zero disagreement**: for every (date, rule, asset) the two sides must agree on
``evaluated`` and on ``hit``.

Three kinds of difference are reported, and only the first is a disagreement:

* ``verdict`` -- both sides have the row and they disagree on ``evaluated`` or ``hit`` (or on the
  rule's severity): a disagreement, opened with the inputs of both sides;
* ``only_system`` / ``only_independent`` -- a row one side has and the other does not (the two
  sessions may cover different asset sets): reported with the counts, not as a disagreement;
* ``severity`` -- the same verdict, a different severity string.

The system side's inputs are recomputed from the database when a disagreement is opened (the same
readers, the same date), so the opened row shows the numbers the rule read.

The independent side's file may arrive later; the comparer is written and tested on a small
hand-made file meanwhile (see ``--independent`` on a file with a few rows).
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sibling(name: str) -> Any:
    """``scripts/pilot_a/`` is not a package, so the sibling module is loaded by path."""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


system_inputs = _sibling("v4_system_side").system_inputs

COLUMNS = (
    "date",
    "rule_id",
    "asset_id",
    "ticker",
    "system_evaluated",
    "system_hit",
    "system_severity",
    "independent_evaluated",
    "independent_hit",
    "independent_severity",
    "difference",
    "system_inputs_json",
    "independent_inputs_json",
)


def _read(path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            key = (str(row["date"]), str(row["rule_id"]), str(row["asset_id"]))
            rows[key] = row
    return rows


def _flag(row: dict[str, Any] | None, name: str) -> str:
    if row is None:
        return ""
    return str(row.get(name, "")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    parser.add_argument("--independent", required=True)
    parser.add_argument("--db", default="")
    parser.add_argument("--universe-db", default="/workspaces/thesis/data/universe_history_gics.db")
    parser.add_argument("--out-csv", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--max-disagreements", type=int, default=200)
    parser.add_argument(
        "--max-opened",
        type=int,
        default=50,
        help="how many disagreements are opened with both sides' inputs (each one re-reads the "
        "database, ~10 s): the rest are counted and listed without their inputs",
    )
    args = parser.parse_args()

    system = _read(Path(args.system))
    independent = _read(Path(args.independent))
    common = sorted(set(system) & set(independent))
    only_system = sorted(set(system) - set(independent))
    only_independent = sorted(set(independent) - set(system))

    disagreements: list[dict[str, Any]] = []
    severity_diffs: list[dict[str, Any]] = []
    by_rule: Counter[str] = Counter()
    by_date: Counter[str] = Counter()
    for key in common:
        s, i = system[key], independent[key]
        s_eval, i_eval = _flag(s, "evaluated"), _flag(i, "evaluated")
        s_hit, i_hit = _flag(s, "hit"), _flag(i, "hit")
        s_sev, i_sev = _flag(s, "severity"), _flag(i, "severity")
        if s_eval == i_eval and s_hit == i_hit:
            if s_sev != i_sev:
                severity_diffs.append(
                    {
                        "date": key[0],
                        "rule_id": key[1],
                        "asset_id": key[2],
                        "system_severity": s_sev,
                        "independent_severity": i_sev,
                    }
                )
            continue
        by_rule[key[1]] += 1
        by_date[key[0]] += 1
        entry: dict[str, Any] = {
            "date": key[0],
            "rule_id": key[1],
            "asset_id": key[2],
            "ticker": _flag(s, "ticker") or _flag(i, "ticker"),
            "system_evaluated": s_eval,
            "system_hit": s_hit,
            "independent_evaluated": i_eval,
            "independent_hit": i_hit,
            "system_severity": s_sev,
            "independent_severity": i_sev,
            "difference": (
                f"hit: system={s_hit} independent={i_hit}"
                if s_eval == i_eval
                else f"evaluated: system={s_eval} independent={i_eval}"
            ),
            "independent_inputs_json": _flag(i, "inputs_json"),
        }
        if args.db:
            entry["system_inputs_json"] = json.dumps(
                system_inputs(args.db, key[0], int(key[2]), args.universe_db),
                sort_keys=True,
                default=str,
            )
        else:
            entry["system_inputs_json"] = _flag(s, "inputs_json")
        disagreements.append(entry)

    out_csv = Path(args.out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for entry in disagreements[: args.max_disagreements]:
            writer.writerow([entry.get(c, "") for c in COLUMNS])

    doc = {
        "system": {"path": str(args.system), "rows": len(system)},
        "independent": {"path": str(args.independent), "rows": len(independent)},
        "common_rows": len(common),
        "disagreements": len(disagreements),
        "disagreements_by_rule": dict(sorted(by_rule.items())),
        "disagreements_by_date": dict(sorted(by_date.items())),
        "severity_differences": len(severity_diffs),
        "only_system": len(only_system),
        "only_independent": len(only_independent),
        "only_system_examples": [
            {"date": d, "rule_id": r, "asset_id": a, "ticker": _flag(system[(d, r, a)], "ticker")}
            for d, r, a in only_system[:20]
        ],
        "only_independent_examples": [
            {
                "date": d,
                "rule_id": r,
                "asset_id": a,
                "ticker": _flag(independent[(d, r, a)], "ticker"),
            }
            for d, r, a in only_independent[:20]
        ],
        "acceptance": (
            "zero disagreements"
            if not disagreements
            else f"{len(disagreements)} disagreements to open"
        ),
        "disagreements_opened": disagreements[: args.max_disagreements],
    }
    Path(args.out_json).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    print(
        f"system {len(system)} rows · independent {len(independent)} rows · common {len(common)} · "
        f"disagreements {len(disagreements)} · only_system {len(only_system)} · "
        f"only_independent {len(only_independent)}"
    )
    print(f"wrote {out_csv} and {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
