"""T-155 verification: the current members' CIK successions (checklist v1.3, APP-10b; T155_plan.md).

    uv run python scripts/verify_t155.py classify [--out DIR] [--fetch]

P1 of ``/workspaces/thesis/handoff/T155_plan.md``. It reads T-147's register
(``docs/checklist_sec/succession_8ks.csv``: one line per succession 8-K among the current members), reads
each 8-K's own text from the EDGAR archive, records the predecessor the filing names -- its name, and its
CIK through the SEC submissions file -- and the verbatim sentence that establishes the succession, and
decides one class per filing:

* ``new_cik``  -- the successor's CIK differs from the predecessor's (a row may be needed);
* ``same_cik`` -- the registrant kept its CIK (a reincorporation filed under the old number; no row);
* ``not_a_member_succession`` -- the 8-K is not about the member's own registrant;
* ``unclear``  -- anything else.

For a ``new_cik`` filing it measures what is lost: the predecessor's 10-K/10-Q filings in the window
(filed 2022 onward, the pipeline's ``DEFAULT_SINCE_YEAR``) that the successor's CIK does not list.

Network is read-only to ``www.sec.gov`` (the 8-K documents) and ``data.sec.gov`` (the submissions files),
through :class:`sec_xcheck.common.SecClient` (User-Agent ``research@example.com``, at most 10 requests per
second). ``--fetch`` downloads what is not cached; nothing here writes a database.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient
from sec_xcheck.submissions import all_filings

REPO = Path(__file__).resolve().parents[1]
REGISTER = REPO / "docs" / "checklist_sec" / "succession_8ks.csv"
DEFAULT_OUT = Path("/workspaces/thesis/scratch/t155/out")
WINDOW_START = "2022-01-01"  # fundamental_agent.pipeline.DEFAULT_SINCE_YEAR

# The hand reading of each succession 8-K (APP-10b-2): the predecessor the filing names, as a label the
# 8-K's own text carries (verified below), and its CIK. A `same_cik` filing keeps the member's own CIK
# (A7 p.4; WELL's 8-K says "New Welltower expects to make filings with the SEC under Old Welltower's prior
# CIK (0000766704)"); a `new_cik` filing names a different CIK, resolved from that CIK's submissions file.
READING: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("APO", "0001193125-22-000274"): [
        ("AAM", "0001411494"),
        ("AHL", "0001527469"),
    ],
    ("APTV", "0001193125-24-280796"): [("Aptiv", "0001521332")],
    ("BG", "0001104659-23-113123"): [("Bunge Bermuda", "0001144519")],
    ("BLK", "0001193125-24-229601"): [("Old BlackRock", "0001364742")],
    ("DOC", "0001104659-23-018524"): [("Old Healthpeak", "0000765880")],
    ("FERG", "0001193125-24-190512"): [("Ferguson plc", "0001832433")],
    ("FRT", "0001193125-22-000302"): [("Old FRT", "0000034903")],
    ("J", "0001193125-22-232264"): [("Predecessor", "0000052988")],
    ("KIM", "0001140361-23-000046"): [("Old Kimco", "0000879101")],
    ("KKR", "0001140361-22-021298"): [("Old Pubco", "0001404912")],
    ("LH", "0000920148-24-000063"): [("Labcorp", "0000920148")],
    ("LIN", "0001193125-23-055949"): [("Predecessor", "0001707925")],
    ("OKE", "0001193125-26-387972"): [("Legacy ONEOK", "0001039684")],
    ("PSA", "0001193125-23-211436"): [("Old PSA", "0001393311")],
    ("PSKY", "0001193125-25-175046"): [("Paramount", "0000813828")],
    ("PSKY", "0002041610-25-000029"): [("Paramount", "0000813828")],
    ("TEL", "0001104659-24-103940"): [("Swiss TEL", "0001385157")],
    ("WELL", "0001193125-22-092900"): [("Old Welltower", "0000766704")],
    ("XOM", "0001193125-26-291990"): [("ExxonMobil", "0000034088")],
}

FIELDS = (
    "ticker",
    "cik",
    "form",
    "filing_date",
    "accession_number",
    "predecessor",
    "predecessor_cik",
    "class",
    "lost_filings",
    "lost_accessions",
    "evidence_quote",
)

USED_FIELDS = (
    "ticker",
    "successor_cik",
    "effective_date",
    "first_member_date",
    "own_first",
    "own_last",
    "pred_first",
    "pred_last",
    "used",
    "used_at",
    "latest_accession",
    "latest_cik",
    "prior_10k_accession",
    "prior_10k_cik",
)


def to_text(markup: str) -> str:
    """The visible text of an EDGAR HTML document: tags dropped, entities unescaped, whitespace collapsed."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


