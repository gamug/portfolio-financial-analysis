#!/usr/bin/env python3
"""8A: the current lines' gateway coverage -- complete, and not forgotten (T-143 part A pre-flight).

    uv run python scripts/pilot_a/current_line_price_coverage.py \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --production-db /workspaces/thesis/data/financial.db \
        --archive-db /workspaces/thesis/data/archive/t153c_r4_gateway_2026-10-14/financial.db \
        --analysis-date D --out out/current_line_price_coverage.json

Read-only: gateway GETs, and the databases opened ``mode=ro``. For every line
``kg_schema.queries.members_over_range`` returns with ``window_to is None`` (still a member at D),
the twin of ``leaver_price_coverage_2019.py`` (which covers the leavers):

* **(i) complete** -- the gateway's days against the NYSE sessions from
  ``max(2019-01-02, the line's first served day)`` to D. Days before a stock first traded are not
  required;
* **(ii) not forgotten** -- the gateway's days over production's stored range at least production's
  stored ``price_daily`` count for the line (the WBD precedent: a line the gateway forgets).

A line failing either is listed with both coverages and whether the R4 archive covers its whole
span. Counts, dates and hashes only -- never a price. Nothing is seeded and no vendor switches:
A's stop is the reviewer's.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from kg_schema.queries import WINDOW_START, connect_ro, members_over_range
from kg_schema.trading_calendar import is_trading_day
from pricing_agent.config import DEFAULT_PRICING_BASE_URL
from pricing_agent.pricing_client import PricingClient

START = "2019-01-02"


@dataclass(frozen=True)
class Coverage:
    """One line's coverage from one source (counts and dates only)."""

    days: int
    first: str | None
    last: str | None

    @classmethod
    def of(cls, dates: set[str]) -> Coverage:
        return cls(len(dates), min(dates) if dates else None, max(dates) if dates else None)


def _sessions(start: str, end: str) -> list[str]:
    """Every NYSE session in ``[start, end]``, from the shared calendar."""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _sha1(path: str) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()  # noqa: S324 - a change check


def _stored_days(db_path: str, cik: str, ticker: str) -> set[str]:
    """The line's stored ``price_daily`` days, resolved through its own CIK line (read-only)."""
    with contextlib.closing(connect_ro(db_path)) as conn:
        rows = conn.execute("SELECT id, ticker FROM assets WHERE cik = ?", (cik,)).fetchall()
        matching = [r for r in rows if str(r["ticker"]).upper() == ticker.upper()]
        ids = [int(r["id"]) for r in (matching or rows)]
        days: set[str] = set()
        for asset_id in ids:
            days |= {
                str(r["date"])
                for r in conn.execute(
                    "SELECT date FROM price_daily WHERE asset_id = ?", (asset_id,)
                )
            }
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-db", required=True)
    parser.add_argument("--production-db", required=True)
    parser.add_argument("--archive-db", required=True)
    parser.add_argument("--fin-db", help="the database whose ticker_history names the lines")
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with (
        contextlib.closing(connect_ro(args.universe_db)) as universe,
        contextlib.closing(connect_ro(args.fin_db or args.production_db)) as fin,
    ):
        window = members_over_range(universe, fin, WINDOW_START, args.analysis_date)
    current = [w for w in window if w.window_to is None]

    detail: list[dict[str, Any]] = []
    with PricingClient(DEFAULT_PRICING_BASE_URL) as client:
        for w in sorted(current, key=lambda w: w.member.symbol):
            ticker, cik = w.member.symbol, str(w.member.cik or "")
            try:
                served = {
                    c.date
                    for c in client.daily_any_spelling(ticker, START, args.analysis_date).candles
                }
                served = {d for d in served if d <= args.analysis_date}
                error = None
            except Exception as exc:  # a dead line is a gap, not a crash
                served, error = set(), f"{type(exc).__name__}: {exc}"
            first = min(served) if served else None
            span_start = max(START, first) if first else START
            expected = set(_sessions(span_start, args.analysis_date))
            complete = expected <= served
            prod = _stored_days(args.production_db, cik, ticker)
            archive = _stored_days(args.archive_db, cik, ticker)
            prod_range = prod
            gateway_over_prod = served & prod_range
            prod_covered = len(gateway_over_prod) >= len(prod)
            archive_covers = expected <= archive
            failing = (not complete) or (not prod_covered) or error is not None
            detail.append(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "gateway": asdict(Coverage.of(served)),
                    "expected_sessions": len(expected),
                    "first_trade": first,
                    "complete": complete,
                    "production": asdict(Coverage.of(prod)),
                    "gateway_over_production": len(gateway_over_prod),
                    "not_forgotten": prod_covered,
                    "archive": asdict(Coverage.of(archive)),
                    "archive_covers_span": archive_covers,
                    "error": error,
                    "failing": failing,
                }
            )

    failing = [r for r in detail if r["failing"]]
    doc = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gateway": DEFAULT_PRICING_BASE_URL,
        "analysis_date": args.analysis_date,
        "range_start": START,
        "calendar": "kg_schema.trading_calendar (NYSE sessions)",
        "universe_db": args.universe_db,
        "universe_sha1": _sha1(args.universe_db),
        "production_db": args.production_db,
        "production_sha1": _sha1(args.production_db),
        "archive_db": args.archive_db,
        "lines": len(detail),
        "failing": len(failing),
        "detail": detail,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "lines": doc["lines"],
                "failing": doc["failing"],
                "incomplete": sum(1 for r in detail if not r["complete"]),
                "forgotten": sum(1 for r in detail if not r["not_forgotten"]),
                "errors": sum(1 for r in detail if r["error"]),
            },
            indent=1,
            sort_keys=True,
        )
    )
    for r in failing:
        print(
            f"  {r['ticker']} cik={r['cik']} gateway={r['gateway']['days']} "
            f"expected={r['expected_sessions']} first_trade={r['first_trade']} "
            f"complete={r['complete']} production={r['production']['days']} "
            f"gateway_over_production={r['gateway_over_production']} "
            f"not_forgotten={r['not_forgotten']} archive_covers_span={r['archive_covers_span']} "
            f"error={r['error']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
