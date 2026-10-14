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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["classify"])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache", default=str(REPO / ".cache" / "sec_xcheck"))
    parser.add_argument("--fetch", action="store_true", help="download what is not cached")
    args = parser.parse_args(argv)
    if args.command == "classify":
        return cmd_classify(args)
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
