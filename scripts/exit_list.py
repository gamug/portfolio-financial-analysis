"""The exit list for T-100's evaluation step (T-153 part 3c, brief section 4).

Facts only: 3c decides nothing from them. Cached data only -- the SEC submissions cache, the
universe file, the Tiingo manifest and the gateway coverage measurement of 2026-10-15. No SEC
request, and no filing document is read (the SEC's own `items` column, not the document).

    uv run python scripts/exit_list.py --universe U.db --coverage coverage.json \
        --manifest leaver_prices.manifest.json --db ARCHIVE --out docs/exit_list.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sec_xcheck.common import SecClient, cache_dir

from kg_schema.db import connect_ro
from kg_schema.queries import connect_ro as connect_ro_universe
from kg_schema.trading_calendar import is_trading_day, last_session_on_or_before

WINDOW = ("2022-01-03", "2026-10-05")
COLUMNS = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "items")
# the failures: classed from the trading halt while members, never from a filing (brief section 4)
FAILURES = {
    "SIVB": "failure (FDIC receivership 2023-03-10; halted while a member)",
    "SBNY": "failure (FDIC receivership 2023-03-12; halted while a member)",
    "FRC": "failure (FDIC receivership 2023-05-01; halted while a member)",
}
FDIC = {"SBNY", "FRC"}  # filed with the FDIC, not the SEC: no exit filing on EDGAR
SPECIAL = {
    "DISCK": "other (a share class of WBD's CIK, converted into WBD at the 2022-04-08 deal)",
    "INFO": "acquisition (IHS Markit by S&P Global)",
    "WRK": "acquisition (Westrock into Smurfit Westrock)",
}
EXIT_LATER_GAP = 5  # more than this many sessions after the last membership session: exit later
BOUNDARY_GAP = (2, 5)  # inclusive: reported, not classified
HALT_NOTE = {
    "SIVB": "its only 2.01 8-K reports a bond-portfolio sale, and its 3.01 comes after the halt",
}


def rows_with_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    """The columnar submissions block as dicts, keeping the SEC's own `items` column."""
    n = len(block.get("accessionNumber", []))
    return [{c: (block.get(c) or [None] * n)[i] for c in COLUMNS} for i in range(n)]


def all_rows(client: SecClient, cik: str) -> list[dict[str, Any]]:
    doc = client.get("submissions", cik)
    if not doc:
        return []
    rows = rows_with_items(doc.get("filings", {}).get("recent", {}))
    for page in doc.get("filings", {}).get("files", []):
        try:
            got = client.get_file("submissions", page["name"])
        except FileNotFoundError:
            continue
        if got:
            rows.extend(rows_with_items(got))
    return rows


def with_items(
    rows: list[dict[str, Any]], items: set[str], since: str = "2022-01-01", latest: bool = False
) -> dict | None:
    """The first (or last) 8-K whose `items` carry one of *items*."""
    hits = [
        r
        for r in rows
        if r["form"] == "8-K"
        and r["filingDate"] >= since
        and any(i in str(r.get("items") or "").split(",") for i in items)
    ]
    if not hits:
        return None
    pick = max if latest else min
    return pick(hits, key=lambda r: (r["filingDate"], r["accessionNumber"]))


def earliest(rows: list[dict[str, Any]], forms: set[str], since: str = "2022-01-01") -> dict | None:
    hits = [r for r in rows if r["form"] in forms and r["filingDate"] >= since]
    return min(hits, key=lambda r: (r["filingDate"], r["accessionNumber"])) if hits else None


def latest(rows: list[dict[str, Any]], forms: set[str]) -> dict | None:
    hits = [r for r in rows if r["form"] in forms]
    return max(hits, key=lambda r: (r["filingDate"], r["accessionNumber"])) if hits else None


def sessions_between(after: str, up_to: str) -> int:
    """NYSE sessions strictly after session(*after*) and up to session(*up_to*)."""
    day = last_session_on_or_before(date.fromisoformat(after)) + timedelta(days=1)
    end = last_session_on_or_before(date.fromisoformat(up_to))
    n = 0
    while day <= end:
        n += int(is_trading_day(day))
        day += timedelta(days=1)
    return n


def span_of(spans: dict[str, list[dict[str, Any]]], cik: str, symbols: list[str]) -> dict[str, Any]:
    """The line's own membership span: by its symbols (DISCA/WBD and DISCK share a CIK)."""
    ends = [
        s["valid_to"]
        for sym in symbols
        for s in spans.get(sym, [])
        if s["cik"] == cik and s["valid_to"]
    ]
    return {"valid_to": max(ends) if ends else None}


@dataclass(frozen=True)
class Facts:
    """The cached inputs the per-line classification needs."""

    spans: dict[str, list[dict[str, Any]]]
    by_line: dict[tuple[str, str], dict[str, Any]]
    client: SecClient
    failures: dict[str, str]
    special: dict[str, str]
    fdic: set[str]


