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
import importlib.util
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient
from sec_xcheck.submissions import all_filings

from fundamental_agent import pillars, pipeline
from fundamental_agent.config import Settings
from fundamental_agent.edgar_client import EdgarClient
from kg_schema import identity
from kg_schema.db import connect_ro

REPO = Path(__file__).resolve().parents[1]
REGISTER = REPO / "docs" / "checklist_sec" / "succession_8ks.csv"
DEFAULT_OUT = Path("/workspaces/thesis/scratch/t155/out")
WINDOW_START = "2022-01-01"  # fundamental_agent.pipeline.DEFAULT_SINCE_YEAR

# F1 (fix 1): each successor's first 10-K after its succession's effective date, checked for a
# full-year column (a business combination presents a predecessor and a successor part instead).
SPLIT = {
    "BLK": ("0002012383", "2024-10-01"),
    "BG": ("0001996862", "2023-10-31"),
    "PSKY": ("0002041610", "2025-08-07"),
    "XOM": ("0002115436", "2026-07-01"),
}
OPERATING_CASH_FLOW = "NetCashProvidedByUsedInOperatingActivities"
REGISTER_LINES = 19  # T-147's register: 19 succession 8-Ks from 18 current members (T155_plan.md)
PER08_YEAR = (364, 371)  # PER-08's year length, in days
FACTS_CACHE = REPO / ".cache" / "sec_xcheck" / "companyfacts"
USER_AGENT = "research@example.com"

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
    text = re.sub(r"\u201c\s+", "\u201c", text)  # a tag between an opening quote and its text
    text = re.sub(r"\s+\u201d", "\u201d", text)
    for curly, straight in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(curly, straight)
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
    # F3 (fix 1): the register check lives here, not in a test -- AGENTS.md: tests never read
    # docs/checklist_sec/ (it is git-excluded, so such a test always skips in CI and checks nothing).
    assert len(register) == REGISTER_LINES, (
        f"the register has {len(register)} lines, expected {REGISTER_LINES}"
    )
    for row in register:
        key = (row["ticker"], row["accession_number"])
        assert key in READING, f"the register line {key} is not read"
    print(f"register: {len(register)} lines, every line read")

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


def cmd_check(args: argparse.Namespace) -> int:
    """Run the pipeline's own APP-10b-3 test (``pipeline._last_annual_report_missing``) on every row of
    ``src/kg_schema/data/cik_successions.csv``, through the EDGAR gateway (read-only listing calls)."""
    rows = identity.read_successions()
    print(f"{len(rows)} rows in {identity.SUCCESSIONS_CSV}")
    failed = 0
    with EdgarClient(os.environ["EDGAR_BASE_URL"]) as edgar:
        engine = SimpleNamespace(edgar=edgar)
        for row in rows:
            verdict = pipeline._last_annual_report_missing(
                engine, row["predecessor_cik"], row["effective_date"], row["successor_cik"]
            )
            failed += verdict is not None
            state = "PASS" if verdict is None else f"FAIL: {verdict}"
            print(
                f"  {row['successor_cik']} <- {row['predecessor_cik']} eff {row['effective_date']}: {state}"
            )
    return 1 if failed else 0


