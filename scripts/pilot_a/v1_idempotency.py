#!/usr/bin/env python3
"""V1's idempotency check: what did re-running P2's commands write?

    uv run python scripts/pilot_a/v1_idempotency.py \
        --before /workspaces/thesis/pilot_a/financial.db \
        --after /home/vscode/pilot_a_run/p3_copy.db \
        --out /workspaces/thesis/scratch/t143a/out/p3_v1_idempotency.json

Read-only on both databases. The "after" database started as an exact copy of the "before" one, so
every row it holds that the "before" one lacks was written by the re-run. The plan's V1 rule: a
re-run writes **0 rows**; reviewer B's override: rows written for the filings that failed with the
gateway's DNS errors in step 4 (ETR and the others) are **expected**, not a FAIL -- they are listed
by filing, and **any other written row is a FAIL**.

Counts, tickers, accessions and periods only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

#: The tables whose row count can change (the write paths P2 uses).
TABLES = (
    "sec_filings",
    "financial_facts",
    "fundamental_metrics",
    "filing_cover_shares",
    "data_quality_issue",
    "corporate_action",
    "price_daily",
    "price_observation",
    "value_inputs",
    "rule_input",
)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def _ids(conn: sqlite3.Connection, table: str) -> set[int]:
    # ``table`` is one of this script's own constants, never a caller's string.
    sql = f"SELECT id FROM {table}"  # noqa: S608 - our own table names
    return {int(r[0]) for r in conn.execute(sql)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    before = sqlite3.connect(f"file:{args.before}?mode=ro", uri=True)
    before.row_factory = sqlite3.Row
    after = sqlite3.connect(f"file:{args.after}?mode=ro", uri=True)
    after.row_factory = sqlite3.Row

    have = {
        str(r[0]) for r in before.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    counts: dict[str, dict[str, int]] = {}
    for table in TABLES:
        if table not in have:
            continue
        sql = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - our own table names
        b = int(before.execute(sql).fetchone()[0])
        a = int(after.execute(sql).fetchone()[0])
        counts[table] = {"before": b, "after": a, "written": a - b}

    new_filings = []
    if counts["sec_filings"]["written"] > 0:
        new_ids = _ids(after, "sec_filings") - _ids(before, "sec_filings")
        if new_ids:
            marks = ",".join("?" * len(new_ids))
            # ``marks`` holds only question marks; the ids are bound, never interpolated.
            sql = (
                "SELECT a.ticker, a.cik, f.form, f.fiscal_period, f.accession_number, "  # noqa: S608 - marks are question marks only
                "f.filing_date, f.period_end FROM sec_filings f JOIN assets a "
                "ON a.id = f.asset_id WHERE f.id IN (" + marks + ")"
            )
            new_filings = [
                {
                    "ticker": r["ticker"],
                    "cik": r["cik"],
                    "form": r["form"],
                    "fiscal_period": r["fiscal_period"],
                    "accession_number": r["accession_number"],
                    "filing_date": r["filing_date"],
                    "period_end": r["period_end"],
                }
                for r in after.execute(
                    # ``marks`` holds only question marks: the ids are bound, never interpolated.
                    sql,
                    tuple(new_ids),
                )
            ]

    new_metrics = counts.get("fundamental_metrics", {}).get("written", 0)

    doc = {
        "before": args.before,
        "after": args.after,
        "table_counts": counts,
        "new_filings": new_filings,
        "new_filings_count": len(new_filings),
        "new_metrics_rows": new_metrics,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(doc, indent=1, sort_keys=True, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
