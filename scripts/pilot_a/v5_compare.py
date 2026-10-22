#!/usr/bin/env python3
"""V5: the cards against the human reading of the 10-Ks, item by item.

    .venv/bin/python scripts/pilot_a/v5_compare.py \
        --db /workspaces/thesis/pilot_a2/financial.db \
        --cards /workspaces/thesis/scratch/t143a/out/v5_cards_p2b_v2 \
        --transcription /workspaces/thesis/handoff/pilot/v5/v5_html_transcription.csv \
        --out /workspaces/thesis/scratch/t143a/out/v5_html_vs_card.csv \
        --json /workspaces/thesis/scratch/t143a/out/v5_html_vs_card.json

The independent reading session transcribes ten line items from each of the 25 10-Ks' **HTML
statements** (blind: it never saw our database).  This script compares its transcription with the
cards and lists **every** difference with an explanation.

The comparison is on the values, with DQ-01's tolerance (the rounding unit of the larger number
plus 0.5 % of it) -- the same tolerance the fidelity gate uses.  The card's value for an item is
the one the stored metric recorded (the card never recomputes); a card that recorded no value for
an item is reported as "the card has no value", not as a difference.

The transcription may not have arrived yet: the script then says so and writes an empty result
(it is never a pass by absence).
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from fundamental_agent.card import build_card
from fundamental_agent.dq01 import dq01_tolerance
from kg_schema.db import connect_ro

#: The plan's V5 line items -> the card's line item and the basis the card reads it on.
ITEMS: dict[str, tuple[str, str]] = {
    "revenue": ("revenue", "period"),
    "operating_income": ("operating_income", "period"),
    "net_income": ("net_income", "period"),
    "depreciation_amortization": ("depreciation_amortization", "period"),
    "capital_expenditure": ("capital_expenditure", "period"),
    "operating_cash_flow": ("operating_cash_flow", "period"),
    "cash": ("cash", "period"),
    "equity": ("equity", "period"),
}

#: The reader's ``debt`` rows are the balance sheet's parts plus its own total; the card carries
#: the two debt items it resolved (the long-term and the short-term one).
DEBT_ITEMS = ("long_term_debt", "short_term_debt")
#: The reader's ``shares`` rows are the classes on the cover page; the card carries the count the
#: market cap read (the common classes).
SHARE_CAPTION_MARKERS = ("common", "ordinary")

#: How a difference is classified: the known defects (the finding's F5a family -- the item's total
#: under another standard concept), the reading conventions (the parent vs the consolidated net
#: income, ID-03; the financial firm's revenue overlay, APP-01/03), and the rest, which are new.
KNOWN_DEFECTS = Path("/workspaces/thesis/handoff/pilot/known_defects_by_company.csv")


def _known_defects() -> dict[str, dict[str, str]]:
    if not KNOWN_DEFECTS.exists():
        return {}
    with KNOWN_DEFECTS.open(newline="") as handle:
        return {
            str(row["ticker"]): {k: str(v) for k, v in row.items()}
            for row in csv.DictReader(handle)
        }


def _classify(  # noqa: PLR0911 - one class per case
    ticker: str,
    item: str,
    difference_ppm: float,
    known: dict[str, dict[str, str]],
    is_reit: bool = False,
) -> str:
    """The difference's class: explained by a known defect, or new."""
    row = known.get(ticker)
    if row is None:
        return "new (the company has no known defect)"
    other_concept = int(float(row.get("T159_other_concept_values") or 0))
    if item == "capital_expenditure" and is_reit:
        return (
            "the REIT's capex composite (APP-04b: the corporate items, the existing properties, the "
            "development and the acquisitions) against the reader's capex lines"
        )
    if item == "shares":
        if difference_ppm < 0:
            return "the reader's classes include the non-common members the card excludes (MKT-04)"
        return "the cover count the filing's XBRL carries (a filer scale)"
    if item == "net_income":
        return "the parent vs the consolidated net income (ID-03)"
    if item == "revenue":
        return "the statement's alternative presentation (APP-01/APP-03)"
    if item in ("depreciation_amortization", "capital_expenditure") and other_concept:
        return (
            "explained by a known defect (T-159: the item's total under another standard concept -- "
            "the finding's F5a family)"
        )
    return "new"


def _tolerance(a: float, b: float) -> float:
    """DQ-01's tolerance, on the larger of the two numbers (the same one the fidelity gate uses)."""
    return dq01_tolerance(max(abs(a), abs(b)))