_SUCCESSOR = re.compile(r"successor (?:issuer|registrant)", re.IGNORECASE)
_RULE = re.compile(r"Rule 12g-3|Rule 15d-5", re.IGNORECASE)

# An amendment (/A) restates no succession of its own: PSKY's 8-K12B/A refers to the original filing,
# which carries the sentence the row cites.
EVIDENCE_OF: dict[str, str] = {"0002041610-25-000029": "0001193125-25-175046"}


def succession_sentence(text: str, label: str) -> str | None:
    """The verbatim sentence that establishes the succession. First the shortest sentence that says
    *label* is the successor issuer/registrant; then the shortest naming *label* and Rule 12g-3/15d-5;
    then the same without *label*."""
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.;])\s+", flat)
    named = label.lower()
    for pattern in (_SUCCESSOR, _RULE):
        with_label = [s.strip() for s in sentences if pattern.search(s) and named in s.lower()]
        if with_label:
            return min(with_label, key=len)
    for pattern in (_SUCCESSOR, _RULE):
        hits = [s.strip() for s in sentences if pattern.search(s)]
        if hits:
            return min(hits, key=len)
    return None


def _submissions(client: SecClient, cik: str) -> dict[str, Any] | None:
    return client.get("submissions", cik)


def _registrant_names(doc: dict[str, Any]) -> list[str]:
    names = [str(doc.get("name") or "")]
    names += [str(n.get("name") or "") for n in (doc.get("formerNames") or [])]
    return [n for n in names if n]


