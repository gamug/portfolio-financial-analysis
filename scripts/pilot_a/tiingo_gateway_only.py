#!/usr/bin/env python3
"""P2 step 2's gate: the Tiingo import's ``lines_gateway_only`` must be an empty list.

    uv run python scripts/pilot_a/tiingo_gateway_only.py --db /workspaces/thesis/pilot_a/financial.db

Read-only (``mode=ro``). ``lines_gateway_only`` names every line the import skipped because the
database already held a gateway candle for it (T-153 part 3d, rule 2: one vendor per line, the
gateway wins). On a fresh part A database the gateway serves every line it can, so the list must be
empty; a named line **stops the run** (the brief, P2 step 2) with its gateway count.

**Fail closed:** a database with no ``tiingo-import`` run record is not an all-clear; the script
exits 2 and says so. Exits 1 when the list is non-empty, 0 when it is empty.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

IMPORT_RUN_KIND = "tiingo-import"


def latest_import_params(db_path: str | Path) -> dict[str, object] | None:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT id, params_json FROM pricing_run WHERE params_json LIKE ? ORDER BY id DESC",
            (f'%"kind": "{IMPORT_RUN_KIND}"%',),
        ).fetchall()
    for run_id, params_json in rows:
        try:
            params = json.loads(params_json or "{}")
        except json.JSONDecodeError:
            continue
        if params.get("kind") == IMPORT_RUN_KIND:
            params["_run_id"] = run_id
            return params
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="the part A database, read-only")
    args = parser.parse_args(argv)

    try:
        params = latest_import_params(args.db)
    except sqlite3.Error as exc:
        print(f"error: cannot read the import run from {args.db}: {exc}", file=sys.stderr)
        return 2
    if params is None:
        print(f"error: no {IMPORT_RUN_KIND!r} run recorded in {args.db}", file=sys.stderr)
        return 2

    named = list(params.get("lines_gateway_only") or [])
    print(
        f"import run {params['_run_id']}: {params.get('lines_imported')}/{params.get('lines')} "
        f"lines imported, {params.get('price_rows')} price rows, "
        f"{params.get('dividend_rows')} dividends, {params.get('split_rows')} splits"
    )
    print(f"lines_gateway_only: {len(named)}")
    for line in named:
        print(f"  {line}")
    return 1 if named else 0


if __name__ == "__main__":
    raise SystemExit(main())
