#!/usr/bin/env python3
"""V1 (T-143 part A): the market-cap checks on the pilot database.

    uv run python scripts/pilot_a/v1_market_cap.py \
        --db /workspaces/thesis/pilot_a/financial.db --analysis-date 2026-10-18 \
        --out /workspaces/thesis/scratch/t143a/out/p3_v1_market_cap.json

Read-only. Three things, all on the fresh database (production has no cover counts, so only the
fresh one can measure them):

* **the refusal reasons** -- for every filing whose valuation metrics are empty, the reason
  ``kg_schema.market_cap`` gives (K1: the prior filing's cover count is absent because the earlier
  filing lies outside the window; a stale count; no recent price; an unclassified action);
* **the combined-event refusals** -- the caps refused with ``combined_corporate_action`` over every
  filing part A computes. Expected **0** (reviewer A, 2026-10-18). Only an asset with a
  combined-class event can produce that reason, so every such asset is recomputed for all of its
  filings;
* **MKT-02, the cap across every corporate action** -- for every event in the tracked list inside
  the window, the cap on the last session before the ex-date and on the first session after it
  (both resting on the same filed count) and how it moved: a **split** must leave the cap
  continuous, a **spin-off** must move it by the stored-close ratio divided by the event's factor.

Counts, dates, reasons and ratios only -- never a price.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kg_schema.corporate_actions import COMBINED_CLASSES, read_events
from kg_schema.db import connect_ro
from kg_schema.market_cap import market_caps_as_of
from kg_schema.trading_calendar import is_trading_day

#: The classes whose factor never applies to the count (MKT-02: the cap must stay continuous).
SPLIT_CLASSES = ("split", "reverse_split")
SPINOFF_CLASSES = ("spinoff", "reverse_split_spinoff", "split_spinoff")
#: How close to the event the cap is measured: the last session before, the first session after.
CONTINUITY_TOLERANCE = 0.02
#: The same tolerance for the spin-off rule.
RATIO_TOLERANCE = 0.02
#: How many examples to keep per list.
EXAMPLES_LIMIT = 40


def _sessions(start: str, end: str) -> list[str]:
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args))


def refusal_reasons(db: Any, conn: sqlite3.Connection) -> dict[str, Any]:
    """Every filing whose valuation metrics are empty, with the market cap's own reason."""
    rows = _rows(
        conn,
        "SELECT DISTINCT f.id, f.asset_id, a.ticker, f.form, f.fiscal_period, f.period_end "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id JOIN fundamental_metrics m "
        "ON m.filing_id = f.id WHERE m.metric_group = 'valuation' AND m.value IS NULL "
        "ORDER BY a.ticker, f.period_end",
    )
    reasons: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    for row in rows:
        result = market_caps_as_of(db, [int(row["asset_id"])], as_of=str(row["period_end"]))
        reason = result.missing.get(int(row["asset_id"])) or "cap_computed"
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(details) < EXAMPLES_LIMIT and reason != "no_cover_shares":
            details.append(
                {
                    "ticker": row["ticker"],
                    "form": row["form"],
                    "fiscal_period": row["fiscal_period"],
                    "period_end": row["period_end"],
                    "reason": reason,
                }
            )
    return {
        "filings_without_a_valuation_metric": len(rows),
        "reasons": reasons,
        "examples_beyond_no_cover_shares": details,
    }


def combined_event_refusals(db: Any, conn: sqlite3.Connection) -> dict[str, Any]:
    """The caps refused with ``combined_corporate_action`` over every filing part A computes."""
    events = read_events()
    combined_assets: dict[int, list[str]] = {}
    for event in events:
        if event.event_class not in COMBINED_CLASSES:
            continue
        for row in _rows(
            conn,
            "SELECT id, ticker FROM assets WHERE cik = ? OR cik = ?",
            (event.cik, str(event.cik).lstrip("0")),
        ):
            combined_assets.setdefault(int(row["id"]), []).append(
                f"{event.ticker} {event.ex_date} ({event.event_class})"
            )
    filings = _rows(
        conn,
        "SELECT f.id, f.asset_id, a.ticker, f.form, f.fiscal_period, f.period_end "
        "FROM sec_filings f JOIN assets a ON a.id = f.asset_id ORDER BY a.ticker, f.period_end",
    )
    refusals: list[dict[str, Any]] = []
    checked = 0
    for row in filings:
        if int(row["asset_id"]) not in combined_assets:
            continue
        checked += 1
        result = market_caps_as_of(db, [int(row["asset_id"])], as_of=str(row["period_end"]))
        reason = result.missing.get(int(row["asset_id"]))
        if reason == "combined_corporate_action":
            refusals.append(
                {
                    "ticker": row["ticker"],
                    "form": row["form"],
                    "fiscal_period": row["fiscal_period"],
                    "period_end": row["period_end"],
                    "events": combined_assets[int(row["asset_id"])],
                }
            )
    return {
        "assets_with_a_combined_class_event": len(combined_assets),
        "filings_of_those_assets_checked": checked,
        "combined_corporate_action_refusals": len(refusals),
        "refusals": refusals,
    }


