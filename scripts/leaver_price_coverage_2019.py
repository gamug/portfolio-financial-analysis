"""D8 (T-153 part 3d): what the gateway serves TODAY for every leaver line over T-100's range.

    uv run python scripts/leaver_price_coverage_2019.py \
        --universe-db /workspaces/thesis/data/universe_history_gics.db \
        --production-db /workspaces/thesis/data/financial.db \
        --archive-db /workspaces/thesis/data/archive/t153c_r4_gateway_2026-10-14/financial.db \
        --out docs/leaver_price_coverage_2019.json

Read-only: gateway GETs, and the two databases opened ``mode=ro``. For every line of
``universe_history_gics.db`` that left the S&P 500 inside the window, from 2019-01-02 to the
line's last membership day, it records:

* today's candles -- count, first and last date (one gateway call per line);
* production's stored count and span (clipped to the line's last membership day);
* the archive's count and span (the same clip);
* and an AAPL control over a fixed recent range.

The output is the baseline T-100 compares against (its own entry in ``TASKS.md``): a line less
covered than this commit stops T-100 and goes to reviewer A, whose first remedy is the archive.
Counts, dates and hashes only -- never a price.

Reviewer B's run is ``/workspaces/thesis/handoff/lpc2019.py``; it lacks the archive columns and
resolves a line's stored rows by bare ticker (so DISCK reports Discovery/WBD's asset), which this
script fixes by resolving through the tracked ticker history's canonical symbol.
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

from kg_schema.queries import WINDOW_START, connect_ro
from kg_schema.ticker_history import read_history
from kg_schema.trading_calendar import is_trading_day
from pricing_agent.config import DEFAULT_PRICING_BASE_URL
from pricing_agent.pricing_client import PricingClient

DEFAULT_START = "2019-01-02"
CONTROL = {"ticker": "AAPL", "start": "2026-09-01", "end": "2026-10-13"}


@dataclass(frozen=True)
class Coverage:
    """One line's coverage from one source (counts and dates only)."""

    days: int
    first: str | None
    last: str | None

    @classmethod
    def of(cls, dates: set[str]) -> Coverage:
        return cls(
            days=len(dates),
            first=min(dates) if dates else None,
            last=max(dates) if dates else None,
        )


def _sessions(start: str, end: str) -> list[str]:
    """Every NYSE session in ``[start, end]``, from the shared calendar."""
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    out: list[str] = []
    while day <= last:
        if is_trading_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha1(path: str) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()  # noqa: S324 - a change check


def _canonical() -> dict[tuple[str, str], str]:
    """``(CIK, symbol) -> the line's canonical symbol`` from the tracked ticker history."""
    out: dict[tuple[str, str], str] = {}
    for row in read_history():
        out.setdefault((row.cik, row.symbol.upper()), row.canonical_symbol.upper())
    return out


@dataclass
class Stint:
    """One membership stint of a company that left the index in the window."""

    symbol: str
    security: str
    cik: str
    valid_to: str


def _leaver_stints(universe: Any) -> list[Stint]:
    rows = universe.execute(
        "SELECT symbol, security, cik, valid_to FROM universe_membership "
        "WHERE cik IS NOT NULL AND valid_to IS NOT NULL AND valid_to >= ? "
        "ORDER BY valid_to, symbol",
        (WINDOW_START,),
    ).fetchall()
    open_ciks = {
        str(r["cik"])
        for r in universe.execute(
            "SELECT DISTINCT cik FROM universe_membership "
            "WHERE cik IS NOT NULL AND valid_to IS NULL"
        )
    }
    return [
        Stint(str(r["symbol"]), str(r["security"]), str(r["cik"]), str(r["valid_to"]))
        for r in rows
        if str(r["cik"]) not in open_ciks
    ]


