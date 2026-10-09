"""The SEC ``submissions`` API as a list of filings: the recent block plus the older pages.

``https://data.sec.gov/submissions/CIK##########.json`` carries the ~1,000 most recent filings inline and names
further pages in ``filings.files``; a heavy filer's 1,000 most recent filings (Form 4s) may span under two years, so the
pages that reach back to ``since`` are read too. Every response is cached by :class:`~sec_xcheck.common.SecClient`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import SecClient, cik10

COLUMNS = (
    "accessionNumber",
    "form",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "primaryDocument",
)


def rows_of(block: dict[str, Any]) -> list[dict[str, Any]]:
    """The columnar ``recent`` block (parallel arrays) as one dict per filing."""
    n = len(block.get("accessionNumber", []))
    return [{c: (block.get(c) or [None] * n)[i] for c in COLUMNS} for i in range(n)]


def page_names(doc: dict[str, Any], since: str) -> list[str]:
    """The older pages whose date range reaches *since* (``YYYY-MM-DD``)."""
    return [
        p["name"]
        for p in doc.get("filings", {}).get("files", [])
        if p.get("filingTo", "9999") >= since
    ]


def all_filings(
    client: SecClient, cik: str | int, since: str = "2021-12-01"
) -> list[dict[str, Any]] | None:
    """Every filing of *cik* filed on or after *since*; ``None`` when the SEC has no such CIK."""
    doc = client.get("submissions", cik)
    if doc is None:
        return None
    out = rows_of(doc.get("filings", {}).get("recent", {}))
    for name in page_names(doc, since):
        page = client.get_file("submissions", name)
        if page:
            out.extend(rows_of(page))
    return [r for r in out if (r["filingDate"] or "") >= since]


def forms_by_accession(filings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["accessionNumber"]: r for r in filings}


def cached_doc_name(cik: str | int) -> str:
    return f"CIK{cik10(cik)}.json"


def dump(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, indent=1)
