"""N1 (PER-02, L-04): does a 10-K/A or 10-Q/A replace an original filing?

    uv run python scripts/audit_sec_checklist.py measure --only n1

The gateway lists a company's 10-K filings with amendments included (``company.get_filings(form="10-K")``, edgartools'
default) filtered to those *filed* in the calendar year; the pipeline keeps the **most recent** one per year
(``pipeline._select_filings``, ``ordered[-1:]``). A 10-Q year keeps every filing, oldest first; two filings that share a
quarter label overwrite one another (``db.upsert_filing`` ``ON CONFLICT ... DO UPDATE``).

:func:`simulate_selection` replays that rule over the SEC ``submissions`` history; :func:`classify_stored` reads the
form of each stored accession. Level (a) is the SEC; level (b) is what is stored.
"""

from __future__ import annotations

import collections
import datetime as dt
from typing import Any

from sec_xcheck.common import SecClient, cik_family
from sec_xcheck.findings import Finding, spread
from sec_xcheck.submissions import all_filings

ANNUAL = ("10-K", "10-K/A")
QUARTERLY = ("10-Q", "10-Q/A")
SAME_PERIOD_DAYS = 7


def merged_filings(client: SecClient, cik: str, since: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in cik_family(cik):
        out.extend(all_filings(client, c, since) or [])
    return out


def close(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days) <= SAME_PERIOD_DAYS


def simulate_selection(filings: list[dict[str, Any]], years: range) -> list[dict[str, Any]]:
    """The 10-K the pipeline keeps in each filing year, and what that choice does to the original.

    ``outcome``: ``original`` (the kept filing is the year's original 10-K); ``amendment_same_fy`` (a 10-K/A of the same
    fiscal year replaces its original); ``amendment_other_fy`` (the kept 10-K/A belongs to another fiscal year than the
    original filed that year, which is lost); ``amendment_only`` (no original filed that year).
    """
    out = []
    for year in years:
        in_year = [
            f
            for f in filings
            if f["form"] in ANNUAL and (f["filingDate"] or "").startswith(str(year))
        ]
        if not in_year:
            continue
        kept = max(in_year, key=lambda f: (f["filingDate"], f["accessionNumber"]))
        originals = [f for f in in_year if f["form"] == "10-K"]
        if kept["form"] == "10-K":
            outcome = "original"
        elif not originals:
            outcome = "amendment_only"
        elif any(close(kept["reportDate"], o["reportDate"]) for o in originals):
            outcome = "amendment_same_fy"
        else:
            outcome = "amendment_other_fy"
        out.append({"year": year, "kept": kept, "originals": originals, "outcome": outcome})
    return out


COLLISION_GAP_DAYS = (
    350,
    380,
)  # a stored year end one 52/53-week year earlier, in the same calendar year


def missing_causes(
    original: dict[str, Any],
    stored_ends: list[str],
    *,
    amended: bool,
    last_stored_filing: str,
    has_error: bool,
) -> list[str]:
    """Why an original 10-K has no stored 10-K of its period (non-exclusive, in order of certainty).

    * ``label_collision``: a stored 10-K ends 350-380 days earlier in the same calendar year, so a calendar-year label
      (``FY<year>``) was shared by two fiscal years of a 52/53-week filer and the later one was dropped (T-140 fixed the
      labelling; production predates it);
    * ``amendment``: a 10-K/A of the period exists and the pipeline keeps the most recent filing;
    * ``after_cutoff``: filed after the latest filing the database holds;
    * ``recorded_error``: the run recorded a failure for that company and filing year;
    * ``stored_under_another_period``: a stored 10-K carries the accession's year but a different period end.
    """
    report = dt.date.fromisoformat(original["reportDate"])
    causes: list[str] = []
    if any(
        pe
        and pe[:4] == original["reportDate"][:4]
        and COLLISION_GAP_DAYS[0]
        <= (report - dt.date.fromisoformat(pe)).days
        <= COLLISION_GAP_DAYS[1]
        for pe in stored_ends
    ):
        causes.append("label_collision")
    if amended:
        causes.append("amendment")
    if original["filingDate"] > last_stored_filing:
        causes.append("after_cutoff")
    if has_error:
        causes.append("recorded_error")
    return causes or ["unexplained"]


def ingestion_error(conn: Any, ticker: str, year: str) -> bool:
    """A 10-K ingestion failure recorded for the company in the filing year (gateway or period errors, not LLM errors)."""
    return bool(
        conn.execute(
            "SELECT 1 FROM analysis_run_error WHERE ticker = ? AND form = '10-K' AND fiscal_period = ? "
            "AND (message LIKE '%/financials/%' OR message LIKE '%fiscal-year column%') LIMIT 1",
            (ticker, year),
        ).fetchone()
    )


def classify_stored(stored_accession: str, by_accession: dict[str, dict[str, Any]]) -> str:
    sec = by_accession.get(stored_accession)
    if sec is None:
        return "not_in_submissions"
    return "amendment" if sec["form"].endswith("/A") else "original"


def quarterly_collisions(
    filings: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """10-Q/A filings with an original 10-Q of the same period: the amendment overwrites the original's label."""
    originals = [f for f in filings if f["form"] == "10-Q"]
    return [
        (f, o)
        for f in filings
        if f["form"] == "10-Q/A"
        for o in originals
        if close(f["reportDate"], o["reportDate"])
    ]


def measure(conn: Any, client: SecClient, until: str) -> list[Finding]:  # noqa: C901, PLR0915 - one pass over the SEC history
    assets = conn.execute(
        "SELECT id, ticker, cik FROM assets WHERE cik IS NOT NULL ORDER BY ticker"
    ).fetchall()
    years = range(2022, int(until[:4]) + 1)
    stored_class: collections.Counter[str] = collections.Counter()
    stored_ex: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    stored_cos: dict[str, set[str]] = collections.defaultdict(set)
    outcome: collections.Counter[str] = collections.Counter()
    outcome_ex: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    outcome_cos: dict[str, set[str]] = collections.defaultdict(set)
    lost_n: collections.Counter[str] = collections.Counter()
    lost_cos: dict[str, set[str]] = collections.defaultdict(set)
    lost_ex: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    originals_total = q_collisions = 0
    cause_n: collections.Counter[str] = collections.Counter()
    cause_cos: dict[str, set[str]] = collections.defaultdict(set)
    cause_ex: dict[str, list[tuple[str, str, str]]] = collections.defaultdict(list)
    last_stored = conn.execute("SELECT MAX(filing_date) FROM sec_filings").fetchone()[0]
    q_ex: list[tuple[str, str, str]] = []
    q_cos: set[str] = set()
    filing_total = 0
    for a in assets:
        sec = [
            f for f in merged_filings(client, a["cik"], "2021-12-01") if f["filingDate"] <= until
        ]
        by_accession = {f["accessionNumber"]: f for f in sec}
        stored = conn.execute(
            "SELECT form, fiscal_period, period_end, accession_number FROM sec_filings WHERE asset_id = ?",
            (a["id"],),
        ).fetchall()
        for s in stored:
            filing_total += 1
            cls = classify_stored(s["accession_number"], by_accession)
            stored_class[cls] += 1
            stored_cos[cls].add(a["ticker"])
            stored_ex[cls].append(
                (a["ticker"], s["accession_number"], f"{s['form']} {s['fiscal_period']}")
            )
        stored_10k_ends = [s["period_end"] for s in stored if s["form"] == "10-K"]
        for sim in simulate_selection(sec, years):
            outcome[sim["outcome"]] += 1
            outcome_cos[sim["outcome"]].add(a["ticker"])
            outcome_ex[sim["outcome"]].append(
                (
                    a["ticker"],
                    sim["kept"]["accessionNumber"],
                    f"{sim['kept']['form']} FY{sim['kept']['reportDate']}",
                )
            )
        for o in (f for f in sec if f["form"] == "10-K" and f["filingDate"] >= "2022-01-01"):
            originals_total += 1
            if any(close(o["reportDate"], pe) for pe in stored_10k_ends):
                continue
            amended = any(
                f["form"] == "10-K/A" and close(f["reportDate"], o["reportDate"]) for f in sec
            )
            for cause in missing_causes(
                o,
                stored_10k_ends,
                amended=amended,
                last_stored_filing=last_stored,
                has_error=ingestion_error(conn, a["ticker"], o["filingDate"][:4]),
            ):
                cause_n[cause] += 1
                cause_cos[cause].add(a["ticker"])
                cause_ex[cause].append(
                    (a["ticker"], o["accessionNumber"], f"FY ending {o['reportDate']}")
                )
            lost = "with_amendment" if amended else "without_amendment"
            lost_n[lost] += 1
            lost_cos[lost].add(a["ticker"])
            lost_ex[lost].append(
                (a["ticker"], o["accessionNumber"], f"FY ending {o['reportDate']} not stored")
            )
        for f, o in quarterly_collisions([f for f in sec if f["filingDate"] >= "2022-01-01"]):
            q_collisions += 1
            q_cos.add(a["ticker"])
            q_ex.append((a["ticker"], f["accessionNumber"], f"10-Q/A of {o['accessionNumber']}"))
    cmd = "uv run python scripts/audit_sec_checklist.py measure --only n1"
    findings = [
        Finding(
            "N1.stored_is_amendment",
            ["PER-02", "L-04"],
            "A stored filing is a 10-K/A or 10-Q/A",
            "b",
            stored_class["amendment"],
            filing_total,
            len(stored_cos["amendment"]),
            examples=spread(stored_ex["amendment"]),
            command=cmd,
            note="form of the stored accession, read from the SEC submissions API",
        ),
        Finding(
            "N1.stored_not_in_submissions",
            ["APP-10a", "PER-11"],
            "A stored accession is not in its CIK's submissions",
            "b",
            stored_class["not_in_submissions"],
            filing_total,
            len(stored_cos["not_in_submissions"]),
            examples=spread(stored_ex["not_in_submissions"]),
            command=cmd,
            note="fetched by ticker from another issuer, or outside the cached submissions window",
        ),
    ]
    year_total = sum(outcome.values())
    for key, title in (
        ("amendment_same_fy", "A 10-K/A of the same fiscal year is the 10-K the pipeline keeps"),
        (
            "amendment_other_fy",
            "A 10-K/A of another fiscal year displaces that year's original 10-K",
        ),
        ("amendment_only", "The only 10-K-type filing of the year is an amendment"),
    ):
        findings.append(
            Finding(
                f"N1.select_{key}",
                ["PER-02", "L-04"],
                title,
                "a",
                outcome[key],
                year_total,
                len(outcome_cos[key]),
                unit="company-years",
                examples=spread(outcome_ex[key]),
                command=cmd,
                note="replay of pipeline._select_filings over the SEC submissions history",
            )
        )
    for key, title in (
        (
            "with_amendment",
            "An original 10-K has no stored 10-K of its period and a 10-K/A of that period exists (year lost to the amendment)",
        ),
        (
            "without_amendment",
            "An original 10-K has no stored 10-K of its period and no amendment exists (another ingestion loss)",
        ),
    ):
        findings.append(
            Finding(
                f"N1.original_10k_not_stored_{key}",
                ["PER-02", "PER-11"],
                title,
                "a",
                lost_n[key],
                originals_total,
                len(lost_cos[key]),
                unit="10-K filings",
                examples=spread(lost_ex[key]),
                command=cmd,
                note="stored period matched to the SEC reportDate within 7 days",
            )
        )
    for cause in ("label_collision", "amendment", "after_cutoff", "recorded_error", "unexplained"):
        findings.append(
            Finding(
                f"N1.missing_cause.{cause}",
                ["PER-02", "PER-08", "ID-08"]
                if cause == "label_collision"
                else ["PER-02", "PER-11"],
                f"An original 10-K has no stored 10-K of its period; cause: {cause}",
                "a",
                cause_n[cause],
                originals_total,
                len(cause_cos[cause]),
                unit="10-K filings",
                examples=spread(cause_ex[cause]),
                command=cmd,
                note="causes are not exclusive (a filing can be both amended and label-collided); "
                "label_collision is T-140's repeated-period-label loss, fixed in code and absent from this older database",
            )
        )
    findings.append(
        Finding(
            "N1.10qa_overwrites_10q",
            ["PER-02", "L-04"],
            "A 10-Q/A shares its period with an original 10-Q (label collision)",
            "a",
            q_collisions,
            filing_total,
            len(q_cos),
            unit="10-Q/A filings",
            examples=spread(q_ex),
            command=cmd,
            note="denominator is the stored filings, for scale only",
        )
    )
    return findings
