"""APP-10b-1 (T-149): the register of succession 8-Ks among the stored companies' filings.

    uv run python scripts/audit_sec_checklist.py successions [--out docs/checklist_sec/succession_8ks.csv]

A successor issuer reports its succession on an 8-K of submission type ``8-K12B``, ``8-K12G3`` or
``8-K15D5`` (or the ``/A`` of one), under Rule 12g-3 / 15d-5 of the Exchange Act. The EDGAR gateway
lists only ``10-K``, ``10-Q``, ``8-K`` and ``S-1`` (its ``form`` is an enum) and returns plain
``8-K``s for ``8-K``, so the pipeline cannot see these filings; the SEC's ``submissions`` list can.
The register records each one -- ticker, CIK, form, filing date, accession -- and **links nothing**:
tying a successor's series to its predecessor's needs the 8-K's own words (APP-10b-2), a hand reading. The
links that were read and are used live in ``src/kg_schema/data/cik_successions.csv`` (XOM only: the one succession
of the 18 whose predecessor filings the by-CIK fetch would lose).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import SecClient, cik_family
from sec_xcheck.submissions import all_filings

SUCCESSION_FORMS = ("8-K12B", "8-K12G3", "8-K15D5", "8-K12B/A", "8-K12G3/A", "8-K15D5/A")
SINCE = "2022-01-01"  # our filings start 2022-01-06
FIELDS = ("ticker", "cik", "filer_cik", "form", "filing_date", "accession_number", "linked_to")


def build_register(conn: Any, client: SecClient, since: str = SINCE) -> list[dict[str, str]]:
    """One row per succession filing of each stored asset's CIK (and its predecessors' CIKs the
    audit already maps), oldest first within a company. ``linked_to`` is always empty."""
    rows: list[dict[str, str]] = []
    assets = conn.execute("SELECT ticker, cik FROM assets WHERE cik IS NOT NULL ORDER BY ticker")
    for ticker, cik in ((r["ticker"], r["cik"]) for r in assets):
        for filer in cik_family(cik):
            for f in all_filings(client, filer, since) or []:
                if f["form"] in SUCCESSION_FORMS:
                    rows.append(
                        {
                            "ticker": ticker,
                            "cik": cik,
                            "filer_cik": filer,
                            "form": f["form"],
                            "filing_date": f["filingDate"],
                            "accession_number": f["accessionNumber"],
                            "linked_to": "",
                        }
                    )
    return sorted(rows, key=lambda r: (r["ticker"], r["filing_date"], r["accession_number"]))


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
