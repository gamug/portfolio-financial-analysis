#!/usr/bin/env python3
"""T-153 part 3d (D10): every recorded SPLIT row the tracked event list does not explain.

    uv run python scripts/unexplained_split_rows.py --db PATH

Read-only (``mode=ro``): the pilot's part A and ``T-100`` run it as a gate after their own
``quant backfill-actions --from 2019-01-02``. An unexplained SPLIT row refuses the market cap
(``kg_schema.market_cap``) and stops the Tiingo import, so the list
(``src/kg_schema/data/corporate_action_events.csv``) must cover every row. Prints counts, tickers
and dates only; exits 1 when any row is unexplained, 0 when the list explains them all.

**Fail closed** (reviewer B, fix round 2): only a database with no ``corporate_action`` table is
empty (nothing to explain); any other read error propagates and the script exits non-zero, so the
gate can never PASS without having read the table.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any

from portfolio_common.db import DatabaseError

from kg_schema.corporate_actions import CorporateActionEvent, explained, read_events
from kg_schema.db import connect_ro

# A static literal query: no identifier is interpolated (constitution Code & Git #10). Every SPLIT
# row counts, whatever engine recorded it (the gateway's or the private import's).
_SPLIT_ROWS_SQL = (
    "SELECT c.ex_date, c.value, c.engine_version, a.ticker, a.cik "
    "FROM corporate_action c LEFT JOIN assets a ON a.id = c.asset_id "
    "WHERE c.action_type = 'SPLIT' ORDER BY c.ex_date, a.ticker"
)


def unexplained_split_rows(
    db_path: str | Path, events: tuple[CorporateActionEvent, ...] | None = None
) -> list[dict[str, Any]]:
    """Every SPLIT row of ``db_path`` the event list cannot explain (read-only).

    Fail closed: only a database with no ``corporate_action`` table returns empty; any other read
    error propagates. A row whose asset has no ``assets`` row reports a blank CIK and is
    unexplained, never guessed (MKT-02)."""
    listed = events if events is not None else read_events()
    with contextlib.closing(connect_ro(db_path)) as db:
        if not db.relation_exists("corporate_action"):
            return []
        rows = db.execute(_SPLIT_ROWS_SQL).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        cik = str(r["cik"] or "")
        ex_date = str(r["ex_date"])
        value = float(r["value"])
        if explained(listed, cik=cik, ex_date=ex_date, value=value) is None:
            out.append(
                {
                    "ticker": str(r["ticker"] or ""),
                    "cik": cik,
                    "ex_date": ex_date,
                    "value": value,
                    "engine": str(r["engine_version"] or ""),
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="a KG_FINANCIAL_DB to inspect, read-only")
    args = parser.parse_args(argv)
    try:
        rows = unexplained_split_rows(args.db)
    except DatabaseError as exc:
        # Fail closed: a database the gate could not read is not an all-clear.
        print(f"error: cannot read SPLIT rows from {args.db}: {exc}", file=sys.stderr)
        return 1
    print(f"unexplained SPLIT rows: {len(rows)}")
    for r in rows:
        print(f"  {r['ticker']} {r['ex_date']}")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