def cap_across_events(db: Any, conn: sqlite3.Connection, analysis_date: str) -> dict[str, Any]:
    """MKT-02: the cap before and after every event inside the window, and how it moved."""
    sessions = _sessions("2022-01-03", analysis_date)
    events = read_events()
    asset_of_cik: dict[str, list[int]] = {}
    for row in _rows(conn, "SELECT id, cik, ticker FROM assets"):
        asset_of_cik.setdefault(str(row["cik"]), []).append(int(row["id"]))
    checked: list[dict[str, Any]] = []
    for event in events:
        if not ("2022-01-03" <= event.ex_date <= analysis_date):
            continue
        assets = asset_of_cik.get(str(event.cik), [])
        if not assets:
            continue
        asset_id = assets[0]
        before = [s for s in sessions if s < event.ex_date]
        after = [s for s in sessions if s > event.ex_date]
        if not before or not after:
            continue
        before_date, after_date = before[-1], after[0]
        cap_before = market_caps_as_of(db, [asset_id], as_of=before_date)
        cap_after = market_caps_as_of(db, [asset_id], as_of=after_date)
        one = cap_before.caps.get(asset_id)
        two = cap_after.caps.get(asset_id)
        entry: dict[str, Any] = {
            "ticker": event.ticker,
            "cik": event.cik,
            "ex_date": event.ex_date,
            "event_class": event.event_class,
            "factor": event.value,
            "session_before": before_date,
            "session_after": after_date,
            "cap_before": None if one is None else round(one.value, 4),
            "cap_after": None if two is None else round(two.value, 4),
            "count_filing_before": None if one is None else one.filing_id,
            "count_filing_after": None if two is None else two.filing_id,
            "same_filed_count": (
                one is not None and two is not None and one.filing_id == two.filing_id
            ),
            "reason_before": cap_before.missing.get(asset_id),
            "reason_after": cap_after.missing.get(asset_id),
        }
        if one is not None and two is not None and entry["same_filed_count"]:
            ratio = two.value / one.value if one.value else None
            entry["cap_ratio"] = None if ratio is None else round(ratio, 6)
            if event.event_class in SPLIT_CLASSES:
                entry["rule"] = "the cap must be continuous across a split"
                entry["ok"] = ratio is not None and abs(ratio - 1.0) <= CONTINUITY_TOLERANCE
            elif event.event_class in SPINOFF_CLASSES:
                entry["rule"] = "the cap ratio = the stored-close ratio / the event's factor"
                close_before = one.close
                close_after = two.close
                close_ratio = close_after / close_before if close_before else None
                entry["close_ratio"] = None if close_ratio is None else round(close_ratio, 6)
                expected = None if close_ratio is None else close_ratio / event.value
                entry["expected_cap_ratio"] = None if expected is None else round(expected, 6)
                entry["ok"] = (
                    expected is not None
                    and ratio is not None
                    and abs(ratio - expected) <= RATIO_TOLERANCE
                )
            else:
                entry["rule"] = "no cap rule for this class"
                entry["ok"] = None
        else:
            entry["rule"] = "not measured (no cap on one side, or a different filed count)"
            entry["ok"] = None
        checked.append(entry)
    measured = [e for e in checked if e["ok"] is not None]
    failed = [e for e in measured if not e["ok"]]
    return {
        "events_in_the_window": len(checked),
        "measured": len(measured),
        "ok": len(measured) - len(failed),
        "failed": len(failed),
        "not_measured": len(checked) - len(measured),
        "failures": failed,
        "detail": checked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    db = connect_ro(args.db)
    with contextlib.closing(connect_ro(args.db)) as conn:
        conn.row_factory = sqlite3.Row
        doc = {
            "db": args.db,
            "analysis_date": args.analysis_date,
            "refusal_reasons": refusal_reasons(db, conn),
            "combined_event_refusals": combined_event_refusals(db, conn),
            "cap_across_events": cap_across_events(db, conn, args.analysis_date),
        }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    print(
        json.dumps(
            {
                "refusal_reasons": doc["refusal_reasons"]["reasons"],
                "combined_corporate_action_refusals": doc["combined_event_refusals"][
                    "combined_corporate_action_refusals"
                ],
                "cap_across_events": {
                    k: doc["cap_across_events"][k]
                    for k in ("events_in_the_window", "measured", "ok", "failed", "not_measured")
                },
            },
            indent=1,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