def _load_module(name: str, path: Path) -> Any:
    """Load a sibling script as a module (the repo's own measurement scripts, reused not copied)."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a local file always loads
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _facts_only_analyst() -> Any:
    """The facts-only analyst from ``scripts/verify_t149.py`` (T-149): every metric computed, the score
    the rule-based fallback, no model and no network beyond the gateway."""
    return _load_module("verify_t149", REPO / "scripts" / "verify_t149.py")._FactsOnlyAnalyst


def _members(universe: str, day: str) -> list[dict[str, str]]:
    """The members as of *day* (symbol, CIK, join date), from the universe file (F2, L-11's source)."""
    uconn = _ro(universe)
    return [
        dict(r)
        for r in uconn.execute(
            "SELECT symbol, cik, valid_from FROM universe_membership WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to >= ?) ORDER BY symbol",
            (day, day),
        )
    ]


def _unscored_on_members(db: str, universe: str, day: str) -> dict[str, Any]:
    """T-073's unscored members, counted on the **members as of *day*** (F2), not the copy's asset set.

    A member is a symbol on the date (503 on 2026-09-22; the share-class pairs share a CIK). The copy
    holds assets the index no longer has (a leaver) and may miss members added after it was built, so
    neither enters the denominator; a member whose asset has no filing by the date is named.
    """
    t073 = _load_module("verify_t073", REPO / "scripts" / "verify_t073.py")
    stored = t073.Stored(db)
    latest = stored.latest_filings()
    members = {m["symbol"]: m for m in _members(universe, day)}
    # a member's asset: by CIK, and within a CIK's assets by symbol (the share classes), so a line the
    # copy still carries under its old symbol (PSKY -> SKYD) is found and the classes are not merged
    by_cik: dict[str, list[tuple[str, int]]] = {}
    for aid, asset in stored.assets.items():
        by_cik.setdefault(str(asset["cik"]), []).append((str(asset["ticker"]), aid))

    def asset_for(symbol: str, cik: str) -> int | None:
        candidates = by_cik.get(cik, [])
        return next((aid for ticker, aid in candidates if ticker == symbol), None) or (
            candidates[0][1] if candidates else None
        )

    has_filing = {
        symbol: aid
        for symbol, member in members.items()
        if (aid := asset_for(symbol, member["cik"])) is not None and latest.get(aid) is not None
    }
    unscored = sorted(
        s for s, aid in has_filing.items() if pillars.empty_pillars(stored.statuses(latest[aid]))
    )
    return {
        "date": day,
        "members": len(members),
        "members_with_a_filing": len(has_filing),
        "members_without_a_filing": [
            {"symbol": s, "valid_from": members[s]["valid_from"]}
            for s in sorted(set(members) - set(has_filing))
        ],
        "unscored": unscored,
        "share": len(unscored) / len(has_filing) if has_filing else 0.0,
    }


def cmd_run(args: argparse.Namespace) -> int:
    """Run the fundamental pipeline for the named members on the scratch copy, facts-only (no LLM)."""
    pipeline.FundamentalAnalyst = _facts_only_analyst()  # type: ignore[misc]
    pipeline.build_model = lambda _s: None  # type: ignore[assignment]
    settings = Settings(
        db_path=Path(args.db),
        universe_db_path=Path(args.universe),
        llm_api_key="unused-facts-only",
        llm_model="facts-only-no-llm",
        llm_url="http://unused.invalid",
        edgar_base_url=os.environ["EDGAR_BASE_URL"],
    )
    report = pipeline.run(
        settings,
        pipeline.RunParams(
            forms=("10-K", "10-Q"),
            since_year=2022,
            tickers=tuple(args.tickers.split(",")),
            analysis_date=args.analysis_date,
        ),
    )
    print(
        f"run {report.run_id}: planned {report.planned}, completed {report.completed}, "
        f"skipped {report.skipped}, failed {report.failed}"
    )
    for line in report.errors:
        print("  ERR", line[:300])
    return 0


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _filings_of(conn: sqlite3.Connection, cik: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT f.form, f.fiscal_period, f.period_end, f.filing_date, f.accession_number, "
            "f.cik FROM sec_filings f JOIN assets a ON a.id = f.asset_id WHERE a.cik = ? "
            "ORDER BY f.period_end, f.id",
            (cik,),
        )
    ]


def _metric_names(conn: sqlite3.Connection, cik: str) -> dict[str, str]:
    """``metric_name -> group`` for every metric row of the member's filings that has a value."""
    return {
        str(r["metric_name"]): str(r["metric_group"])
        for r in conn.execute(
            "SELECT DISTINCT m.metric_name, m.metric_group FROM fundamental_metrics m "
            "JOIN sec_filings f ON f.id = m.filing_id JOIN assets a ON a.id = f.asset_id "
            "WHERE a.cik = ? AND m.engine_version = 'metrics-v7' AND m.value IS NOT NULL",
            (cik,),
        )
    }


