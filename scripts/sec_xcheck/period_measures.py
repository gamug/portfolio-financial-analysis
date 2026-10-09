"""Period rules at levels (a) and (b): ID-07, PER-08, PER-09, PER-12 to PER-14 and APP-10b.

* ID-07: the stored ``period_end`` against the EDGAR header ``periodOfReport`` (``submissions`` ``reportDate``).
* PER-08: the length of a filing's own column at the source, and the gap the code accepts between a column and its
  prior (``YEAR_TOLERANCE_DAYS = 20``) against the rule's ranges.
* PER-12 to PER-14: growth and CAGR whose base period precedes the adoption of ASC 842 or ASC 326 (CECL).
* PER-09, APP-10b: transition reports and succession 8-Ks among the CIKs.
"""

from __future__ import annotations

import collections
import datetime as dt
from typing import Any

from sec_xcheck.common import SecClient, cik_family
from sec_xcheck.findings import Finding
from sec_xcheck.records import Rec, count
from sec_xcheck.submissions import all_filings

CMD = "uv run python scripts/audit_sec_checklist.py measure --only periods"
YEAR_RANGE = (364, 371)
QUARTER_RANGE = (89, 98)
CODE_TOLERANCE = 20  # statements.YEAR_TOLERANCE_DAYS
YEAR_DAYS = 365
# Effective dates for public business entities that are SEC filers (not smaller reporting companies):
# ASC 842: fiscal years beginning after 2018-12-15; ASC 326 (CECL): fiscal years beginning after 2019-12-15.
ASC842_FIRST_YEAR_BEGINS = dt.date(2018, 12, 16)
CECL_FIRST_YEAR_BEGINS = dt.date(2019, 12, 16)
LENDER_SUB_INDUSTRIES = {
    "Diversified Banks",
    "Regional Banks",
    "Consumer Finance",
    "Investment Banking & Brokerage",
    "Asset Management & Custody Banks",
    "Commercial & Residential Mortgage Finance",
    "Specialized Finance",
    "Multi-Sector Holdings",
    "Diversified Financial Services",
    "Mortgage REITs",
}
TRANSITION_FORMS = ("10-KT", "10-QT", "10-KT/A", "10-QT/A")
SUCCESSION_FORMS = ("8-K12B", "8-K12G3", "8-K15D5", "8-K12B/A", "8-K12G3/A", "8-K15D5/A")


def gap_outside_rule(r: Rec) -> bool:
    """The prior column the code accepts sits outside the rule's range for a year (FY) or a year-ago quarter."""
    p = r.f["periods"]
    gap = p["prior_gap_days"]
    if gap is None:
        return False
    if p["tag"] == "FY":
        return not (YEAR_RANGE[0] <= gap <= YEAR_RANGE[1])
    return not (
        YEAR_RANGE[0] <= gap <= YEAR_RANGE[1]
    )  # a quarter's prior is the quarter a year earlier


def gap_accepted_by_code_only(r: Rec) -> bool:
    """Accepted by ``prior_of``'s ±20-day window around 365 days but outside the rule's [364, 371]."""
    gap = r.f["periods"]["prior_gap_days"]
    if gap is None or r.f["periods"]["tag"] == "FY":
        return False
    return abs(gap - YEAR_DAYS) <= CODE_TOLERANCE and not (YEAR_RANGE[0] <= gap <= YEAR_RANGE[1])


def fy_prior_not_a_year(r: Rec) -> bool:
    """A 10-K's prior fiscal-year column is taken without a date check: the gap is not a year."""
    p = r.f["periods"]
    return (
        p["tag"] == "FY"
        and p["prior_gap_days"] is not None
        and not (YEAR_RANGE[0] <= p["prior_gap_days"] <= YEAR_RANGE[1])
    )


def base_before(begin: dt.date) -> Any:
    """Predicate: the first fiscal-year column of the payload is a year that began before *begin*."""

    def pred(r: Rec) -> bool:
        dates = r.f["periods"]["fy_dates"]
        if not dates:
            return False
        first_end = dt.date.fromisoformat(min(dates))
        return first_end - dt.timedelta(days=364) < begin

    return pred


def cagr_crosses(metric: str, begin: dt.date, *, lenders: bool = False) -> Any:
    base = base_before(begin)

    def pred(r: Rec) -> bool:
        if r.form != "10-K" or r.f["metrics"].get(metric, {}).get("value") is None:
            return False
        if lenders and r.sub_industry not in LENDER_SUB_INDUSTRIES:
            return False
        return base(r)

    return pred


