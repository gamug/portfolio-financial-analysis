"""T-153 part 3b: every leaver's cover-page symbols over its filings in the window.

    uv run python scripts/leaver_cover_classes.py [--types FILE] [--out FILE] [--fetch]

Read-only. For each leaver CIK it reads the cover page of every 10-K/10-Q it filed inside the
backtest window (up to its exit) and reports the symbols registered there -- the SEC's own
**point-in-time** list, so a change inside the window is visible with the dates. The profile's
multi-class overlay is the set on the **last** 10-K on or before the exit (L-03/L-11, D7); this
measurement is the evidence for every leaver whose set changed, declared in ``docs/model_fixes.md``.

The set is what the cover registers: the ``dei:TradingSymbol`` facts, so a registered preferred or
note series counts beside the common classes -- the same breadth the current members' rule has
(``scripts/sec_xcheck/company_types.py`` counts the submissions file's ticker list). A maturing note
therefore reads as a change; the declaration says so.

Output is committed (``docs/leaver_cover_classes.json``) so the list is reproducible from the repo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from leaver_company_types import cover_symbols  # noqa: E402
from sec_xcheck.common import SecClient, cache_dir, cik10  # noqa: E402
from sec_xcheck.submissions import all_filings  # noqa: E402

DEFAULT_TYPES = REPO / "docs" / "leaver_company_types.csv"
DEFAULT_OUT = REPO / "docs" / "leaver_cover_classes.json"
FORMS = ("10-K", "10-Q")


def changes_over(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The symbol-set changes between consecutive filings (the dates, and what moved)."""
    out: list[dict[str, Any]] = []
    for before, after in pairwise(rows):
        was, now = set(before["symbols"]), set(after["symbols"])
        if was != now:
            out.append(
                {
                    "from_filing": before["filing_date"],
                    "to_filing": after["filing_date"],
                    "to_accession": after["accession"],
                    "added": sorted(now - was),
                    "removed": sorted(was - now),
                }
            )
    return out


def measure(types_path: str, client: Any) -> dict[str, Any]:
    """The whole document: each leaver's filings' cover symbols and its changes."""
    text = Path(types_path).read_text(encoding="utf-8")
    staged = list(
        csv.DictReader(
            io.StringIO("\n".join(line for line in text.splitlines() if not line.startswith("#")))
        )
    )
    detail: list[dict[str, Any]] = []
    filings_read = 0
    for row in staged:
        cik = cik10(row["cik"])
        docs = all_filings(client, cik, "2022-01-01") or []
        rows: list[dict[str, Any]] = []
        for filing in sorted(
            (f for f in docs if f["form"] in FORMS and (f["filingDate"] or "") <= row["exit_date"]),
            key=lambda f: (f["filingDate"], f["accessionNumber"]),
        ):
            symbols = cover_symbols(client, cik, filing)
            if not symbols:
                continue  # no cover facts (a document the cache does not hold)
            rows.append(
                {
                    "accession": filing["accessionNumber"],
                    "form": filing["form"],
                    "filing_date": filing["filingDate"],
                    "symbols": symbols,
                }
            )
        filings_read += len(rows)
        detail.append(
            {
                "cik": row["cik"],
                "symbols": row["symbols"],
                "exit_date": row["exit_date"],
                "cover_symbols_last_10k": row["cover_symbols"],
                "filings": rows,
                "changes": changes_over(rows),
            }
        )
    changed = [entry for entry in detail if entry["changes"]]
    return {
        "types": str(types_path),
        "leavers": len(detail),
        "filings_read": filings_read,
        "leavers_with_a_change": len(changed),
        "detail": detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--types", default=str(DEFAULT_TYPES), help="the staged leaver evidence")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--fetch", action="store_true", help="download missing documents into the cache"
    )
    args = ap.parse_args()

    client = SecClient(cache_dir(), offline=not args.fetch)
    report = measure(args.types, client)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(
        f"{report['leavers']} leavers, {report['filings_read']} filings read: "
        f"{report['leavers_with_a_change']} whose cover-page symbol set changed in the window"
    )
    for entry in report["detail"]:
        if not entry["changes"]:
            continue
        print(
            f"  {entry['symbols']:16} exit {entry['exit_date']}  last 10-K cover: {entry['cover_symbols_last_10k']}"
        )
        for change in entry["changes"]:
            print(
                f"      {change['from_filing']} -> {change['to_filing']} ({change['to_accession']}):"
                f" added {change['added'] or '-'}, removed {change['removed'] or '-'}"
            )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