def classify_one(client: SecClient, row: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """One register line -> its ``classify.csv`` row and any stop-check notes."""
    ticker, cik, accession = row["ticker"], row["cik"], row["accession_number"]
    reading = READING[(ticker, accession)]
    evidence_acc = EVIDENCE_OF.get(accession, accession)
    document = client.get_document(cik, evidence_acc, _primary_of(client, cik, evidence_acc))
    text = to_text(document or "")
    quote = succession_sentence(text, reading[0][0])
    notes: list[str] = []
    if document is None:
        notes.append(f"{ticker} {accession}: the 8-K document is missing")
    for label, _pred in reading:
        if label.lower() not in text.lower():
            notes.append(f"{ticker} {accession}: the 8-K text does not carry {label!r}")

    preds = [pred for _label, pred in reading]
    same = all(p == cik for p in preds)
    cls = "same_cik" if same else "new_cik"

    lost: list[tuple[str, str, str]] = []
    if cls == "new_cik":
        successor = all_filings(client, cik, since="2021-12-01") or []
        seen = {r["accessionNumber"] for r in successor}
        for pred in preds:
            for r in all_filings(client, pred, since="2021-12-01") or []:
                if (
                    r["form"] in ("10-K", "10-Q")
                    and r["filingDate"] >= WINDOW_START
                    and r["accessionNumber"] not in seen
                ):
                    lost.append((r["form"], r["filingDate"], r["accessionNumber"]))

    names = []
    for pred in preds:
        doc = _submissions(client, pred)
        names.append(str((doc or {}).get("name") or pred))
    out = {
        "ticker": ticker,
        "cik": cik,
        "form": row["form"],
        "filing_date": row["filing_date"],
        "accession_number": accession,
        "predecessor": "; ".join(names),
        "predecessor_cik": "; ".join(preds),
        "class": cls,
        "lost_filings": str(len(lost)),
        "lost_accessions": "; ".join(a for _f, _d, a in lost),
        "evidence_quote": quote or "",
    }
    return out, notes


def _primary_of(client: SecClient, cik: str, accession: str) -> str:
    """The primary document name of *accession*, from the cached submissions file (the recent block plus
    the older pages, a heavy filer's 8-K may sit in a page)."""
    for r in all_filings(client, cik, since="2021-12-01") or []:
        if r["accessionNumber"] == accession:
            return str(r["primaryDocument"])
    raise SystemExit(
        f"{accession} is not in CIK {cik}'s submissions file; cannot name its document"
    )


def cmd_classify(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = SecClient(Path(args.cache), offline=not args.fetch)
    with REGISTER.open(newline="", encoding="utf-8") as fh:
        register = list(csv.DictReader(fh))

    rows: list[dict[str, str]] = []
    notes: list[str] = []
    for row in register:
        out, row_notes = classify_one(client, row)
        rows.append(out)
        notes.extend(row_notes)

    path = out_dir / "classify.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    by_class: dict[str, list[str]] = {}
    for r in rows:
        by_class.setdefault(r["class"], []).append(r["ticker"])
    print(f"{len(rows)} filings classified; wrote {path}")
    for cls, tickers in sorted(by_class.items()):
        print(f"  {cls:24} {len(tickers):>2}  {', '.join(sorted(set(tickers)))}")
    new_cik_lost = [r for r in rows if r["class"] == "new_cik" and int(r["lost_filings"]) > 0]
    print(
        f"  new_cik with filings lost: {len(new_cik_lost)}  "
        + ", ".join(r["ticker"] for r in new_cik_lost)
    )
    for r in new_cik_lost:
        print(
            f"    {r['ticker']:5} {r['accession_number']} lost={r['lost_filings']:>2} pred={r['predecessor_cik']}"
        )
    if notes:
        print("STOP-CHECK notes:")
        for n in notes:
            print("  -", n)
    print(f"requests made: {client.requests}")
    return 0


def _universe_rows(universe: Path, cik: str) -> list[tuple[str, str | None]]:
    conn = sqlite3.connect(f"file:{universe}?mode=ro", uri=True)
    try:
        return [
            (str(vf), None if vt is None else str(vt))
            for vf, vt in conn.execute(
                "SELECT valid_from, valid_to FROM universe_membership WHERE cik = ? ORDER BY valid_from",
                (cik,),
            )
        ]
    finally:
        conn.close()


def first_member_date(universe: Path, cik: str) -> str | None:
    """The first day on or after :data:`WINDOW_START` the CIK is a member, from the universe file."""
    for valid_from, valid_to in _universe_rows(universe, cik):
        if valid_to is None or valid_to >= WINDOW_START:
            return max(valid_from, WINDOW_START)
    return None


def _window_filings(client: SecClient, cik: str) -> list[dict[str, Any]]:
    """The CIK's 10-K/10-Q filings in the window, each tagged with the CIK it was listed under."""
    return [
        {**r, "_cik": cik}
        for r in all_filings(client, cik, since="2021-12-01") or []
        if r["form"] in ("10-K", "10-Q") and r["filingDate"] >= WINDOW_START
    ]


def inputs_at(
    available: list[dict[str, Any]], day: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The filing a run at *day* scores (the latest by period end available then) and, when it is a
    10-Q, the prior 10-K its TTM and prior-year capital read (PER-06)."""
    current = [r for r in available if r["filingDate"] <= day]
    if not current:
        return None, None
    latest = max(
        current, key=lambda r: (r["reportDate"] or "", r["filingDate"], r["accessionNumber"])
    )
    prior = None
    if latest["form"] == "10-Q":
        tens = [
            r
            for r in current
            if r["form"] == "10-K" and (r["reportDate"] or "") < (latest["reportDate"] or "")
        ]
        if tens:
            prior = max(tens, key=lambda r: (r["reportDate"] or "", r["filingDate"]))
    return latest, prior


def used_row(
    client: SecClient, universe: Path, member_spec: tuple[str, str, str, tuple[str, ...]]
) -> dict[str, str]:
    """One ``used.csv`` row: does a member date actually read a lost predecessor filing?"""
    ticker, successor, effective, preds = member_spec
    own = _window_filings(client, successor)
    pred_rows = [r for p in preds for r in _window_filings(client, p)]
    pred_set = set(preds)
    member = first_member_date(universe, successor)
    days = {member, "2026-09-22"} | {r["filingDate"] for r in own + pred_rows}
    available = own + pred_rows

    used_at: str | None = None
    latest = prior = None
    for day in sorted(d for d in days if d and member and d >= member):
        latest, prior = inputs_at(available, day)
        if latest is None:
            continue
        reads_pred = latest["_cik"] in pred_set or (prior is not None and prior["_cik"] in pred_set)
        if reads_pred:
            used_at = day
            break
    if used_at is None:  # report the first member date's inputs when nothing is used
        latest, prior = inputs_at(available, member or "")
    return {
        "ticker": ticker,
        "successor_cik": successor,
        "effective_date": effective,
        "first_member_date": member or "",
        "own_first": min((r["filingDate"] for r in own), default=""),
        "own_last": max((r["filingDate"] for r in own), default=""),
        "pred_first": min((r["filingDate"] for r in pred_rows), default=""),
        "pred_last": max((r["filingDate"] for r in pred_rows), default=""),
        "used": "yes" if used_at else "no",
        "used_at": used_at or "",
        "latest_accession": (latest or {}).get("accessionNumber", ""),
        "latest_cik": (latest or {}).get("_cik", ""),
        "prior_10k_accession": (prior or {}).get("accessionNumber", ""),
        "prior_10k_cik": (prior or {}).get("_cik", ""),
    }


def _report_date(client: SecClient, cik: str, accession: str) -> str:
    """The 8-K's own "Date of earliest event reported" (the succession's effective date)."""
    for r in all_filings(client, cik, since="2021-12-01") or []:
        if r["accessionNumber"] == accession:
            return str(r["reportDate"] or "")
    return ""


def cmd_used(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = SecClient(Path(args.cache), offline=not args.fetch)
    universe = Path(args.universe)
    with REGISTER.open(newline="", encoding="utf-8") as fh:
        register = list(csv.DictReader(fh))

    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for row in register:
        ticker, accession = row["ticker"], row["accession_number"]
        reading = READING[(ticker, accession)]
        if all(p == row["cik"] for _label, p in reading) or ticker in seen:
            continue  # same_cik: no row; one row per member
        seen.add(ticker)
        rows.append(
            used_row(
                client,
                universe,
                (
                    ticker,
                    row["cik"],
                    _report_date(client, row["cik"], accession),
                    tuple(p for _label, p in reading),
                ),
            )
        )

    path = out_dir / "used.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=USED_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} new_cik members; wrote {path}")
    for r in rows:
        print(
            f"  {r['ticker']:5} member_from={r['first_member_date'] or '-':10} "
            f"effective={r['effective_date']} own={r['own_first'] or '-'}..{r['own_last'] or '-'} "
            f"pred={r['pred_first'] or '-'}..{r['pred_last'] or '-'} USED={r['used']}"
            + (f" at {r['used_at']}" if r["used_at"] else "")
        )
    print(f"requests made: {client.requests}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["classify", "used"])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache", default=str(REPO / ".cache" / "sec_xcheck"))
    parser.add_argument("--fetch", action="store_true", help="download what is not cached")
    parser.add_argument(
        "--universe",
        default="/workspaces/thesis/data/universe_history.db",
        help="the universe file",
    )
    args = parser.parse_args(argv)
    if args.command == "classify":
        return cmd_classify(args)
    return cmd_used(args)


if __name__ == "__main__":
    sys.exit(main())
