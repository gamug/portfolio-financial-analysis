"""The leavers' APP-00 evidence and point-in-time facts, staged for the company profile.

    uv run python scripts/leaver_company_types.py --universe-db PATH --out FILE [--fetch]

Read-only. For every company that left the S&P 500 inside the backtest window it records:

* the **Article 9 statement** of the last 10-K filed on or before its exit (the SEC submissions
  file): the same method ``scripts/sec_xcheck/company_types.py`` uses (``HOLDING_PHRASE``,
  ``SELF_STATEMENT``, ``NOT_THE_FILER``, ``NEGATED``, via ``article_9_statement``);
* the **delivered GICS label** of the stint that ends on its last membership day, with the
  Wikipedia revision it was read from (``universe_history_gics.db``, data-mining PR #51);
* the **share classes registered on that 10-K's cover page** (the inline-XBRL
  ``dei:TradingSymbol`` facts) -- the SEC's own point-in-time class list, the one L-03/L-11's
  "classification in force on the last membership day" calls for (T-153 part 3b, D7). The
  submissions file's ``tickers`` is the company as it is *today*, so it is not used for a leaver.

The rows are the **evidence**; ``scripts/build_company_profile.py`` merges them into the tracked
``src/kg_schema/data/company_profile.csv`` (one row per CIK, each with its own interval) and is the
only place the APP-00 type is assigned. This file is not read by any runtime code.

``--fetch`` downloads filing documents into the gitignored ``SEC_XCHECK_CACHE``; without it only
already-cached documents are read (and a missing one is recorded, not fetched).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html
import io
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient, cache_dir, cik10  # noqa: E402
from sec_xcheck.company_types import article_9_statement, strip_html  # noqa: E402
from sec_xcheck.submissions import all_filings  # noqa: E402

from kg_schema.queries import WINDOW_START, connect_ro  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "leaver_company_types.csv"
FIELDS = [
    "cik",
    "symbols",
    "company_name",
    "exit_date",
    "gics_sector",
    "gics_sub_industry",
    "gics_revision_id",
    "gics_revision_timestamp",
    "last_10k_accession",
    "last_10k_filed",
    "article_9",
    "evidence_quote",
    "cover_symbols",
    "note",
]

#: The XBRL cover page's registered trading symbols (T-153 part 3b, reviewer A): the SEC's own
#: **point-in-time** class list for the CIK, unlike the submissions file's `tickers`, which shows
#: the company as it is today. ``dei:TradingSymbol`` is an inline-XBRL fact on the 10-K's cover.
COVER_SYMBOL = re.compile(r'name\s*=\s*"dei:TradingSymbol"', re.IGNORECASE)
COVER_CLOSE = re.compile(r"</ix:nonNumeric\s*>", re.IGNORECASE)
#: A cover row's symbol is short ("CE /26A" is the longest seen); anything longer is not one.
MAX_COVER_SYMBOL = 24


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


def leaver_labels(universe: Any, window_start: str = WINDOW_START) -> dict[str, dict[str, str]]:
    """The delivered GICS label of each leaver CIK's **last** stint, with its revision.

    ``universe_history_gics.db`` (data-mining PR #51) fills the leaver stints' ``gics_sector`` /
    ``gics_sub_industry``; each carries the Wikipedia revision it was read from. The classification
    in force on the company's last membership day (L-03/L-11, D7) is the label of the stint that
    ends there -- the same stint ``leaver_ciks`` takes the exit from."""
    rows = universe.execute(
        "SELECT symbol, cik, valid_to, gics_sector, gics_sub_industry, "
        "gics_revision_id, gics_revision_timestamp FROM universe_membership "
        "WHERE cik IS NOT NULL AND valid_to IS NOT NULL AND valid_to >= ? "
        "ORDER BY valid_to, symbol",
        (window_start,),
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        cik = str(r["cik"])
        entry = out.setdefault(cik, {"valid_to": ""})
        if str(r["valid_to"]) >= entry["valid_to"]:
            out[cik] = {
                "valid_to": str(r["valid_to"]),
                "gics_sector": str(r["gics_sector"] or ""),
                "gics_sub_industry": str(r["gics_sub_industry"] or ""),
                "gics_revision_id": str(r["gics_revision_id"] or ""),
                "gics_revision_timestamp": str(r["gics_revision_timestamp"] or ""),
            }
    return {cik: {k: v for k, v in entry.items() if k != "valid_to"} for cik, entry in out.items()}


def cover_symbols(client: Any, cik: str, filing: dict[str, Any]) -> list[str]:
    """The trading symbols registered on the 10-K's cover page, in document order (D7).

    The SEC's own **point-in-time** class list: the inline-XBRL ``dei:TradingSymbol`` facts of the
    filing's primary document. Unlike the submissions file's ``tickers`` (which shows the company
    as it is today), this is the cover of the 10-K the company filed while it was still a member.
    One entry per fact (a cover row is one registered class, even when the symbol is written with a
    space), distinct, in first-seen order; empty when the document is not cached."""
    primary = client.get_document(cik, filing["accessionNumber"], filing["primaryDocument"])
    if not primary:
        return []
    out: list[str] = []
    for match in COVER_SYMBOL.finditer(primary):
        gt = primary.find(">", match.end())
        if gt < 0:
            continue
        close = COVER_CLOSE.search(primary, gt)
        body = primary[gt + 1 : close.start()] if close else primary[gt + 1 : gt + 200]
        text = re.sub(r"[\s\u00a0]+", " ", html.unescape(re.sub(r"<[^>]+>", "", body))).strip()
        if text and len(text) <= MAX_COVER_SYMBOL and text.upper() not in {s.upper() for s in out}:
            out.append(text)
    return out


#: Sentences the method cannot pick correctly, verified by hand (T-153 part 3a review,
#: 2026-10-14). Keyed by CIK: the company's own verbatim self-statement, or ``None`` when the
#: method's sentence is not one. Each carries why (``VERIFIED_NOTES``).
#:
#: * **CMA** -- the method took a generic Gramm-Leach-Bliley sentence ("... a bank holding
#:   company registered as a financial holding company can engage"). Comerica's own statement is
#:   in the same 10-K's MD&A overview.
#: * **ZION** -- a **false negative**: since 2018 Zions Bancorporation, N.A. is itself a national
#:   bank, not a holding company, so ``HOLDING_PHRASE`` cannot match; APP-00's Article 9 includes
#:   banks. The method's gap (its regexes assume a holding-company form) is part 3b's.
#: * **DRE** -- the method matched a director's seat on another bank ("Dee serves as a member of
#:   the Board of Directors of First Internet Bancorp, a bank holding company ..."). Duke Realty
#:   is a REIT: ``article_9`` is false.
VERIFIED: dict[str, str | None] = {
    "0000028412": (
        "Comerica Incorporated (the Corporation) is a financial holding company headquartered "
        "in Dallas, Texas."
    ),
    "0000109380": (
        "Zions Bancorporation, National Association (\u201cZions Bancorporation, N.A.,\u201d "
        "\u201cthe Bank,\u201d \u201cwe,\u201d \u201cour,\u201d \u201cus\u201d) is a bank "
        "headquartered in Salt Lake City, Utah with annual net revenue (net interest income and "
        "noninterest income) of $3.1 billion in 2023, and total assets of approximately $87 "
        "billion at December 31, 2023."
    ),
    "0000783280": None,
}
VERIFIED_NOTES: dict[str, str] = {
    "0000028412": "verified by hand: the method's sentence is a generic Gramm-Leach-Bliley one",
    "0000109380": (
        "verified by hand: a national bank since 2018, so HOLDING_PHRASE cannot match "
        "(false negative; the method's gap is part 3b's)"
    ),
    "0000783280": (
        "verified by hand: the method's sentence is a director's seat on another bank; "
        "Duke Realty is a REIT"
    ),
}


def build(
    universe_path: str, client: Any, window_start: str = WINDOW_START
) -> list[dict[str, str]]:
    """The staged rows: one per leaver CIK, with its last 10-K and the Article 9 statement."""
    with contextlib.closing(connect_ro(universe_path)) as universe:
        leavers = leaver_ciks(universe, window_start)
        labels = leaver_labels(universe, window_start)
    rows: list[dict[str, str]] = []
    for entry in leavers:
        # Back far enough for a company that left in early 2022, whose last 10-K is from 2021.
        filings = all_filings(client, entry["cik"], "2019-01-01") or []
        filing = last_10k(filings, entry["exit_date"])
        quote, accession, filed, note = "", "", "", "no 10-K on or before the exit"
        symbols: list[str] = []
        if filing is not None:
            accession = filing["accessionNumber"]
            filed = filing["filingDate"]
            try:
                found = evidence(client, cik10(entry["cik"]), filing)
                symbols = cover_symbols(client, cik10(entry["cik"]), filing)
            except FileNotFoundError as exc:  # not cached and not fetching
                note = str(exc)
            else:
                if entry["cik"] in VERIFIED:
                    quote = VERIFIED[entry["cik"]] or ""
                    note = VERIFIED_NOTES[entry["cik"]]
                else:
                    quote = html.unescape(found) if found else ""
                    note = "" if quote else "no Article 9 statement found in the 10-K"
        rows.append(
            {
                **entry,
                **labels.get(entry["cik"], {}),
                "last_10k_accession": accession,
                "last_10k_filed": filed,
                "article_9": "true" if quote else "false",
                "evidence_quote": quote.replace("\n", " "),
                "cover_symbols": " | ".join(symbols),
                "note": note,
            }
        )
    return rows


def dump(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    buffer.write(
        "# Generated by scripts/leaver_company_types.py from the SEC submissions files, the delivered\n"
        "# universe_history_gics.db and the 10-K cover pages -- the leavers' APP-00 evidence and\n"
        "# point-in-time facts, merged into the company profile by scripts/build_company_profile.py.\n"
        "# Not read by any runtime code (T-153 parts 3a/3b).\n"
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