def m_periods(recs: list[Rec]) -> list[Finding]:
    return [
        count(
            "PER08.prior_gap_accepted_by_code_not_rule",
            ["PER-08"],
            "Quarter pairs with its year-ago quarter at a gap the code accepts (±20 days of 365) and the rule rejects ([364, 371])",
            recs,
            gap_accepted_by_code_only,
            population=lambda r: (
                r.f["periods"]["tag"] != "FY" and r.f["periods"]["prior_gap_days"] is not None
            ),
            command=CMD,
        ),
        count(
            "PER08.fy_prior_not_a_year",
            ["PER-08"],
            "A 10-K's prior fiscal-year column is not a year earlier (taken with no date check)",
            recs,
            fy_prior_not_a_year,
            population=lambda r: (
                r.f["periods"]["tag"] == "FY" and r.f["periods"]["prior_gap_days"] is not None
            ),
            command=CMD,
        ),
        count(
            "PER13.cagr_crosses_asc842",
            ["PER-12", "PER-13"],
            "A 10-K revenue/net-income/OCF CAGR whose first fiscal year began before the ASC 842 adoption (fiscal years beginning after 2018-12-15)",
            recs,
            cagr_crosses("revenue_cagr", ASC842_FIRST_YEAR_BEGINS),
            population=lambda r: r.form == "10-K",
            command=CMD,
            note="adoption boundary is the standard's effective date for large SEC filers, not each company's actual adoption date",
        ),
        count(
            "PER14.net_income_cagr_crosses_cecl",
            ["PER-12", "PER-14"],
            "A lender's 10-K net-income CAGR whose first fiscal year began before the CECL adoption (fiscal years beginning after 2019-12-15)",
            recs,
            cagr_crosses("net_income_cagr", CECL_FIRST_YEAR_BEGINS, lenders=True),
            population=lambda r: r.form == "10-K" and r.sub_industry in LENDER_SUB_INDUSTRIES,
            command=CMD,
            note="lenders = banks, consumer finance, brokers/custody, mortgage and specialized finance; the CECL adoption date is the effective date for large SEC filers",
        ),
    ]


def id07(conn: Any, client: SecClient, recs: list[Rec]) -> list[Finding]:
    """ID-07: the stored period end against the header ``reportDate`` of its accession."""
    by_cik: dict[str, list[Rec]] = collections.defaultdict(list)
    for r in recs:
        by_cik[r.cik].append(r)
    exact = within7 = total = 0
    bad: list[tuple[str, str, str]] = []
    cos: set[str] = set()
    for cik, group in by_cik.items():
        sec: dict[str, dict[str, Any]] = {}
        for c in cik_family(cik):
            for f in all_filings(client, c, "2021-12-01") or []:
                sec[f["accessionNumber"]] = f
        for r in group:
            f = sec.get(r.accession)
            if f is None or not f["reportDate"]:
                continue
            total += 1
            diff = abs(
                (dt.date.fromisoformat(r.period_end) - dt.date.fromisoformat(f["reportDate"])).days
            )
            exact += diff == 0
            within7 += diff <= 7  # noqa: PLR2004
            if diff > 7:  # noqa: PLR2004
                cos.add(r.ticker)
                bad.append(
                    (
                        r.ticker,
                        r.accession,
                        f"stored {r.period_end} vs periodOfReport {f['reportDate']}",
                    )
                )
    return [
        Finding(
            "ID07.period_end_differs_from_header",
            ["ID-07"],
            "Stored period end differs from the EDGAR header periodOfReport by more than 7 days",
            "a",
            total - within7,
            total,
            len(cos),
            examples=bad[:5],
            command=CMD,
        ),
        Finding(
            "ID07.period_end_not_exact",
            ["ID-07"],
            "Stored period end is not exactly the header periodOfReport",
            "a",
            total - exact,
            total,
            len({r.ticker for r in recs}) and len(cos),
            examples=[],
            command=CMD,
            note="includes 1-7 day differences (52/53-week calendars)",
        ),
    ]


def forms_among_ciks(conn: Any, client: SecClient) -> list[Finding]:
    """PER-09 and APP-10b: transition reports and succession 8-Ks in the submissions of the CIKs."""
    tickers = {
        r["ticker"]: r["cik"]
        for r in conn.execute("SELECT ticker, cik FROM assets WHERE cik IS NOT NULL")
    }
    hits: dict[str, list[tuple[str, str, str]]] = {"transition": [], "succession": []}
    cos: dict[str, set[str]] = {"transition": set(), "succession": set()}
    for ticker, cik in tickers.items():
        for c in cik_family(cik):
            for f in all_filings(client, c, "2021-12-01") or []:
                kind = (
                    "transition"
                    if f["form"] in TRANSITION_FORMS
                    else "succession"
                    if f["form"] in SUCCESSION_FORMS
                    else None
                )
                if kind:
                    hits[kind].append(
                        (ticker, f["accessionNumber"], f"{f['form']} filed {f['filingDate']}")
                    )
                    cos[kind].add(ticker)
    n = len(tickers)
    return [
        Finding(
            "PER09.transition_reports",
            ["PER-09"],
            "Companies with a 10-KT/10-QT (fiscal-year change) since 2022",
            "a",
            len(cos["transition"]),
            n,
            len(cos["transition"]),
            unit="companies",
            examples=hits["transition"][:5],
            command=CMD,
        ),
        Finding(
            "APP10b.succession_8ks",
            ["APP-10b-1"],
            "Companies with an 8-K12B/8-K12G3/8-K15D5 (succession) since 2022",
            "a",
            len(cos["succession"]),
            n,
            len(cos["succession"]),
            unit="companies",
            examples=hits["succession"][:5],
            command=CMD,
            note="the successor/predecessor link itself (APP-10b-2/3) needs a hand reading of each 8-K",
        ),
    ]