def _lines(stints: list[Stint]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(CIK, canonical ask) -> line`` -- one entry per CIK line, the longest stint's exit."""
    canonical = _canonical()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for stint in stints:
        ask = canonical.get((stint.cik, stint.symbol.upper()), stint.symbol.upper())
        line = out.setdefault(
            (stint.cik, ask),
            {
                "cik": stint.cik,
                "ask_for": ask,
                "company": stint.security,
                "symbols": set(),
                "valid_to": stint.valid_to,
            },
        )
        line["symbols"].add(stint.symbol.upper())
        line["valid_to"] = max(line["valid_to"], stint.valid_to)
    return out


def _stored_days(db_path: str, cik: str, ask: str, end: str) -> set[str]:
    """The line's stored ``price_daily`` days up to *end*, resolved through its own ticker line.

    A bare-ticker lookup reports Discovery/WBD's rows for DISCK; the CIK plus the line's canonical
    symbol keeps the two lines apart (T-153 part 2).
    """
    with contextlib.closing(connect_ro(db_path)) as conn:
        rows = conn.execute("SELECT id, ticker FROM assets WHERE cik = ?", (cik,)).fetchall()
        matching = [r for r in rows if str(r["ticker"]).upper() == ask.upper()]
        ids = [int(r["id"]) for r in (matching or rows)] if matching else []
        days: set[str] = set()
        for asset_id in ids:
            days |= {
                str(r["date"])
                for r in conn.execute(
                    "SELECT date FROM price_daily WHERE asset_id = ? AND date <= ?",
                    (asset_id, end),
                )
            }
    return days


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-db", required=True)
    parser.add_argument("--production-db", required=True)
    parser.add_argument("--archive-db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", default=DEFAULT_START)
    args = parser.parse_args()

    with contextlib.closing(connect_ro(args.universe_db)) as universe:
        lines = _lines(_leaver_stints(universe))

    control_days = set(_sessions(CONTROL["start"], CONTROL["end"]))
    detail: list[dict[str, Any]] = []
    with PricingClient(DEFAULT_PRICING_BASE_URL) as client:
        control_served = {
            c.date
            for c in client.daily_any_spelling(
                CONTROL["ticker"], CONTROL["start"], CONTROL["end"]
            ).candles
        }
        for (cik, ask), line in sorted(lines.items(), key=lambda kv: (kv[1]["valid_to"], kv[0][1])):
            end = line["valid_to"]
            expected = _sessions(args.start, end)
            try:
                served = {c.date for c in client.daily_any_spelling(ask, args.start, end).candles}
            except Exception as exc:  # a dead line is a zero, not a crash
                served = set()
                print(f"  {ask}: the gateway refused ({exc})")
            served &= set(expected)
            prod = _stored_days(args.production_db, cik, ask, end)
            archive = _stored_days(args.archive_db, cik, ask, end)
            status = "full" if served == set(expected) else ("zero" if not served else "partial")
            detail.append(
                {
                    "cik": cik,
                    "ask_for": ask,
                    "company": line["company"],
                    "symbols": sorted(line["symbols"]),
                    "range": f"{args.start}..{end}",
                    "expected_days": len(expected),
                    "gateway": asdict(Coverage.of(served)),
                    "production": asdict(Coverage.of(prod)),
                    "archive": asdict(Coverage.of(archive)),
                    "status": status,
                }
            )

    doc = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gateway": DEFAULT_PRICING_BASE_URL,
        "range_start": args.start,
        "calendar": "kg_schema.trading_calendar (NYSE sessions)",
        "universe_db": args.universe_db,
        "universe_sha1": _sha1(args.universe_db),
        "production_db": args.production_db,
        "production_sha1": _sha1(args.production_db),
        "archive_db": args.archive_db,
        "archive_sha256": _sha256(args.archive_db),
        "lines": len(detail),
        "companies": len({row["cik"] for row in detail}),
        "totals": {
            k: sum(row["status"] == k for row in detail) for k in ("full", "partial", "zero")
        },
        "control": {
            "ticker": CONTROL["ticker"],
            "range": f"{CONTROL['start']}..{CONTROL['end']}",
            "expected_days": len(control_days),
            "priced_days": len(control_days & control_served),
        },
        "detail": detail,
    }
    Path(args.out).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(
        json.dumps(
            {k: doc[k] for k in ("measured_at", "lines", "companies", "totals", "control")},
            indent=1,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
