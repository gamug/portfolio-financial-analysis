#!/usr/bin/env python3
"""D10 window test: would a ``reverse_split_spinoff`` event refuse any cap part A computes?

    uv run python scripts/combined_event_window_test.py --db PATH

Read-only (``mode=ro``). For every asset with a ``reverse_split_spinoff`` event in the tracked list
and every one of its filings the database stores, this applies the **two windows**
``kg_schema.market_cap`` uses to a ``combined_corporate_action`` refusal:

* the **split window**: ``ex_date > count_date`` and no later than the newest stored bar;
* the **spin-off window**: ``ex_date > close_date`` and no later than the newest stored bar.

Two choices, because the copy stores no ``filing_cover_shares``:

* the filing's **period end** stands in for the cover count's ``as_of_date`` -- a cover count is
  dated on or after it, so this can only over-count a case, never miss one;
* the **last stored close on or before the filing's period end** stands in for the close
  ``fundamental_agent`` pairs the filing with (it passes ``target.period.date`` to
  ``market_caps_as_of``).

**Part A's scope** is defined as every ``sec_filings`` row the copy stores for the asset (the
archive copy was built at T-153 part 3a's D-10 window, filings from 2022-01-01; DD's earliest
stored period end is 2021-12-31, filed 2022-02-11).

This is a **window test, not the refusal count**: the copy has no cover counts, so
``market_caps_as_of`` stops at ``no_cover_shares`` before it reaches the new check. The real
refusal count is measured in part A's own run (its V1), where cover counts exist.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Any

from portfolio_common.db import DatabaseError

from kg_schema.corporate_actions import REVERSE_SPLIT_SPINOFF, CorporateActionEvent, read_events
from kg_schema.db import connect_ro

# Static literal queries only: no identifier is interpolated (constitution Code & Git #10).
_ASSETS_SQL = "SELECT id, ticker FROM assets WHERE cik = ?"
_FILINGS_SQL = (
    "SELECT period_end, filing_date FROM sec_filings WHERE asset_id = ? ORDER BY period_end"
)
_LAST_CLOSE_SQL = (
    "SELECT date FROM price_daily WHERE asset_id = ? AND date <= ? ORDER BY date DESC LIMIT 1"
)
_BASIS_END_SQL = "SELECT MAX(date) FROM price_daily WHERE asset_id = ?"


def window_test(
    db_path: str | Path, events: tuple[CorporateActionEvent, ...] | None = None
) -> list[dict[str, Any]]:
    """One row per (combined-event asset, stored filing): its dates and whether it would refuse."""
    listed = events if events is not None else read_events()
    combined = [e for e in listed if e.event_class == REVERSE_SPLIT_SPINOFF]
    out: list[dict[str, Any]] = []
    with contextlib.closing(connect_ro(db_path)) as db:
        for event in combined:
            try:
                assets = db.execute(_ASSETS_SQL, (event.cik,)).fetchall()
            except DatabaseError:
                continue
            for asset in assets:
                asset_id = int(asset["id"])
                ticker = str(asset["ticker"] or "")
                try:
                    basis_end = db.execute(_BASIS_END_SQL, (asset_id,)).fetchone()[0]
                    filings = db.execute(_FILINGS_SQL, (asset_id,)).fetchall()
                except DatabaseError:
                    continue
                for filing in filings:
                    period_end = str(filing["period_end"])
                    close = db.execute(_LAST_CLOSE_SQL, (asset_id, period_end)).fetchone()
                    close_date = str(close[0]) if close and close[0] else None
                    within_bound = basis_end is None or event.ex_date <= str(basis_end)
                    split_window = within_bound and event.ex_date > period_end
                    spinoff_window = (
                        within_bound and close_date is not None and event.ex_date > close_date
                    )
                    out.append(
                        {
                            "ticker": ticker,
                            "cik": event.cik,
                            "ex_date": event.ex_date,
                            "period_end": period_end,
                            "close_date": close_date,
                            "refused": split_window or spinoff_window,
                        }
                    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="a KG_FINANCIAL_DB to inspect, read-only")
    args = parser.parse_args(argv)
    rows = window_test(args.db)
    refused = [r for r in rows if r["refused"]]
    print(f"filings tested: {len(rows)}; refused by the window test: {len(refused)}")
    for r in refused:
        print(
            f"  {r['ticker']} {r['ex_date']} period_end {r['period_end']} close {r['close_date']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