def cmd_measure(args: argparse.Namespace) -> int:
    """T-155 P3: the affected members before (``--before``) vs after (``--db``), plus the unscored
    counts, the succession-stage errors and the pre-close PSKY/SKYD resolution."""
    before = _ro(args.before)
    after = _ro(args.db)
    members = [("BLK", "0002012383"), ("BG", "0001996862"), ("SKYD", "0002041610")]
    report: dict[str, Any] = {"before_db": args.before, "after_db": args.db, "members": []}
    for ticker, cik in members:
        fb, fa = _filings_of(before, cik), _filings_of(after, cik)
        before_acc = {f["accession_number"] for f in fb}
        added = [f for f in fa if f["accession_number"] not in before_acc]
        mb, ma = _metric_names(before, cik), _metric_names(after, cik)
        gained = sorted(set(ma) - set(mb))
        added_acc = {f["accession_number"] for f in added}
        on_added: dict[str, dict[str, Any]] = {}
        for r in after.execute(
            "SELECT m.metric_name, m.metric_group, f.accession_number FROM fundamental_metrics m "
            "JOIN sec_filings f ON f.id = m.filing_id JOIN assets a ON a.id = f.asset_id "
            "WHERE a.cik = ? AND m.engine_version = 'metrics-v7' AND m.value IS NOT NULL",
            (cik,),
        ):
            if r["accession_number"] not in added_acc:
                continue
            entry = on_added.setdefault(
                str(r["metric_name"]), {"group": str(r["metric_group"]), "n": 0}
            )
            entry["n"] += 1
        report["members"].append(
            {
                "ticker": ticker,
                "cik": cik,
                "filings_before": len(fb),
                "filings_after": len(fa),
                "earliest_period_before": fb[0]["period_end"] if fb else None,
                "earliest_period_after": fa[0]["period_end"] if fa else None,
                "added_filings": added,
                "metric_names_gained": [
                    {
                        "metric": name,
                        "group": ma[name],
                        "multi_year": _is_multi_year(name, ma[name]),
                    }
                    for name in gained
                ],
                "metrics_on_added_filings": [
                    {
                        "metric": name,
                        "group": entry["group"],
                        "multi_year": _is_multi_year(name, entry["group"]),
                        "filings": entry["n"],
                    }
                    for name, entry in sorted(on_added.items())
                ],
            }
        )
    report["succession_errors"] = [
        dict(r)
        for r in after.execute(
            "SELECT ticker, message FROM analysis_run_error WHERE stage = 'succession'"
        )
    ]
    for label, conn in (("before", before), ("after", after)):
        report[f"members_{label}"] = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    report["unscored_on_members"] = {
        "before": _unscored_on_members(args.before, args.universe, args.analysis_date),
        "after": _unscored_on_members(args.db, args.universe, args.analysis_date),
    }
    report["preclose"] = _preclose(args.universe, args.db, args.preclose_date)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")

    print(f"wrote {args.out}")
    for m in report["members"]:
        print(
            f"  {m['ticker']:5} filings {m['filings_before']:>2} -> {m['filings_after']:>2}; "
            f"earliest {m['earliest_period_before']} -> {m['earliest_period_after']}; "
            f"added {len(m['added_filings'])}"
        )
        for g in m["metric_names_gained"]:
            mark = " (multi-year)" if g["multi_year"] else ""
            print(f"      gained {g['metric']} [{g['group']}]{mark}")
        my = [x for x in m["metrics_on_added_filings"] if x["multi_year"]]
        print(
            "      on the added filings, multi-year metrics with a value: "
            + ", ".join(f"{x['metric']}({x['filings']})" for x in my)
        )
    print(f"  succession-stage errors: {len(report['succession_errors'])}")
    for label in ("before", "after"):
        u = report["unscored_on_members"][label]
        missing = [m["symbol"] for m in u["members_without_a_filing"]]
        print(
            f"  unscored on the members ({label}, {u['date']}): {len(u['unscored'])} of "
            f"{u['members_with_a_filing']} with a filing ({u['share']:.1%}) of {u['members']} members; "
            f"no asset: {missing}"
        )
    print(f"  pre-close {args.preclose_date}: {json.dumps(report['preclose'])}")
    return 0


def _is_multi_year(name: str, group: str) -> bool:
    """A metric whose value spans more than one period: growth and CAGR by definition, and the
    cash-flow group's ratios, whose flows are trailing-twelve-month (PER-06)."""
    return bool(re.search(r"_growth$|_cagr$", name)) or group == "cashflow"


def _preclose(universe: str, db: str, day: str) -> dict[str, Any]:
    """The member CIK the universe resolves on *day*, its chain in the after DB, and the filing a run
    on *day* would score there (the latest available then) with its prior 10-K."""
    uconn = _ro(universe)
    member = uconn.execute(
        "SELECT symbol, cik FROM universe_membership WHERE valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to >= ?) AND cik = '0002041610'",
        (day, day),
    ).fetchone()
    conn = connect_ro(db)
    cik = "0002041610"
    chain = list(identity.cik_chain(conn, cik))
    available = [
        f
        for f in _filings_of(conn, cik)
        if f["form"] in ("10-K", "10-Q") and (f["filing_date"] or "") <= day
    ]
    latest, prior = inputs_at(
        [
            {
                **f,
                "_cik": f["cik"],
                "filingDate": f["filing_date"],
                "reportDate": f["period_end"],
                "accessionNumber": f["accession_number"],
            }
            for f in available
        ],
        day,
    )
    return {
        "date": day,
        "universe_member": dict(member) if member else None,
        "chain": chain,
        "latest": {
            "accession": latest["accession_number"],
            "cik": latest["_cik"],
            "form": latest["form"],
            "period_end": latest["reportDate"],
        }
        if latest
        else None,
        "prior_10k": {
            "accession": prior["accession_number"],
            "cik": prior["_cik"],
            "period_end": prior["reportDate"],
        }
        if prior
        else None,
    }


