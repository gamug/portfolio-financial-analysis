"""Fact-level cross-check: does every stored standard-taxonomy fact equal the SEC's value for its accession?

    uv run python scripts/sec_xcheck/xcheck.py [--db PATH] [--cache DIR] [--csv PATH]

For each ``financial_facts`` row of a ``us-gaap_`` or ``dei_`` concept, finds the SEC ``companyfacts`` facts of the
same accession, concept and period end (instant, a year, a quarter or a year-to-date duration, by the period key's
tag) and classifies the stored value: ``match``, ``sign_flip``, ``VALUE_MISMATCH``, or
``no_sec_fact_same_accn_period``. Custom concepts are counted apart (``companyfacts`` carries no extension
concepts). Read-only.
"""

from __future__ import annotations

import argparse
import collections
import csv
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import (
    FY_DAYS,
    QUARTER_DAYS,
    SecClient,
    cache_dir,
    cik_family,
    days_between,
    index_by_accession,
    match_value,
    open_ro,
)

DURATION_KEY = re.compile(r"^(\d{4}-\d{2}-\d{2}) \((\w+)\)$")
INSTANT_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YTD_DAYS = (160, 290)


def parse_period_key(key: str) -> tuple[str, str] | None:
    """``(end, tag)`` of a stored period key: ``INST`` for a plain date, else the gateway's duration tag."""
    m = DURATION_KEY.match(key)
    if m:
        return m.group(1), m.group(2)
    if INSTANT_KEY.match(key):
        return key, "INST"
    return None


def kind_ok(tag: str, start: str | None, end: str) -> bool:
    if tag == "INST":
        return start is None
    if start is None:
        return False
    days = days_between(start, end)
    if tag == "FY":
        return FY_DAYS[0] <= days <= FY_DAYS[1]
    if tag.startswith("Q"):
        return QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]
    if tag == "YTD":
        return YTD_DAYS[0] <= days <= YTD_DAYS[1]
    return False


def classify_fact(value: float, candidates: list[float]) -> str:
    if not candidates:
        return "no_sec_fact_same_accn_period"
    if any(match_value(c, value) for c in candidates):
        return "match"
    if any(match_value(abs(c), abs(value)) for c in candidates):
        return "sign_flip"
    return "VALUE_MISMATCH"


def candidates_for(
    index: dict[str, list[tuple[str, str, dict[str, Any]]]],
    accn: str,
    concept: str,
    end: str,
    tag: str,
) -> list[float]:
    tax, _, name = concept.partition("_")
    return [
        fact["val"]
        for t, c, fact in index.get(accn, [])
        if t == tax and c == name and fact["end"] == end and kind_ok(tag, fact.get("start"), end)
    ]


def build_index(client: SecClient, cik: str) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    """The companyfacts of a CIK and its predecessors, grouped by accession."""
    index: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for c in cik_family(cik):
        doc = client.get("companyfacts", c)
        for accn, facts in (index_by_accession(doc, ("us-gaap", "dei")) if doc else {}).items():
            index.setdefault(accn, []).extend(facts)
    return index


def classify_row(
    r: Any, index: dict[str, list[tuple[str, str, dict[str, Any]]]]
) -> tuple[str, list[float]]:
    """``(class, sec candidates)`` of one stored fact row."""
    if not r["concept"].startswith(("us-gaap_", "dei_")):
        return "custom_concept", []
    if r["value"] is None:
        return "null", []
    parsed = parse_period_key(r["period_key"])
    if parsed is None:
        return "unparsed_key", []
    cands = candidates_for(index, r["accn"], r["concept"], *parsed)
    return classify_fact(r["value"], cands), cands


def run(
    conn: Any, client: SecClient
) -> tuple[collections.Counter[str], dict[str, collections.Counter[str]], list[tuple[Any, ...]]]:
    stats: collections.Counter[str] = collections.Counter()
    per_concept: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    bad: list[tuple[Any, ...]] = []
    for (cik,) in conn.execute(
        "SELECT DISTINCT a.cik FROM assets a JOIN sec_filings f ON f.asset_id = a.id"
    ):
        index = build_index(client, cik)
        for r in conn.execute(
            "SELECT ff.concept, ff.statement, ff.period_key, ff.value, ff.filing_version AS accn, "
            "ff.correction_rule, a.ticker, f.form, f.period_end FROM financial_facts ff "
            "JOIN sec_filings f ON f.id = ff.filing_id JOIN assets a ON a.id = f.asset_id WHERE a.cik = ?",
            (cik,),
        ):
            kind, cands = classify_row(r, index)
            stats[kind] += 1
            if kind in ("match", "sign_flip", "VALUE_MISMATCH", "no_sec_fact_same_accn_period"):
                per_concept[r["concept"]][kind] += 1
            if kind == "VALUE_MISMATCH":
                bad.append(
                    (
                        r["ticker"],
                        r["form"],
                        r["period_end"],
                        r["statement"],
                        r["concept"],
                        r["period_key"],
                        r["value"],
                        cands,
                        r["correction_rule"],
                    )
                )
    return stats, per_concept, bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
    parser.add_argument("--csv", help="write the VALUE_MISMATCH rows here")
    args = parser.parse_args(argv)
    stats, per_concept, bad = run(open_ro(args.db), SecClient(cache_dir(args.cache), offline=True))
    print("facts:", sum(stats.values()), dict(stats))
    print("\nconcepts with sign flips (count):")
    for con, cn in sorted(per_concept.items(), key=lambda x: -x[1]["sign_flip"]):
        if cn["sign_flip"]:
            print(f"  {con}: flip {cn['sign_flip']} match {cn['match']}")
    print("\nVALUE_MISMATCH:", len(bad))
    for b in bad[:40]:
        print(" ", b)
    if args.csv:
        with Path(args.csv).open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(bad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
