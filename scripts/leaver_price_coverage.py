"""D8 (T-153 part 3a): the price coverage of every leaver stint over its whole membership.

    uv run python scripts/leaver_price_coverage.py --universe-db PATH [--window-start D]
        [--out FILE] [--offline]

Read-only. For every stint of a company that left the S&P 500 inside the backtest window
(``valid_to >= --window-start``, and the CIK has no open stint today), it asks the pricing
gateway **once**, under the line's **canonical ticker** (T-153 part 2, plus the post-exit
renames part 3a's D4 adds -- GPS is measured under GAP, FBHS under FBIN), for the sessions of
its membership inside the window. It records the expected trading days
(``kg_schema.trading_calendar``, not a vendor calendar), the priced days, the first and last
missing day, and each stint's status (``full`` / ``partial`` / ``zero``).

The output is committed (``docs/leaver_price_coverage.json``) so the request to
``portfolio-data-mining`` (``docs/upstream_requests/data_mining_leaver_prices.md``) and the
PER-11 declaration both cite a reproducible measurement. ``--offline`` reads no gateway and
writes no output: it only lists the stints and their expected days.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kg_schema.queries import WINDOW_START, connect_ro
from kg_schema.ticker_history import read_history
from kg_schema.trading_calendar import is_trading_day
from pricing_agent.config import DEFAULT_PRICING_BASE_URL
from pricing_agent.pricing_client import PricingClient

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "docs" / "leaver_price_coverage.json"


@dataclass(frozen=True)
class Stint:
    """One membership stint of a company that left the index inside the window."""

    symbol: str
    cik: str
    security: str
    valid_from: str
    valid_to: str


def leaver_stints(universe: Any, window_start: str = WINDOW_START) -> list[Stint]:
    """Every stint whose ``valid_to`` is on or after *window_start* and whose CIK is not a
    member today -- the companies that left the index inside the window (T-153 part 3a)."""
    rows = universe.execute(
        "SELECT symbol, security, cik, valid_from, valid_to FROM universe_membership "
        "WHERE cik IS NOT NULL AND valid_to IS NOT NULL AND valid_to >= ? "
        "ORDER BY valid_to, symbol",
        (window_start,),
    ).fetchall()
    open_ciks = {
        str(r["cik"])
        for r in universe.execute(
            "SELECT DISTINCT cik FROM universe_membership "
            "WHERE cik IS NOT NULL AND valid_to IS NULL"
        )
    }
    return [
        Stint(
            symbol=str(r["symbol"]),
            cik=str(r["cik"]),
            security=str(r["security"]),
            valid_from=str(r["valid_from"]),
            valid_to=str(r["valid_to"]),
        )
        for r in rows
        if str(r["cik"]) not in open_ciks
    ]


def canonical_by_key() -> dict[tuple[str, str], str]:
    """``(CIK, symbol) -> the line's canonical symbol`` from the tracked ticker history."""
    return {(r.cik, r.symbol): r.canonical_symbol for r in read_history()}


def expected_days(start: str, end: str) -> list[str]:
    """The NYSE sessions in ``[start, end]`` (``kg_schema.trading_calendar``)."""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def coverage(days: list[str], priced: set[str]) -> tuple[int, str | None, str | None]:
    """``(priced_days, first_missing, last_missing)`` for *days* against *priced*."""
    missing = [d for d in days if d not in priced]
    return (
        len(days) - len(missing),
        (missing[0] if missing else None),
        (missing[-1] if missing else None),
    )


def status(priced: int, expected: int) -> str:
    """``full`` (every expected day), ``zero`` (none) or ``partial`` (some)."""
    if expected == 0:
        return "empty"
    if priced == 0:
        return "zero"
    return "full" if priced == expected else "partial"


def measure(client: Any, ask_for: str, start: str, end: str) -> list[str]:
    """The candle dates the gateway serves for *ask_for* in ``[start, end]`` (one request)."""
    prices = client.daily_any_spelling(ask_for, start, end)
    return [c.date for c in prices.candles if start <= c.date <= end]


def measure_all(
    universe_path: str, client: Any, *, window_start: str = WINDOW_START
) -> dict[str, Any]:
    """The full measurement, as the committed JSON document."""
    canonical = canonical_by_key()
    with contextlib.closing(connect_ro(universe_path)) as universe:
        stints = leaver_stints(universe, window_start)
    sha1 = hashlib.sha1(Path(universe_path).read_bytes()).hexdigest()  # noqa: S324 - a fingerprint
    rows: list[dict[str, Any]] = []
    for stint in stints:
        start = max(stint.valid_from, window_start)
        ask_for = canonical.get((stint.cik, stint.symbol), stint.symbol)
        days = expected_days(start, stint.valid_to)
        priced = set(measure(client, ask_for, start, stint.valid_to)) if days else set()
        n_priced, first_missing, last_missing = coverage(days, priced)
        rows.append(
            {
                "symbol": stint.symbol,
                "cik": stint.cik,
                "company": stint.security,
                "valid_from": stint.valid_from,
                "valid_to": stint.valid_to,
                "ask_for": ask_for,
                "expected_days": len(days),
                "priced_days": n_priced,
                "first_missing": first_missing,
                "last_missing": last_missing,
                "status": status(n_priced, len(days)),
            }
        )
    counts = {
        name: sum(1 for r in rows if r["status"] == name)
        for name in ("full", "partial", "zero", "empty")
    }
    return {
        "universe_db": str(universe_path),
        "universe_sha1": sha1,
        "window_start": window_start,
        "calendar": "kg_schema.trading_calendar (NYSE sessions)",
        "stints": len(rows),
        "lines": len({(r["cik"], r["ask_for"]) for r in rows}),
        "companies": len({r["cik"] for r in rows}),
        "totals": counts,
        "detail": rows,
    }


def _gateway() -> str:
    return os.environ.get("PRICING_BASE_URL", DEFAULT_PRICING_BASE_URL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--universe-db", required=True, help="universe_history.db (read-only)")
    ap.add_argument("--window-start", default=WINDOW_START)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--offline", action="store_true", help="list the stints only; no gateway, no output"
    )
    args = ap.parse_args()

    if args.offline:
        with contextlib.closing(connect_ro(args.universe_db)) as universe:
            stints = leaver_stints(universe, args.window_start)
        for stint in stints:
            start = max(stint.valid_from, args.window_start)
            print(
                f"{stint.symbol:6} {stint.cik} {start}..{stint.valid_to} "
                f"{len(expected_days(start, stint.valid_to))} days"
            )
        print(f"{len(stints)} leaver stints")
        return 0

    with PricingClient(_gateway()) as client:
        report = measure_all(args.universe_db, client, window_start=args.window_start)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    totals = report["totals"]
    print(
        f"{report['stints']} stints, {report['lines']} lines of {report['companies']} companies: "
        f"{totals['full']} full, {totals['partial']} partial, {totals['zero']} zero"
    )
    for row in report["detail"]:
        if row["status"] != "full":
            print(
                f"  {row['status']:7} {row['symbol']:6} {row['ask_for']:6} "
                f"{row['valid_to']} {row['priced_days']}/{row['expected_days']}"
                f" missing {row['first_missing']}..{row['last_missing']}"
            )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
