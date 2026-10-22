#!/usr/bin/env python3
"""Why the gateway's payload has no quarter column for some 10-Qs (T-143 part A, read-only).

    uv run python scripts/pilot_a/payload_period_columns.py \
        --base-url http://host.docker.internal:8000/edgar/edgar \
        --out /workspaces/thesis/scratch/t143a/out/payload_period_columns.json

Read-only GETs to the EDGAR gateway's ``/financials`` route for the filings step 4 recorded with
``stage=period`` ("the payload has no quarter column of its own"). For each filing it lists the
**duration** column keys the payload's income-statement and cash-flow rows carry (the keys
``fundamental_agent.statements`` parses as ``<date> (FY|Qn|YTD)``) and the balance sheet's
**instant** keys -- so the reason is measured, not guessed. Dates, keys and counts only.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import httpx

DURATION_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*\([A-Za-z0-9]+\)\s*$")
INSTANT_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")

#: (ticker, cik, form, year, accession) -- the filings step 4 could not place in a quarter.
FILINGS = [
    ("AAP", "0001158449", "10-Q", 2022, "0001158449-22-000176"),
    ("AAP", "0001158449", "10-Q", 2023, "0001158449-23-000142"),
    ("AEP", "0000004904", "10-Q", 2023, "0000004904-23-000072"),
    ("APO", "0001858681", "10-Q", 2023, "0001858681-23-000017"),
    ("ARES", "0001628280", "10-Q", 2023, "0001628280-23-016241"),
    ("BXP", "0001037540", "10-Q", 2026, "0001037540-26-000019"),
    ("BXP", "0001037540", "10-Q", 2026, "0001037540-26-000034"),
    ("CTLT", "0001596783", "10-Q", 2022, "0001596783-22-000027"),
    ("CTLT", "0001596783", "10-Q", 2022, "0001596783-22-000039"),
    ("DELL", "0001571996", "10-Q", 2023, "0001571996-23-000019"),
    ("DELL", "0001571996", "10-Q", 2023, "0001571996-23-000032"),
    ("DRE", "0000783280", "10-Q", 2022, "0000783280-22-000033"),
    ("DRE", "0000783280", "10-Q", 2022, "0000783280-22-000054"),
]


def keys(rows: list[dict[str, object]]) -> list[str]:
    out: set[str] = set()
    for row in rows:
        for key in row:
            if isinstance(key, str) and (DURATION_RE.match(key) or INSTANT_RE.match(key)):
                out.add(key.strip())
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    detail = []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        for ticker, cik, form, year, accession in FILINGS:
            params = {"form": form, "year": year, "accession_number": accession}
            response = client.get(f"/financials/{cik}", params=params)
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            row: dict[str, object] = {
                "ticker": ticker,
                "cik": cik,
                "form": form,
                "year": year,
                "accession": accession,
                "http": response.status_code,
                "success": payload.get("success") if isinstance(payload, dict) else None,
            }
            if isinstance(data, dict):
                row["income_duration_keys"] = keys(data.get("income_statement") or [])
                row["cash_flow_duration_keys"] = keys(data.get("cash_flow") or [])
                row["balance_instant_keys"] = keys(data.get("balance_sheet") or [])
                row["income_rows"] = len(data.get("income_statement") or [])
                row["balance_rows"] = len(data.get("balance_sheet") or [])
                row["cash_flow_rows"] = len(data.get("cash_flow") or [])
                row["reconciliation_errors"] = data.get("reconciliation_errors")
            else:
                row["raw_error"] = str(payload)[:300]
            detail.append(row)
            print(json.dumps(row, sort_keys=True))

    Path(args.out).write_text(json.dumps({"detail": detail}, indent=1, sort_keys=True) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