def _companyfacts(cik: str, *, fetch: bool) -> dict[str, Any]:
    """The SEC's own ``companyfacts`` for *cik* (read-only), cached under ``.cache/sec_xcheck``."""
    path = FACTS_CACHE / f"CIK{int(cik):010d}.json"
    if not path.exists():
        if not fetch:
            raise FileNotFoundError(f"{path} is not cached; pass --fetch")
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            body = response.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _periods_for(facts: dict[str, Any], accession: str, concept: str) -> list[tuple[str, str, int]]:
    """The distinct ``(start, end, days)`` of *concept*'s facts filed under *accession*."""
    units = facts.get("facts", {}).get("us-gaap", {}).get(concept, {}).get("units", {})
    out: set[tuple[str, str, int]] = set()
    for rows in units.values():
        for f in rows:
            if f.get("accn") == accession and f.get("start") and f.get("end"):
                days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days + 1
                out.add((f["start"], f["end"], days))
    return sorted(out)


def cmd_split(args: argparse.Namespace) -> int:
    """F1 (T-155 fix 1): does each successor's first 10-K after the effective date hold a full-year
    column, or split its year into a predecessor and a successor part (a business combination)?

    Reads the SEC's own ``companyfacts`` (read-only), the same source the FY2025 10-K's split is
    visible in. ``--fetch`` downloads a companyfacts file that is not cached."""
    client = SecClient(Path(args.cache), offline=True)
    report: list[dict[str, Any]] = []
    for ticker, (cik, effective) in SPLIT.items():
        tens = sorted(
            (
                r
                for r in all_filings(client, cik, since="2021-12-01") or []
                if r["form"] == "10-K" and r["filingDate"] > effective
            ),
            key=lambda r: r["filingDate"],
        )
        entry: dict[str, Any] = {
            "ticker": ticker,
            "cik": cik,
            "effective": effective,
            "first_10k": tens[0]["accessionNumber"] if tens else None,
            "first_10k_filed": tens[0]["filingDate"] if tens else None,
            "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
        }
        if tens:
            facts = _companyfacts(cik, fetch=args.fetch)
            periods = _periods_for(facts, tens[0]["accessionNumber"], OPERATING_CASH_FLOW)
            report_end = tens[0]["reportDate"]
            entry["period_end"] = report_end
            entry["periods"] = [{"start": s, "end": e, "days": d} for s, e, d in periods]
            # the filing's OWN fiscal year: a full-year period ending at its report date. A business
            # combination presents that year as a predecessor part and a successor part instead.
            entry["full_year_periods"] = [
                f"{s}..{e}"
                for s, e, d in periods
                if PER08_YEAR[0] <= d <= PER08_YEAR[1] and e == report_end
            ]
            entry["splits"] = not entry["full_year_periods"]
        report.append(entry)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {path}")
    for e in report:
        if e["first_10k"] is None:
            print(f"  {e['ticker']:5} no 10-K filed after {e['effective']}")
            continue
        mark = "SPLITS its year" if e["splits"] else "holds a full-year column"
        print(
            f"  {e['ticker']:5} first 10-K {e['first_10k']} (filed {e['first_10k_filed']}): {mark}"
        )
        for p in e["periods"]:
            print(f"        {p['start']} .. {p['end']}  {p['days']} days")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["classify", "used", "check", "run", "measure", "split"])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--cache", default=str(REPO / ".cache" / "sec_xcheck"))
    parser.add_argument("--fetch", action="store_true", help="download what is not cached")
    parser.add_argument(
        "--universe",
        default="/workspaces/thesis/data/universe_history.db",
        help="the universe file",
    )
    parser.add_argument("--db", help="the scratch copy to run on / measure (run, measure)")
    parser.add_argument("--before", default="/workspaces/thesis/scratch/t071a/after.db")
    parser.add_argument("--preclose-date", default="2024-12-31")
    parser.add_argument("--tickers", help="comma-separated members (run)")
    parser.add_argument("--analysis-date", default="2026-09-22")
    args = parser.parse_args(argv)
    if args.command == "classify":
        return cmd_classify(args)
    if args.command == "used":
        return cmd_used(args)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "split":
        return cmd_split(args)
    return cmd_measure(args)


if __name__ == "__main__":
    sys.exit(main())
