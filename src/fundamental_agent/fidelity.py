"""The post-rebuild fidelity gate (T-151 part 3, D16).

``python -m fundamental_agent fidelity --db PATH --companyfacts-dir DIR [--golden FILE]`` compares,
read-only towards the database, every filing's **resolved line items** with the SEC's own
``companyfacts`` for the same **accession and period** (the matching rule of
``scripts/sec_xcheck/xcheck.py``: same accession, concept and period end, and the period key's tag
against the fact's duration). A value agrees within **DQ-01**'s tolerance,
``max(L-10 rounding, 0.5% x |value|)``.

The gate is a **minimum fidelity per item** (default :data:`MIN_FIDELITY`, 98%): a value a filer's own
SEC data contradicts should not reach the scores. This module only **reports** -- wiring the gate into
the cycle is the next step (see ``docs/model_fixes.md``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from fundamental_agent import db
from fundamental_agent.dq01 import dq01_tolerance as _dq01_tolerance
from fundamental_agent.statements import (
    DA_SOURCED_CHILDREN,
    LONG_TERM_DEBT_CHILDREN,
    LONG_TERM_DEBT_NONCURRENT_CHILDREN,
    REDEEMABLE_NCI_CONCEPTS,
    REGISTRY,
)

# The gate: a minimum share of the compared values that must match, per item. A constant with a
# comment (D16); the measured rates are reported so the reviewer can set it.
MIN_FIDELITY = 0.98

DURATION_KEY = re.compile(r"^(\d{4}-\d{2}-\d{2}) \((\w+)\)$")
INSTANT_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FY_DAYS = (340, 380)
QUARTER_DAYS = (75, 105)
YTD_DAYS = (160, 290)


def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days


def parse_period_key(key: str) -> tuple[str, str] | None:
    """``(end, tag)`` of a stored period key (``scripts/sec_xcheck/xcheck.py``)."""
    match = DURATION_KEY.match(key)
    if match:
        return match.group(1), match.group(2)
    if INSTANT_KEY.match(key):
        return key, "INST"
    return None


def _kind_ok(tag: str, start: str | None, end: str) -> bool:
    if tag == "INST":
        return start is None
    if start is None:
        return False
    days = _days(start, end)
    if tag == "FY":
        return FY_DAYS[0] <= days <= FY_DAYS[1]
    if tag.startswith("Q"):
        return QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]
    if tag == "YTD":
        return YTD_DAYS[0] <= days <= YTD_DAYS[1]
    return False


def dq01_tolerance(value: float, n_terms: int = 1) -> float:
    """DQ-01's threshold: ``max(L-10 rounding, 0.5% x |value|)`` -- defined in
    :mod:`fundamental_agent.dq01` (T-071 part 2b, D6) and re-exported here for this module's
    callers."""
    return _dq01_tolerance(value, n_terms)


def _iter_facts(
    doc: dict[str, Any], taxonomies: tuple[str, ...] = ("us-gaap", "dei")
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for tax in taxonomies:
        for concept, group in doc.get("facts", {}).get(tax, {}).items():
            for unit, unit_facts in group.get("units", {}).items():
                for fact in unit_facts:
                    fact.setdefault("_unit", unit)
                    yield tax, concept, fact


def _index_by_accession(doc: dict[str, Any]) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    out: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for tax, concept, fact in _iter_facts(doc):
        out.setdefault(str(fact.get("accn") or ""), []).append((tax, concept, fact))
    return out


def _candidates(
    index: dict[str, list[tuple[str, str, dict[str, Any]]]],
    accn: str,
    concept: str,
    end: str,
    tag: str,
) -> list[float]:
    tax, _, name = concept.partition("_")
    return [
        float(fact["val"])
        for t, c, fact in index.get(accn, [])
        if t == tax
        and c == name
        and str(fact.get("end")) == end
        and _kind_ok(tag, fact.get("start"), end)
    ]


def aggregate_concept(item: str, concept: str) -> str | None:
    """The SEC concept a resolved **map** implements, when one exists (T-147's comparison of a
    filled sum against the filer's own total): the D&A children's sum against
    ``DepreciationDepletionAndAmortization``, the long-term-debt children against
    ``LongTermDebtNoncurrent`` / ``LongTermDebt``, the redeemable-NCI parts against their total,
    and the current+deferred income tax, the revenue streams, the cash map and ``ProfitLoss - NCI``
    against their own totals. ``None`` for a sum with no single counterpart."""
    parts = {p.strip() for p in concept.split(" + ")}
    if item == "depreciation_amortization" and parts <= set(DA_SOURCED_CHILDREN):
        return "us-gaap_DepreciationDepletionAndAmortization"
    if item == "long_term_debt":
        if parts <= set(LONG_TERM_DEBT_NONCURRENT_CHILDREN):
            return "us-gaap_LongTermDebtNoncurrent"
        if parts <= set(LONG_TERM_DEBT_CHILDREN):
            return "us-gaap_LongTermDebt"
    if item == "redeemable_nci" and parts <= set(REDEEMABLE_NCI_CONCEPTS[1:]):
        return REDEEMABLE_NCI_CONCEPTS[0]
    return {
        "income_tax": "us-gaap_IncomeTaxExpenseBenefit",
        "revenue": "us-gaap_Revenues",
        "cash": "us-gaap_CashAndCashEquivalentsAtCarryingValue",
        "net_income": "us-gaap_NetIncomeLoss",
    }.get(item)


def _companyfacts(companyfacts_dir: str, cik: str) -> dict[str, Any] | None:
    path = Path(companyfacts_dir) / f"CIK{int(cik):010d}.json"
    if not path.exists():
        return None
    return cast("dict[str, Any]", json.loads(path.read_text()))


def _own_period_key(stmts: Any, form: str, period_end: str) -> str | None:
    for period in stmts.periods:
        if period.date != period_end:
            continue
        if (form.startswith("10-K") and period.is_fy) or (
            form.startswith("10-Q") and period.is_quarter
        ):
            return str(period.key)
    return None


def run_fidelity(  # noqa: C901, PLR0912, PLR0915 - one linear pass, like the audit scripts
    conn: Any, companyfacts_dir: str, *, golden: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Compare every filing's resolved line items with the SEC's companyfacts. Returns the report
    text and the mismatching (filing, item) rows."""
    per_item: dict[tuple[str, str], Counter[str]] = {}
    mismatches: list[dict[str, Any]] = []
    filings_compared = 0
    for asset in conn.execute("SELECT id, ticker, cik FROM assets WHERE cik IS NOT NULL"):
        doc = _companyfacts(companyfacts_dir, str(asset["cik"]))
        if doc is None:
            continue
        index = _index_by_accession(doc)
        for filing in conn.execute(
            "SELECT id, form, period_end, accession_number FROM sec_filings "
            "WHERE asset_id = ? AND accession_number IS NOT NULL",
            (int(asset["id"]),),
        ):
            stmts = db.statements_from_facts(conn, int(filing["id"]))
            own_key = _own_period_key(stmts, str(filing["form"]), str(filing["period_end"]))
            if own_key is None:
                continue
            filings_compared += 1
            accn = str(filing["accession_number"])
            # G5 (b): a 10-Q's TTM is built from its YTD column, so every YTD duration column that
            # ends on the filing's period end is compared too, reported per column kind.
            keys = [("own", own_key)]
            for period in stmts.periods:
                if period.tag == "YTD" and period.date == str(filing["period_end"]):
                    keys.append(("ytd", str(period.key)))
            for kind, key in keys:
                for item in REGISTRY:
                    # H5: a balance-sheet item is an instant fact (ID-05) with no year-to-date
                    # column -- the ytd kind applies to duration items only, so an instant item is
                    # compared once, on the own column.
                    if kind == "ytd" and REGISTRY[item].statements == ("balance_sheet",):
                        continue
                    resolution = stmts.resolve(item, key)
                    if resolution.value is None:
                        continue
                    concept = resolution.concept or ""
                    n_terms = 1
                    if " + " in concept:
                        # a map: compare the sum against the aggregate concept it implements, when
                        # one exists (T-147's filled-sum-vs-total comparison); else skip
                        aggregate = aggregate_concept(item, concept)
                        if aggregate is None:
                            continue
                        n_terms = len(concept.split(" + ")) + 1  # the parts + the filer's own total
                        concept = aggregate
                    elif not concept.startswith(("us-gaap_", "dei_")):
                        # a derived or extension value: companyfacts carries no such fact
                        continue
                    # a balance-sheet item is an instant fact (ID-05): its own period key is the
                    # instant column, whose tag is INST
                    column = key
                    if REGISTRY[item].statements == ("balance_sheet",):
                        instant = stmts._instant_for(key)
                        if instant is not None:
                            column = instant
                    parsed = parse_period_key(column)
                    if parsed is None:
                        continue
                    candidates = _candidates(index, accn, concept, *parsed)
                    stats = per_item.setdefault((item, kind), Counter())
                    if not candidates:
                        stats["no_sec_fact"] += 1
                        continue
                    stats["compared"] += 1
                    tolerance = dq01_tolerance(resolution.value, n_terms)
                    if any(abs(c - resolution.value) <= tolerance for c in candidates):
                        stats["matched"] += 1
                    else:
                        stats["mismatched"] += 1
                        mismatches.append(
                            {
                                "ticker": str(asset["ticker"]),
                                "accession": accn,
                                "form": str(filing["form"]),
                                "column": kind,
                                "period_key": key,
                                "item": item,
                                "stored": resolution.value,
                                "sec": candidates[0],
                                "concept": concept,
                            }
                        )

    lines = [
        "Fidelity gate (T-151 part 3, D16)",
        f"companyfacts dir: {companyfacts_dir}",
        f"filings compared: {filings_compared}",
        f"gate: minimum fidelity per item {MIN_FIDELITY:.0%} "
        f"(a constant; the measured rates are below)",
        "",
        f"{'item':<30}{'kind':<6}{'compared':>10}{'matched':>10}{'mismatched':>12}"
        f"{'no fact':>10}{'fidelity':>10}",
    ]
    below: list[str] = []
    for item, kind in sorted(per_item):
        stats = per_item[(item, kind)]
        compared = stats["compared"]
        matched = stats["matched"]
        fidelity = matched / compared if compared else 1.0
        lines.append(
            f"{item:<30}{kind:<6}{compared:>10}{matched:>10}{stats['mismatched']:>12}"
            f"{stats['no_sec_fact']:>10}{fidelity:>9.1%}"
        )
        if compared and fidelity < MIN_FIDELITY:
            below.append(f"{item}[{kind}] {fidelity:.1%} ({stats['mismatched']}/{compared})")
    lines += [
        "",
        f"items below the gate: {len(below)}" + (f" — {', '.join(below)}" if below else ""),
        f"mismatching (filing, item) pairs: {len(mismatches)}",
    ]
    if golden:
        golden_path = Path(golden)
        lines.append(
            f"golden set: {'compared from ' + str(golden_path) if golden_path.exists() else str(golden_path) + ' not found (RECORDED)'}"
        )
    else:
        lines.append(
            "golden set: no golden_set.csv given or found (RECORDED; docs/sec_golden_set.md is prose)"
        )
    lines += ["", "mismatches (first 40):"]
    for row in mismatches[:40]:
        lines.append(
            f"  {row['ticker']:<6}{row['accession']:<22}{row['item']:<26}"
            f"stored {row['stored']!r:>18} sec {row['sec']!r:<18} {row['concept']}"
        )
    return "\n".join(lines) + "\n", mismatches
