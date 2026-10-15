"""The members that joined **after** the checklist's own table was generated, staged for the profile.

    uv run python scripts/new_member_company_types.py --universe-db PATH [--table FILE] [--out FILE]

Read-only. The checklist's own table (`docs/checklist_sec/company_types.csv`) is generated from a
database's ``assets``, so a company that joined the index after that database was last synced has no
row in it -- the mirror of the leavers, which left after it. Measured 2026-10-15 on
``universe_history_gics.db``: **TWLO** (joined 2026-10-06), **VYLR** (2026-10-01), **BE**, **P** and
**ILMN** (2026-09-21) are open members with no row. ILMN matters most: it left in 2024 and rejoined,
so it is not among the leavers, but it was a member 2022-01..2024-06-23 and its filings in that
window are scored.

Each row carries what the **current-member rule** needs (L-03: today's snapshot, applied backwards):

* the label of its **open** stint (a ``live_snapshot`` row of the delivered file), not a leaver's
  last-day label;
* the **Article 9 statement** of its latest 10-K (the same method the checklist's table uses);
* the **live ticker list** (the SEC submissions file's ``tickers``) -- the current members' rule for
  the multi-class overlay, not a leaver's cover page.

``scripts/build_company_profile.py`` merges the rows into the tracked profile with an **open**
interval (``valid_from`` 2020-01-01, ``valid_to`` empty). ``--fetch`` downloads filing documents into
the gitignored ``SEC_XCHECK_CACHE``; without it only cached documents are read.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import html
import io
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient, cache_dir, cik10  # noqa: E402
from sec_xcheck.company_types import article_9_statement, strip_html  # noqa: E402
from sec_xcheck.submissions import all_filings  # noqa: E402

from kg_schema.queries import connect_ro  # noqa: E402

DEFAULT_OUT = REPO / "docs" / "new_member_company_types.csv"
FIELDS = [
    "cik",
    "symbols",
    "company_name",
    "joined_date",
    "gics_sector",
    "gics_sub_industry",
    "last_10k_accession",
    "last_10k_filed",
    "article_9",
    "evidence_quote",
    "live_tickers",
    "note",
]


def table_ciks(table_path: str) -> set[str]:
    """The CIKs the checklist's own table already holds."""
    text = Path(table_path).read_text(encoding="utf-8")
    rows = csv.DictReader(
        io.StringIO(
            "\n".join(line for line in text.splitlines() if line and not line.startswith("#"))
        )
    )
    return {str(row["cik"]) for row in rows}


def open_members(universe: Any, known: set[str]) -> list[dict[str, str]]:
    """Every **open** stint of the delivered file whose CIK the checklist's table does not hold.

    That is exactly a company that joined the index after the table was generated: the table is built
    from a database's ``assets``, which a later joiner is not in."""
    rows = universe.execute(
        "SELECT symbol, security, cik, valid_from, gics_sector, gics_sub_industry, source "
        "FROM universe_membership WHERE cik IS NOT NULL AND valid_to IS NULL "
        "ORDER BY valid_from, symbol"
    ).fetchall()
    out: list[dict[str, str]] = []
    for r in rows:
        if str(r["cik"]) in known:
            continue
        out.append(
            {
                "cik": str(r["cik"]),
                "symbols": str(r["symbol"]),
                "company_name": str(r["security"]),
                "joined_date": str(r["valid_from"]),
                "gics_sector": str(r["gics_sector"] or ""),
                "gics_sub_industry": str(r["gics_sub_industry"] or ""),
                "source": str(r["source"]),
            }
        )
    return out


def latest_10k(filings: list[dict[str, Any]], on_or_before: str) -> dict[str, Any] | None:
    ks = sorted(
        (f for f in filings if f["form"] == "10-K" and (f["filingDate"] or "") <= on_or_before),
        key=lambda f: (f["filingDate"], f["accessionNumber"]),
    )
    return ks[-1] if ks else None


def build(
    universe_path: str, client: Any, on_or_before: str, table_path: str
) -> list[dict[str, str]]:
    """The staged rows: one per unknown open stint, with its 10-K statement and live tickers."""
    known = table_ciks(table_path)
    with contextlib.closing(connect_ro(universe_path)) as universe:
        members = open_members(universe, known)
    rows: list[dict[str, str]] = []
    for entry in members:
        cik = cik10(entry["cik"])
        docs = all_filings(client, cik, "2019-01-01") or []
        filing = latest_10k(docs, on_or_before)
        quote, accession, filed, note = "", "", "", "no 10-K on or before the date"
        if filing is not None:
            accession, filed = filing["accessionNumber"], filing["filingDate"]
            try:
                found = article_9_statement(
                    strip_html(client.get_document(cik, accession, filing["primaryDocument"]))
                )
            except FileNotFoundError as exc:  # not cached and not fetching
                note = str(exc)
            else:
                quote = html.unescape(found) if found else ""
                note = "" if quote else "no Article 9 statement found in the 10-K"
        tickers = (client.get("submissions", cik) or {}).get("tickers") or []
        rows.append(
            {
                **{k: v for k, v in entry.items() if k != "source"},
                "last_10k_accession": accession,
                "last_10k_filed": filed,
                "article_9": "true" if quote else "false",
                "evidence_quote": quote.replace("\n", " "),
                "live_tickers": "/".join(str(t) for t in tickers),
                "note": note,
            }
        )
    return rows


def dump(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    buffer.write(
        "# Generated by scripts/new_member_company_types.py from the delivered universe file, the SEC\n"
        "# submissions and the 10-Ks -- the members that joined after the checklist's own table was\n"
        "# generated, merged into the company profile by scripts/build_company_profile.py.\n"
        "# Not read by any runtime code (T-153 part 3b).\n"
    )
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universe-db", required=True, help="universe_history_gics.db (read-only)")
    ap.add_argument(
        "--table",
        default=str(REPO / "docs" / "checklist_sec" / "company_types.csv"),
        help="the checklist's own table (its CIKs are already held)",
    )
    ap.add_argument(
        "--on-or-before", default="2026-10-15", help="the date the latest 10-K is read to"
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--fetch", action="store_true", help="download filing documents into the cache")
    args = ap.parse_args()

    client = SecClient(cache_dir(), offline=not args.fetch)
    rows = build(args.universe_db, client, args.on_or_before, args.table)
    Path(args.out).write_text(dump(rows))
    print(f"{len(rows)} open stints -> {args.out}")
    for row in rows:
        print(
            f"  {row['symbols']:6} {row['cik']} joined {row['joined_date']} {row['gics_sector']}/"
            f"{row['gics_sub_industry']} tickers={row['live_tickers']} article_9={row['article_9']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
