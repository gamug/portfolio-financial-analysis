#!/usr/bin/env python3
"""P2's manifest: the row counts per table, and the database's sha256 at the end of P2.

    uv run python scripts/pilot_a/p2_manifest.py \
        --db /workspaces/thesis/pilot_a/financial.db --tag pilot-a-2026-10-18 \
        --analysis-date 2026-10-18 --out /workspaces/thesis/scratch/t143a/out/p2_manifest.json

Read-only (``mode=ro``): the plan's P2 manifest -- the commit tag, D, the row counts per table,
and the sha256 of the finished database. Counts only; never a row's value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

#: The tables a reader checks first (the plan's P2 record); every other non-``sqlite_`` table is
#: listed too, so a new one cannot hide.
MAIN_TABLES = (
    "assets",
    "price_daily",
    "price_observation",
    "price_window",
    "corporate_action",
    "sec_filings",
    "financial_facts",
    "fundamental_metrics",
    "metrics_run",
    "pricing_run",
    "quant_run",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def table_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        names = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        for name in names:
            # ``name`` is a table the database itself declares, never a caller's string.
            sql = f'SELECT COUNT(*) FROM "{name}"'  # noqa: S608 - sqlite_master's own name
            counts[name] = int(conn.execute(sql).fetchone()[0])
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    db_path = Path(args.db)
    counts = table_counts(db_path)
    doc = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tag": args.tag,
        "analysis_date": args.analysis_date,
        "db": str(db_path),
        "db_bytes": db_path.stat().st_size,
        "db_sha256": sha256(db_path),
        "table_counts": counts,
        "main_table_counts": {name: counts.get(name) for name in MAIN_TABLES},
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
