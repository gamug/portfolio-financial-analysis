"""D6 (T-153 part 3a): the leavers' Article 9/7 statement-form evidence, staged for part 3b.

    uv run python scripts/leaver_company_types.py --universe-db PATH --out FILE [--fetch]

Read-only. For every company that left the S&P 500 inside the backtest window, it finds the
last 10-K filed on or before its exit (the SEC submissions file) and reads the filing's own
statement that the filer is a bank, financial or savings-and-loan holding company -- the same
method ``scripts/sec_xcheck/company_types.py`` uses (``HOLDING_PHRASE``, ``SELF_STATEMENT``,
``NOT_THE_FILER``, ``NEGATED``, via ``article_9_statement``).

The rows are **staged**, not merged into ``docs/checklist_sec/company_types.csv``: the Article 7
(insurer) and other-financial types are keyed on GICS, which part 3b adds with the leavers'
classification. This file is the evidence; part 3b merges it. The leaver's profile interval
(``valid_to``) is part 3b's too, so ``company_profile.csv`` is unchanged here.

``--fetch`` downloads the filing documents into the gitignored ``SEC_XCHECK_CACHE``; without it
only already-cached documents are read (and a missing one is recorded, not fetched).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient, cache_dir, cik10  # noqa: E402
from sec_xcheck.company_types import article_9_statement, strip_html  # noqa: E402
from sec_xcheck.submissions import all_filings  # noqa: E402

from kg_schema.queries import WINDOW_START, connect_ro  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "checklist_sec" / "company_types_leavers.csv"
FIELDS = [
    "cik",
    "symbols",
    "company_name",
    "exit_date",
    "last_10k_accession",
    "last_10k_filed",
    "article_9",
    "evidence_quote",
    "note",
]


def leaver_ciks(universe: Any, window_start: str = WINDOW_START) -> list[dict[str, str]]:
    """One entry per CIK that left the index inside the window: its symbols and last exit day."""
    rows = universe.execute(
        "SELECT symbol, security, cik, valid_from, valid_to FROM universe_membership "
        "WHERE cik IS NOT NULL AND valid_to IS NOT NULL AND valid_to >= ? "
        "ORDER BY valid_to, symbol",
        (window_start,),
    ).fetchall()
    open_ciks = {
        str(r["cik"])
        for r in universe.execute(
            "SELECT DISTINCT cik FROM universe_membership "
            "WHERE cik IS NOT NULL AND valid_to IS NULL"
        )
    }
    grouped: dict[str, dict[str, str]] = {}
    for r in rows:
        cik = str(r["cik"])
        if cik in open_ciks:
            continue
        entry = grouped.setdefault(
            cik,
            {
                "cik": cik,
                "symbols": "",
                "company_name": str(r["security"]),
                "exit_date": str(r["valid_to"]),
            },
        )
        entry["symbols"] = "/".join(p for p in (entry["symbols"], str(r["symbol"])) if p)
        entry["exit_date"] = max(entry["exit_date"], str(r["valid_to"]))
        entry["company_name"] = str(r["security"])  # the latest stint's name
    return sorted(grouped.values(), key=lambda e: e["cik"])


def last_10k(filings: list[dict[str, Any]], exit_date: str) -> dict[str, Any] | None:
    """The last 10-K filed on or before *exit_date*."""
    ks = sorted(
        (f for f in filings if f["form"] == "10-K" and (f["filingDate"] or "") <= exit_date),
        key=lambda f: (f["filingDate"], f["accessionNumber"]),
    )
    return ks[-1] if ks else None


def evidence(client: Any, cik: str, filing: dict[str, Any]) -> str | None:
    """The Article 9 statement of the 10-K's primary document, or ``None``."""
    primary = client.get_document(cik, filing["accessionNumber"], filing["primaryDocument"])
    return article_9_statement(strip_html(primary)) if primary else None


def build(
    universe_path: str, client: Any, window_start: str = WINDOW_START
) -> list[dict[str, str]]:
    """The staged rows: one per leaver CIK, with its last 10-K and the Article 9 statement."""
    with contextlib.closing(connect_ro(universe_path)) as universe:
        leavers = leaver_ciks(universe, window_start)
    rows: list[dict[str, str]] = []
    for entry in leavers:
        # Back far enough for a company that left in early 2022, whose last 10-K is from 2021.
        filings = all_filings(client, entry["cik"], "2019-01-01") or []
        filing = last_10k(filings, entry["exit_date"])
        quote, accession, filed, note = "", "", "", "no 10-K on or before the exit"
        if filing is not None:
            accession = filing["accessionNumber"]
            filed = filing["filingDate"]
            try:
                quote = evidence(client, cik10(entry["cik"]), filing) or ""
            except FileNotFoundError as exc:  # not cached and not fetching
                note = str(exc)
            else:
                note = "" if quote else "no Article 9 statement found in the 10-K"
        rows.append(
            {
                **entry,
                "last_10k_accession": accession,
                "last_10k_filed": filed,
                "article_9": "true" if quote else "false",
                "evidence_quote": quote.replace("\n", " "),
                "note": note,
            }
        )
    return rows


def dump(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    buffer.write(
        "# Generated by scripts/leaver_company_types.py from the SEC submissions files -- staged for\n"
        "# part 3b, which adds the leavers' GICS and merges them into company_types.csv. Not read by\n"
        "# any runtime code (T-153 part 3a, D6).\n"
    )
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universe-db", required=True, help="universe_history.db (read-only)")
    ap.add_argument("--window-start", default=WINDOW_START)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--fetch", action="store_true", help="download filing documents into SEC_XCHECK_CACHE"
    )
    args = ap.parse_args()

    client = SecClient(cache_dir(), offline=not args.fetch)
    rows = build(args.universe_db, client, args.window_start)
    Path(args.out).write_text(dump(rows))
    found = sum(1 for r in rows if r["article_9"] == "true")
    print(f"{len(rows)} leaver CIKs: {found} with an Article 9 statement; wrote {args.out}")
    for row in rows:
        if row["article_9"] == "true" or row["note"]:
            print(f"  {row['cik']} {row['symbols']:14} {row['article_9']:5} {row['note']}")
    if os.environ.get("SEC_XCHECK_CACHE"):
        print(f"cache {os.environ['SEC_XCHECK_CACHE']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