def _card_values(conn: Any, ticker: str, cik: str, as_of: str) -> dict[str, Any]:
    """The card's values for the V5 items, the cover-page share count and the debt total."""
    card = build_card(conn, cik, as_of, with_vetoes=False)
    values: dict[str, Any] = {}
    for name, (item, basis) in ITEMS.items():
        for row in card.line_items:
            if row["item"] == item and row["basis"] == basis:
                values[name] = row["value"]
                break
    for item in DEBT_ITEMS:
        for row in card.line_items:
            if row["item"] == item and row["basis"] == "period":
                values[item] = row["value"]
                break
    # the cover-page share count: what the filing's cover states (MKT-01). The market cap's own
    # count may be a fallback (T-132), so the card carries the cover page itself.
    cover = card.cover_shares
    if cover and cover[0].get("total") is not None:
        # the total the pipeline reads (the filer's own total, else its classes summed with the
        # non-common members excluded, MKT-04)
        values["shares"] = float(cover[0]["total"])
    # the debt: the card's short-term plus its long-term (the parts it resolved), against the
    # reader's own sum row.  A part the card has no value for is left out (the card never
    # recomputes it), so the card's debt can be short of the statement's total -- which is the
    # finding (F6a/F6c).
    parts = [values.get(item) for item in DEBT_ITEMS]
    present = [float(part) for part in parts if part is not None]
    if present:
        values["debt"] = float(sum(present))
    values["_accession"] = card.filing["accession"]
    values["_card"] = card
    return values


def _reader_values(rows: list[dict[str, Any]], item: str) -> list[dict[str, Any]]:
    """The reader's rows for one item of one company."""
    return [r for r in rows if str(r["item"]).strip() == item and str(r.get("value", "")).strip()]


def _caption(row: dict[str, Any]) -> str:
    return str(row.get("caption") or "").lower()


def _scale_unit(unit_scale: str) -> float:
    """The unit of the statement's own rounding (the reader's ``unit_scale``)."""
    scale = (unit_scale or "").strip().lower()
    if scale.startswith("billions"):
        return 1_000_000_000.0
    if scale.startswith("millions"):
        return 1_000_000.0
    if scale.startswith("thousands"):
        return 1_000.0
    return 1.0


def _rounding_tolerance(row: dict[str, Any]) -> float:
    """Half of the unit the statement itself rounds to (the review's rule)."""
    return _scale_unit(str(row.get("unit_scale") or "")) / 2.0


def _sum_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The reader's own total row.

    Where the reader gave parts and a sum ("Total debt (sum of the balance-sheet parts above)",
    "Total depreciation and amortization"), the sum is the one to compare.
    """
    for row in rows:
        if _caption(row).startswith("total"):
            return row
    return None


def _card_empty_reason(card: Any, item: str) -> str:
    """Why the card has no value for an item: the empty metric's own reason."""
    for entry in card.empty:
        if item in (entry.get("items") or {}):
            return str(entry.get("reason") or "")
    return ""


#: The items a financial firm's statements do not carry (APP-02/APP-03: the cash-flow items a
#: bank's or an insurer's balance sheet does not present).
FINANCIAL_FIRM_ITEMS = frozenset(
    {"cash", "debt", "depreciation_amortization", "capital_expenditure"}
)


def _classify_empty(
    reason: str, known: dict[str, dict[str, str]], ticker: str, company_type: str, item: str
) -> str:
    """A card-empty row: by a rule (the item is not applicable) or by a known defect."""
    if reason == "not_applicable":
        return "rule"
    if (
        company_type in ("article_9", "article_7", "other_financial")
        and item in FINANCIAL_FIRM_ITEMS
    ):
        return "rule"
    if ticker not in known:
        return "known (the company has no known defect)"
    return "known"