def classify_candidates(
    candidates: list[dict[str, Any]],
    facts: Facts,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The exit list, the "left the index, exit later" table and the boundary cases."""
    spans, by_line = facts.spans, facts.by_line
    client, failures, special, fdic = facts.client, facts.failures, facts.special, facts.fdic
    exits: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    boundary: list[dict[str, Any]] = []
    for ln in candidates:
        ask, cik = str(ln["ask_for"]), str(ln["cik"])
        rows = all_rows(client, cik)
        span = span_of(spans, cik, ln["symbols"])
        last_member = span.get("valid_to")
        man = by_line.get((ask, cik))
        completion = with_items(rows, {"2.01"})
        # the exit's own event: the last 8-K carrying Item 2.01 or 3.01 (a company may have
        # disposed of a division earlier; SEE's is the 2026 completion, not its 2023 one). A
        # special case (DISCK) and a failure are classed from their own halt/exit instead, so
        # their first such 8-K is the one that belongs to them (WBD's 2026 filing is not DISCK's).
        event = with_items(rows, {"2.01", "3.01"}, latest=not (ask in failures or ask in special))
        if ask in failures or ask in special:
            # its own deal, not a later filing on the same CIK (WBD's 2026 accession is not DISCK's)
            own = [
                r
                for r in rows
                if r["form"] in ("DEFM14A", "DEF 14A", "S-4")
                and (last_member is None or r["filingDate"] <= last_member)
            ]
            terms = max(own, key=lambda r: (r["filingDate"], r["accessionNumber"])) if own else None
        else:
            terms = latest(rows, {"DEFM14A", "DEF 14A", "S-4"})
        record: dict[str, Any] = {
            "line": ask,
            "cik": cik,
            "symbols": ln["symbols"],
            "last_membership_day": last_member,
            "last_membership_session": (
                last_session_on_or_before(date.fromisoformat(last_member)).isoformat()
                if last_member
                else None
            ),
            "last_trade_date": man["last_date"] if man else "no price held",
            "cause": failures.get(ask)
            or special.get(ask)
            or ("acquisition" if completion else "other (no completion 8-K with Item 2.01)"),
            "terms_accession": (
                "filed with the FDIC"
                if ask in fdic
                else (terms or event or completion or {}).get("accessionNumber") or "not cached"
            ),
            "event_8k": None
            if event is None
            else {
                "filed": event["filingDate"],
                "report": event["reportDate"],
                "items": event["items"],
                "accession": event["accessionNumber"],
            },
            "completion_8k_2.01": None
            if completion is None
            else {"filed": completion["filingDate"], "accession": completion["accessionNumber"]},
            "25-NSE": (latest(rows, {"25-NSE"}) or {}).get("filingDate"),
            "form_15": (latest(rows, {"15-12G", "15-15D", "15-12B"}) or {}).get("filingDate"),
            "tiingo_missing_sessions": (man or {}).get("missing_sessions", []),
            "tiingo_trimmed_rows": (man or {}).get("trimmed_rows", []),
            "halt_note": HALT_NOTE.get(ask),
        }
        if ask in failures or ask in special:
            # a failure is classed from the halt while a member, never from a filing (its 3.01 can
            # come after the halt, and SIVB's only 2.01 reports a bond-portfolio sale); DISCK is a
            # share class of WBD's CIK, so WBD's own 2026 filings are not its exit
            exits.append(record)
            continue
        if event and event.get("reportDate") and last_member:
            gap = sessions_between(last_member, str(event["reportDate"]))
            record["sessions_from_last_membership_session_to_event"] = gap
            if gap > EXIT_LATER_GAP:
                later.append(record)
                continue
            if BOUNDARY_GAP[0] <= gap <= BOUNDARY_GAP[1]:
                boundary.append(record)
                continue
        exits.append(record)
    return exits, later, boundary


def archive_check(
    coverage: dict[str, Any], company_end: dict[str, str], db: str
) -> tuple[int, list[str]]:
    """The rows each leaver company's gateway series holds after its last membership session."""
    total = 0
    with_rows: list[str] = []
    with contextlib.closing(connect_ro(db)) as arch:
        for ln in coverage["detail"]:
            cik = str(ln["cik"])
            end = company_end.get(cik) or WINDOW[1]
            last = last_session_on_or_before(date.fromisoformat(end)).isoformat()
            n = 0
            for r in arch.execute("SELECT id FROM assets WHERE cik = ?", (cik,)):
                n += int(
                    arch.execute(
                        "SELECT COUNT(*) FROM price_observation WHERE asset_id = ? AND obs_date > ?",
                        (int(r["id"]), last),
                    ).fetchone()[0]
                )
            total += n
            if n:
                with_rows.append(f"{ln['ask_for']}:{n}")
    return total, sorted(set(with_rows))


def served_25nse_lines(served: list[dict[str, Any]], client: SecClient) -> list[dict[str, Any]]:
    """The served lines with a 25-NSE since 2022 (security not read: it may be a bond)."""
    out = []
    for ln in served:
        hits = [
            r
            for r in all_rows(client, str(ln["cik"]))
            if r["form"] == "25-NSE" and r["filingDate"] >= "2022-01-01"
        ]
        if hits:
            out.append(
                {
                    "line": ln["ask_for"],
                    "status": ln["status"],
                    "filing_dates": sorted({r["filingDate"] for r in hits}),
                    "accessions": sorted({r["accessionNumber"] for r in hits}),
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", required=True)
    parser.add_argument("--coverage", required=True, help="leaver_price_coverage_2019.json")
    parser.add_argument("--manifest", required=True, help="leaver_prices.manifest.json")
    parser.add_argument("--db", required=True, help="the archive (read-only)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", default=None)
    args = parser.parse_args(argv)

    coverage = json.loads(Path(args.coverage).read_text())
    manifest = json.loads(Path(args.manifest).read_text())
    # the manifest is keyed by (ask, cik): DISCA/WBD and DISCK share a CIK
    by_line = {(ln["ask"], ln["cik"]): ln for ln in manifest["lines"]}
    client = SecClient(Path(args.cache) if args.cache else cache_dir(), offline=True)

    with contextlib.closing(connect_ro_universe(args.universe)) as univ:
        spans: dict[str, list[dict[str, Any]]] = {}
        for r in univ.execute("SELECT symbol, cik, valid_from, valid_to FROM universe_membership"):
            spans.setdefault(str(r["symbol"]), []).append(
                {"cik": str(r["cik"]), "valid_to": str(r["valid_to"]) if r["valid_to"] else None}
            )

    candidates = [ln for ln in coverage["detail"] if ln["status"] == "zero"]
    served = [ln for ln in coverage["detail"] if ln["status"] != "zero"]

    # per company (CIK): its last membership session, from the longest of its lines' spans
    company_end: dict[str, str] = {}
    for entries in spans.values():
        for s in entries:
            if s["valid_to"] and (
                s["cik"] not in company_end or s["valid_to"] > company_end[s["cik"]]
            ):
                company_end[s["cik"]] = s["valid_to"]
    archive_rows_after, archive_companies_with_rows = archive_check(coverage, company_end, args.db)
    served_25nse = served_25nse_lines(served, client)
    exits, later, boundary = classify_candidates(
        candidates, Facts(spans, by_line, client, FAILURES, SPECIAL, FDIC)
    )

    out: dict[str, Any] = {
        "scope": list(WINDOW),
        "gateway_measurement": coverage.get("measured_at"),
        "served_lines_kept_trading": [
            {"line": ln["ask_for"], "status": ln["status"], "served_last": ln["served_last"]}
            for ln in served
        ],
        "served_25nse": served_25nse,
        "candidates": [ln["ask_for"] for ln in candidates],
        "exits": exits,
        "left_index_exit_later": later,
        "boundary_cases": boundary,
        "archive_rows_after_last_membership_session": archive_rows_after,
        "archive_companies_with_rows_after": sorted(set(archive_companies_with_rows)),
        "notes": [
            "a line the gateway served in the 2026-10-15 measurement kept trading: it is not an "
            "exit, and the archive cannot show it (its gateway series for leavers end at the last "
            "membership day)",
            "GPS and FBHS are served as GAP and FBIN: renames, not exits",
            "a 25-NSE does not prove a stock exit: it can delist a bond or preferred shares, or "
            "record a move to another exchange. The served lines with one since 2022 are in "
            "served_25nse, security not read",
            "the exit-later test uses the 8-K's event date (reportDate) for Item 2.01 or 3.01 and "
            "counts NYSE sessions from the last membership session; C11 printed p.6 allows 'on or "
            "around' the last trading date, so a gap of one session is not 'later'",
            "the event date is never the last trade date: WBD's says 2026-09-30 but it traded "
            "until 2026-10-05",
            "INFO and WRK: no price is held; their event and 25-NSE dates are the filing facts",
        ],
    }
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(
        f"candidates {len(candidates)}; exits {len(exits)}; exit later {len(later)}; "
        f"boundary {len(boundary)}; served {len(served)}; 25-NSE since 2021: {len(served_25nse)}; "
        f"archive rows after a last membership session: {archive_rows_after}"
    )
    for r in later:
        print(
            f"  exit later {r['line']}: last membership session {r['last_membership_session']} "
            f"event {r['event_8k']['report']} gap {r['sessions_from_last_membership_session_to_event']}"
        )
    for r in boundary:
        print(
            f"  boundary {r['line']}: {r['last_membership_session']} -> "
            f"{r['event_8k']['report']} gap {r['sessions_from_last_membership_session_to_event']}"
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
