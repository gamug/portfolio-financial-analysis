"""The D8 leaver price-coverage script (T-153 part 3a): pure functions, no live gateway."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

from conftest import write_universe_db

from kg_schema.queries import connect_ro
from pricing_agent.pricing_client import Candle, DailyPrices

_SPEC = importlib.util.spec_from_file_location(
    "leaver_price_coverage",
    Path(__file__).resolve().parent.parent / "scripts" / "leaver_price_coverage.py",
)
assert _SPEC and _SPEC.loader
cov = importlib.util.module_from_spec(_SPEC)
sys.modules["leaver_price_coverage"] = cov
_SPEC.loader.exec_module(cov)


class FakeGateway:
    def __init__(self, dates: list[str]) -> None:
        self.dates = dates
        self.calls: list[tuple[str, str, str]] = []

    def daily_any_spelling(self, ticker: str, start: str, end: str) -> DailyPrices:
        self.calls.append((ticker, start, end))
        candles = [Candle(d, 1, 1, 1, 1, 1, "x") for d in self.dates if start <= d <= end]
        return DailyPrices(ticker, start, end, "yfinance", candles, None)


def test_expected_days_are_the_nyse_sessions() -> None:
    """The calendar, not a vendor's: MLK Day 2022-01-17 is not a session."""
    days = cov.expected_days("2022-01-14", "2022-01-19")
    assert days == ["2022-01-14", "2022-01-18", "2022-01-19"]


def test_coverage_and_status() -> None:
    days = ["2022-01-03", "2022-01-04", "2022-01-05"]
    assert cov.coverage(days, {"2022-01-03", "2022-01-05"}) == (2, "2022-01-04", "2022-01-04")
    assert cov.coverage(days, set()) == (0, "2022-01-03", "2022-01-05")
    assert cov.coverage(days, set(days)) == (3, None, None)
    assert cov.status(3, 3) == "full"
    assert cov.status(2, 3) == "partial"
    assert cov.status(0, 3) == "zero"
    assert cov.status(0, 0) == "empty"


def test_measure_uses_one_request_and_the_canonical_ticker() -> None:
    gateway = FakeGateway(["2022-01-03", "2022-01-04", "2022-01-05"])
    assert cov.measure(gateway, "GAP", "2022-01-03", "2022-01-04") == [
        "2022-01-03",
        "2022-01-04",
    ]
    assert gateway.calls == [("GAP", "2022-01-03", "2022-01-04")]


def test_canonical_by_key_maps_the_post_exit_renames() -> None:
    """D4: GPS is measured under GAP and FBHS under FBIN."""
    canonical = cov.canonical_by_key()
    assert canonical[("0000039911", "GPS")] == "GAP"
    assert canonical[("0001519751", "FBHS")] == "FBIN"
    assert canonical[("0001437107", "DISCA")] == "WBD"


def test_leaver_stints_excludes_current_and_pre_window_members(tmp_path: Path) -> None:
    udb = write_universe_db(
        tmp_path / "u.db",
        [
            ("AAA", "2020-01-01", "2022-06-15"),  # a leaver in the window
            ("BBB", "2020-01-01", None),  # a current member
            ("CCC", "2020-01-01", "2021-12-31"),  # left before the window
        ],
    )
    with contextlib.closing(connect_ro(udb)) as universe:
        stints = cov.leaver_stints(universe)
    assert [(s.symbol, s.valid_to) for s in stints] == [("AAA", "2022-06-15")]