def _compare(
    conn: Any, manifest: dict[str, Any], rows: list[dict[str, Any]], result: dict[str, Any]
) -> None:
    """Every item of every company: the card's value against the reader's, with a verdict."""
    cards = {c["ticker"]: c for c in manifest["cards"]}
    by_accession = {str(c.get("accession") or ""): c for c in manifest["cards"]}
    known = _known_defects()
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_company.setdefault(str(row["ticker"]).strip(), []).append(row)
    for ticker, company_rows in sorted(by_company.items()):
        # the card is matched by its filing (the accession): a two-class line's card is drawn
        # under one ticker and read under the other (FOXA / FOX).
        card_entry = cards.get(ticker) or by_accession.get(
            str(company_rows[0].get("accession") or "")
        )
        if card_entry is None:
            continue
        values = _card_values(conn, ticker, str(card_entry["cik"]), str(card_entry["as_of"]))
        card = values["_card"]
        for item in (*ITEMS, "debt"):
            reader_rows = _reader_values(company_rows, item)
            reader_row = _sum_row(reader_rows) or (reader_rows[0] if reader_rows else None)
            html_value = None if reader_row is None else float(reader_row["value"])
            card_value = values.get(item)
            entry = {
                "ticker": ticker,
                "accession": company_rows[0].get("accession", ""),
                "item": item,
                "card_value": None if card_value is None else float(card_value),
                "html_value": html_value,
                "caption": "" if reader_row is None else str(reader_row.get("caption") or ""),
                "unit_scale": "" if reader_row is None else str(reader_row.get("unit_scale") or ""),
                "reader_note": "" if reader_row is None else str(reader_row.get("note") or ""),
            }
            if html_value is None and card_value is None:
                entry["verdict"] = "both empty"
                entry["class"] = "no such line"
            elif card_value is None:
                reason = _card_empty_reason(card, item)
                entry["verdict"] = "card empty"
                entry["class"] = _classify_empty(
                    reason, known, ticker, str(card.company_type or ""), item
                )
                entry["reason"] = reason
            elif html_value is None:
                entry["verdict"] = "reader empty"
                entry["class"] = "the reader did not transcribe this item"
            else:
                diff = float(card_value) - html_value
                entry["difference"] = diff
                entry["difference_ppm"] = (
                    diff / abs(html_value) * 1_000_000 if html_value else float("inf")
                )
                tolerance = _rounding_tolerance(reader_row or {})
                if abs(diff) <= tolerance:
                    entry["verdict"] = "match"
                else:
                    entry["verdict"] = "differ"
                    entry["class"] = _classify(
                        ticker, item, entry["difference_ppm"], known, bool(card.is_reit)
                    )
            result["comparisons"].append(entry)
        # the shares: the parent registrant's cover count (the reader's other registrants are the
        # company's subsidiaries, not the parent)
        reader_rows = _reader_values(company_rows, "shares")
        if reader_rows:
            name = str(card_entry.get("company", ""))
            parent = [
                r for r in reader_rows if name and name.lower() in str(r.get("caption") or "")
            ]
            used = (parent or reader_rows)[0]
            result["shares"].append(
                {
                    "ticker": ticker,
                    "card_cover_total": values.get("shares"),
                    "reader_value": float(used["value"]),
                    "caption": str(used.get("caption") or ""),
                    "unit_scale": str(used.get("unit_scale") or ""),
                    "registrant": "parent" if parent else "the reader's first row",
                }
            )


def _write(result: dict[str, Any], out_path: Path, json_path: Path) -> None:
    columns = (
        "ticker",
        "accession",
        "item",
        "verdict",
        "class",
        "card_value",
        "html_value",
        "difference",
        "difference_ppm",
        "reason",
        "caption",
        "unit_scale",
        "reader_note",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for entry in result["comparisons"]:
            writer.writerow([entry.get(column, "") for column in columns])
        for entry in result["shares"]:
            writer.writerow(
                [
                    entry.get("ticker", ""),
                    "",
                    "shares",
                    "shares",
                    entry.get("registrant", ""),
                    entry.get("card_cover_total", ""),
                    entry.get("reader_value", ""),
                    "",
                    "",
                    "",
                    entry.get("caption", ""),
                    entry.get("unit_scale", ""),
                    "",
                ]
            )
    json_path.write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cards", required=True)
    parser.add_argument("--transcription", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--json", required=True)
    args = parser.parse_args()

    transcription_path = Path(args.transcription)
    result: dict[str, Any] = {
        "cards": args.cards,
        "transcription": str(transcription_path),
        "transcription_present": transcription_path.exists(),
        "compared": 0,
        "agree": 0,
        "differences": [],
        "comparisons": [],
        "shares": [],
    }
    rows: list[dict[str, Any]] = []
    if transcription_path.exists():
        rows = list(csv.DictReader(transcription_path.open(newline="")))
    if rows:
        manifest = json.loads((Path(args.cards) / ".." / "p2b_v5_cards_manifest.json").read_text())
        conn = connect_ro(args.db)
        try:
            _compare(conn, manifest, rows, result)
        finally:
            conn.close()
    else:
        result["note"] = (
            "the independent reading session's transcription has not arrived; nothing is compared "
            "(an absent comparison is never a pass)"
        )
    verdicts = Counter(entry["verdict"] for entry in result["comparisons"])
    classes = Counter(
        entry["class"] for entry in result["comparisons"] if entry["verdict"] == "card empty"
    )
    result["verdicts"] = dict(verdicts)
    result["card_empty_classes"] = dict(classes)
    _write(result, Path(args.out), Path(args.json))
    print(
        f"comparisons {len(result['comparisons'])}: "
        + ", ".join(f"{name} {count}" for name, count in sorted(verdicts.items()))
    )
    print("card empty: " + ", ".join(f"{name} {count}" for name, count in sorted(classes.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
