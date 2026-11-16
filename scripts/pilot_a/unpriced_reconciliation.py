#!/usr/bin/env python3
"""P2 reconciliation: the lines step 1 skipped (no gateway candle) that step 2 did not import.

    uv run python scripts/pilot_a/unpriced_reconciliation.py \
        --db /workspaces/thesis/pilot_a/financial.db \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --baseline docs/leaver_price_coverage_2019.json \
        --manifest /workspaces/thesis/data/private/tiingo/leaver_prices.manifest.json \
        --analysis-date 2026-10-18 --out /workspaces/thesis/scratch/t143a/out/unpriced_reconciliation.json

Read-only. Step 1 priced the gateway-served lines; step 2 imported T-153 part 3d's Tiingo file. A
line that the gateway served **no candle** for and the file does **not** carry has **no prices at
all** in this database. For each such line it prints the ticker, the CIK and the line's membership
span in ``[window_start, D]``, and says whether it is in the committed baseline
``docs/leaver_price_coverage_2019.json`` and whether it is in part 3d's imported set (the manifest's
``lines``). The known set is the manifest's ``not_delivered``; if the observed set differs, the script
prints that too, so the report cannot quietly drop a line.

Counts, tickers, CIKs and dates only. The manifest is read for its line **names** and the
``not_delivered`` reasons alone -- never a vendor value (no price, volume or dividend).
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
from typing import Any

from kg_schema.queries import WINDOW_START, connect_ro, members_over_range


def _spans(universe_db: str, fin_db: str, analysis_date: str) -> dict[str, dict[str, Any]]:
    """Every line of the window, keyed by ticker: its CIK and its membership span."""
    with (
        contextlib.closing(connect_ro(universe_db)) as universe,
        contextlib.closing(connect_ro(fin_db)) as fin,
    ):
        window = members_over_range(universe, fin, WINDOW_START, analysis_date)
    return {
        w.member.symbol: {
            "cik": str(w.member.cik or ""),
            "window_from": w.window_from,
            "window_to": w.window_to,
            "stints": w.stints,
            "still_member_at_D": w.window_to is None,
        }
        for w in window
    }


def _unpriced(db: str) -> list[dict[str, str]]:
    """Every asset with no ``price_daily`` row: no gateway candle and no import row."""
    with contextlib.closing(connect_ro(db)) as conn:
        return [
            {"ticker": str(r["ticker"]), "cik": str(r["cik"] or "")}
            for r in conn.execute(
                "SELECT a.ticker, a.cik FROM assets a "
                "WHERE NOT EXISTS (SELECT 1 FROM price_daily d WHERE d.asset_id = a.id) "
                "ORDER BY a.ticker"
            )
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--universe-db", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    unpriced = _unpriced(args.db)
    spans = _spans(args.universe_db, args.db, args.analysis_date)

    baseline = json.loads(Path(args.baseline).read_text())
    baseline_by_ask = {r["ask_for"]: r["status"] for r in baseline["detail"]}

    manifest = json.loads(Path(args.manifest).read_text())
    imported = {line["ask"] for line in manifest["lines"]}
    not_delivered = {line["ask"]: line["reason"] for line in manifest["not_delivered"]}

    detail: list[dict[str, Any]] = []
    for row in unpriced:
        ticker = row["ticker"]
        detail.append(
            {
                "ticker": ticker,
                "cik": row["cik"],
                "membership_span": spans.get(ticker),
                "in_committed_baseline": ticker in baseline_by_ask,
                "baseline_status": baseline_by_ask.get(ticker),
                "in_part3d_imported_set": ticker in imported,
                "declared_not_delivered": not_delivered.get(ticker),
            }
        )

    observed = {r["ticker"] for r in detail}
    doc = {
        "analysis_date": args.analysis_date,
        "window_start": WINDOW_START,
        "db": args.db,
        "baseline": args.baseline,
        "manifest": args.manifest,
        "unpriced_lines": len(detail),
        "imported_set_lines": len(imported),
        "not_delivered_lines": sorted(not_delivered),
        "observed_equals_known": observed == set(not_delivered),
        "observed_not_in_known": sorted(observed - set(not_delivered)),
        "known_not_observed": sorted(set(not_delivered) - observed),
        "detail": detail,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"lines with no prices at all: {len(detail)}")
    for r in detail:
        span = r["membership_span"] or {}
        print(
            f"  {r['ticker']:5s} cik={r['cik']} span={span.get('window_from')}..{span.get('window_to')}"
            f" stints={span.get('stints')} baseline={r['baseline_status']}"
            f" in_imported_set={r['in_part3d_imported_set']}"
            f" declared_not_delivered={r['declared_not_delivered']}"
        )
    print(
        f"observed == the manifest's not_delivered set: {doc['observed_equals_known']}"
        f" (not in known: {doc['observed_not_in_known']};"
        f" known but priced: {doc['known_not_observed']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
