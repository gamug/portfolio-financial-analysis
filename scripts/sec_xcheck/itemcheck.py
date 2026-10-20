"""Line-item resolution against the SEC: which concept does each stored input come from?

    uv run python scripts/sec_xcheck/itemcheck.py [--db PATH] [--cache DIR] [--tickers A,B]

For each stored ``fundamental_metrics.inputs_json`` value of a checked line item, looks the filing's accession,
period end and duration (a year for a 10-K, a quarter for a 10-Q) up in the SEC ``companyfacts`` cache and
classifies the stored value:

* ``ok_own_concept``  -- equals a fact of one of the item's own registry concepts;
* ``FLIPPED``         -- equals the negative of such a fact (a sign defect, D1);
* ``ok_sourced_map``  -- equals a sourced map of the item's own facts (T-148): ``ProfitLoss`` less the NCI income for parent
  net income (ID-03), current plus deferred tax for income tax (ID-02b, B6's definition);
* ``OTHER_CONCEPT``   -- equals a fact of a concept the registry does not list for the item (D2);
* ``NOT_IN_SEC(rebuilt/derived)`` -- equals no fact of the accession (a derived value, or a custom concept);
* ``no_sec_facts_for_accn`` / ``none`` -- the accession has no facts at the source / the input is absent.

The database is opened read-only. Companyfacts files come from the cache (``audit_sec_checklist.py --fetch``).
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fundamental_agent.statements import REGISTRY

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sec_xcheck.common import (
    SecClient,
    cache_dir,
    cik_family,
    duration_matches,
    filings,
    iter_us_gaap,
    match_value,
    open_ro,
)

CHECK: dict[str, list[str]] = {
    "net_margin": ["revenue", "net_income"],
    "operating_margin": ["operating_income"],
    "effective_tax_rate": ["pretax_income", "income_tax"],
    "return_on_assets": ["total_assets"],
    "return_on_equity": ["equity"],
    "operating_cash_flow_margin": ["operating_cash_flow"],
    "net_debt_to_ebitda": ["depreciation_amortization", "cash"],
}
INSTANT_ITEMS = {"total_assets", "equity", "cash"}


def own_concepts() -> dict[str, set[str]]:
    """Registry item -> its own concept names, without the ``us-gaap_`` prefix."""
    return {
        name: {
            c.split("_", 1)[1]
            for c in (*item.concepts, *item.total_concepts, *item.fallback_concepts)
        }
        for name, item in REGISTRY.items()
    }


def facts_of(
    docs: Iterable[dict[str, Any]], accn: str, end: str, form: str, *, instant: bool
) -> dict[float, set[str]]:
    """``{value: {concepts}}`` of the accession's facts at *end*: instants, or durations of the form's length."""
    out: dict[float, set[str]] = collections.defaultdict(set)
    for doc in docs:
        for _tax, concept, fact in iter_us_gaap(doc):
            if fact["accn"] != accn or fact["end"] != end:
                continue
            if (
                (fact.get("start") is None)
                if instant
                else duration_matches(form, fact.get("start"), end)
            ):
                out[fact["val"]].add(concept)
    return out


# the sourced maps T-148 implements: item -> ((concept, weight), ...), the value being the weighted sum of the facts
SOURCED_MAPS: dict[str, tuple[tuple[str, float], ...]] = {
    "net_income": (
        ("ProfitLoss", 1.0),
        ("NetIncomeLossAttributableToNoncontrollingInterest", -1.0),
    ),
    "income_tax": (
        ("CurrentIncomeTaxExpenseBenefit", 1.0),
        ("DeferredIncomeTaxExpenseBenefit", 1.0),
    ),
}


def map_values(item: str, values: dict[float, set[str]]) -> list[float]:
    """The values a sourced map of *item* gives from the SEC facts of the accession and period (empty when a term is
    not filed)."""
    terms = SOURCED_MAPS.get(item)
    if not terms:
        return []
    by_concept: dict[str, list[float]] = collections.defaultdict(list)
    for value, concepts in values.items():
        for concept in concepts:
            by_concept[concept].append(value)
    if any(concept not in by_concept for concept, _w in terms):
        return []
    totals = [0.0]
    for concept, weight in terms:
        totals = [t + weight * v for t in totals for v in by_concept[concept]]
    return totals


@dataclass(frozen=True)
class _Drops:
    """The run's drop log for one filing, and what the item reads.

    *rows* are the rows the gateway nulled, as ``(statement, concept, the column's date, the
    original value)``; *period_end* is the filing's own period end; *statements* are the item's own
    statements (the LineItem's).
    """

    rows: tuple[tuple[str, str, str, float], ...] = ()
    period_end: str | None = None
    statements: tuple[str, ...] = ()


def _classify_absent(  # noqa: C901, PLR0912 - one return per class, as the docstring says
    own: set[str],
    values: dict[float, set[str]],
    stored_facts: dict[float, set[str]] | None,
    drops: _Drops,
) -> tuple[str, Any]:
    """The class of a stored value that is **absent** (P3 round 1, reviewer A).

    The SEC holds a non-dimensional fact of one of the item's own concepts for the same accession
    and period, but the run stored nothing for it. That is three findings, not one:

    * ``FAIL_gateway_dropped`` -- the gateway **dropped** the row: the run's drop log (item K)
      names the correction whose original value equals the SEC's fact. The gateway regression;
    * ``WARN_other_concept`` -- the value **is** stored, under another concept the item does not
      read (changing an item's concept set needs a rule per item, ID-02a/ID-02b, so this PR does
      not);
    * ``WARN_never_served`` -- the gateway never served the row (notes only): the pipeline reads
      the face statements, so this is by design.

    *stored_facts* maps a stored value to the concepts it is stored under; *drops* is the run's
    drop log for this accession (see :class:`_Drops`).

    The dropped row must be one of the item's **own** concepts, in the filing's own period, on the
    item's own statement, and worth what the SEC filed: the gateway's own concept and the SEC's
    need not be the same one (PHM filed its revenue as ``us-gaap:Revenues``; the gateway dropped
    the row it had tagged ``RevenueFromContractWithCustomerExcludingAssessedTax``).

    The statement matters: every registry item reads one statement, and ``cash`` reads the balance
    sheet, while the gateway drops the cash-flow
    ``CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`` rows by design (PHM's four and
    Agilent's six are all ``cash_flow``). Those must never be a failure for a balance-sheet item.
    """
    own_facts = {v: cs for v, cs in values.items() if own & cs}
    if not own_facts:
        return "none", None
    if drops.rows:
        for statement, concept, column_date, original in drops.rows:
            if concept not in own:
                continue  # a row of a concept this item does not read
            if drops.statements and statement not in drops.statements:
                continue  # a row of another statement (the gateway drops cash-flow cash by design)
            if drops.period_end is not None and column_date != drops.period_end:
                continue  # a row of another period (a comparative, a year-to-date column)
            for value, concepts in own_facts.items():
                if match_value(original, value):
                    return "FAIL_gateway_dropped", sorted(concepts)[:3]
    if stored_facts:
        for value in own_facts:
            for stored_value, stored_concepts in stored_facts.items():
                if not match_value(stored_value, value):
                    continue
                others = stored_concepts - own
                if others:
                    return "WARN_other_concept", sorted(others)[:3]
    return "WARN_never_served", sorted(set().union(*own_facts.values()))[:3]


def classify(  # noqa: PLR0911, PLR0913 - one return per class of the module docstring
    stored: float | None,
    own: set[str],
    values: dict[float, set[str]],
    mapped: Iterable[float] = (),
    *,
    drops: list[tuple[str, str, str, float]] | None = None,
    stored_facts: dict[float, set[str]] | None = None,
    period_end: str | None = None,
    statements: Iterable[str] | None = None,
) -> tuple[str, Any]:
    """Class of one stored input against the SEC facts of its accession and period (see the module docstring).
    *mapped* are the values of the item's sourced map (:func:`map_values`).
    """
    if stored is None:
        return _classify_absent(
            own,
            values,
            stored_facts,
            _Drops(
                rows=tuple(drops or ()),
                period_end=period_end,
                statements=tuple(statements or ()),
            ),
        )
    hit = [cons for v, cons in values.items() if match_value(v, stored)]
    flip = [cons for v, cons in values.items() if match_value(v, -stored)]
    if any(own & cs for cs in hit):
        return "ok_own_concept", None
    if any(match_value(m, stored) for m in mapped):
        return "ok_sourced_map", None
    if any(own & cs for cs in flip):
        return "FLIPPED", "flip"
    if hit:
        return "OTHER_CONCEPT", sorted(set().union(*hit))[:3]
    if not values:
        return "no_sec_facts_for_accn", None
    return "NOT_IN_SEC(rebuilt/derived)", "derived"


def load_drop_log(path: str | None) -> dict[str, list[tuple[str, str, str, float]]]:
    """The run's gateway drop log (P3 round 1, item K):
    ``{accession: [(statement, concept, date, value)]}``.

    One JSON object per line, written by ``fundamental_agent.pipeline.log_gateway_drops``: every
    gateway correction whose ``corrected`` is null -- a row the gateway dropped -- with the
    statement, the concept, the column, the original value and the reason.

    **Every** row is kept: the gateway nulls the same concept in several columns (PHM's revenue in
    four), and two different rows in one column (Agilent's beginning and ending cash). The concept
    names lose their taxonomy prefix (``us-gaap_``, ``schw_``, ...), so they are the names the
    SEC's facts carry (:func:`own_concepts` strips the prefix too), and the column's date is the
    part before the tag (``2023-09-30 (Q3)`` -> ``2023-09-30``).

    The log is appended on every fetch, so a retry or a resume repeats a row: exact duplicates
    are collapsed here, and the readers count distinct ``(accession, concept, column, original)``.
    """
    if not path:
        return {}
    out: dict[str, list[tuple[str, str, str, float]]] = {}
    seen: set[tuple[str, str, str, float]] = set()
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            row = json.loads(line)
            accession = str(row.get("accession") or "")
            concept = str(row.get("concept") or "")
            column = str(row.get("column") or "")
            original = row.get("original")
            if not (accession and concept and isinstance(original, (int, float))):
                continue
            name = concept.split("_", 1)[1] if "_" in concept else concept
            column_date = column.split(" ", 1)[0]
            key = (accession, name, column_date, float(original))
            if key in seen:
                continue
            seen.add(key)
            statement = str(row.get("statement") or "")
            out.setdefault(accession, []).append((statement, name, column_date, float(original)))
    return out


def stored_facts(conn: Any, filing_id: int, period_end: str) -> dict[float, set[str]]:
    """The filing's own stored facts at its period end: ``{value: {concepts}}``."""
    out: dict[float, set[str]] = collections.defaultdict(set)
    for row in conn.execute(
        "SELECT concept, period_key, value FROM financial_facts WHERE filing_id = ? AND value IS NOT NULL",
        (filing_id,),
    ):
        if str(row[1]).split(" ", 1)[0] != str(period_end):
            continue
        out[float(row[2])].add(str(row[0]))
    return out


def run(
    conn: Any,
    client: SecClient,
    tickers: set[str] | None = None,
    drops: dict[str, list[tuple[str, str, str, float]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    own = own_concepts()
    drops = drops or {}
    results: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    examples: dict[str, list[tuple[Any, ...]]] = collections.defaultdict(list)
    for f in filings(conn):
        if tickers and f["ticker"] not in tickers:
            continue
        docs = [d for c in cik_family(f["cik"]) if (d := client.get("companyfacts", c))]
        dur = facts_of(docs, f["accession_number"], f["period_end"], f["form"], instant=False)
        ins = facts_of(docs, f["accession_number"], f["period_end"], f["form"], instant=True)
        for metric, keys in CHECK.items():
            row = conn.execute(
                "SELECT inputs_json FROM fundamental_metrics WHERE filing_id = ? AND metric_name = ?",
                (f["id"], metric),
            ).fetchone()
            if not row or not row["inputs_json"]:
                continue
            inputs = json.loads(row["inputs_json"])
            for key in keys:
                values = ins if key in INSTANT_ITEMS else dur
                cls, extra = classify(
                    inputs.get(key),
                    own.get(key, set()),
                    values,
                    map_values(key, values),
                    drops=drops.get(str(f["accession_number"])),
                    stored_facts=stored_facts(conn, int(f["id"]), str(f["period_end"])),
                    period_end=str(f["period_end"]),
                    statements=REGISTRY[key].statements,
                )
                results[key][cls] += 1
                if cls in ("FLIPPED", "OTHER_CONCEPT", "NOT_IN_SEC(rebuilt/derived)"):
                    examples[key].append(
                        (f["ticker"], f["form"], f["period_end"], inputs.get(key), extra)
                    )
    return results, examples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db")
    parser.add_argument("--cache", help="SEC cache directory (SEC_XCHECK_CACHE)")
    parser.add_argument("--tickers", help="comma-separated tickers to restrict to")
    parser.add_argument(
        "--drop-log",
        help="the run's gateway drop log (item K): the corrections the gateway nulled",
    )
    args = parser.parse_args(argv)
    conn = open_ro(args.db)
    client = SecClient(cache_dir(args.cache), offline=True)
    tickers = set(args.tickers.split(",")) if args.tickers else None
    results, examples = run(conn, client, tickers, load_drop_log(args.drop_log))
    for key, counts in results.items():
        print(key, dict(counts))
        for ex in examples[key][:5]:
            print("    ", ex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
