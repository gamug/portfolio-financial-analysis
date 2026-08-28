"""Parsing the pricing gateway's /universe rows."""

from __future__ import annotations

import pytest

from pricing_agent.universe import parse_universe

_ROWS = [
    {
        "Symbol": "AAPL",
        "Security": "Apple Inc.",
        "GICS Sector": "Information Technology",
        "GICS Sub-Industry": "Technology Hardware, Storage & Peripherals",
        "CIK": "320193",
    },
    {
        "Symbol": "BRK.B",
        "Security": "Berkshire Hathaway",
        "GICS Sector": "Financials",
        "GICS Sub-Industry": "Multi-Sector Holdings",
        "CIK": "0001067983",
    },
    {"Symbol": "", "Security": "junk row"},  # dropped
]


def test_parse_universe_renames_and_pads_cik() -> None:
    companies = parse_universe(_ROWS)
    assert [c.symbol for c in companies] == ["AAPL", "BRK.B"]
    aapl = companies[0]
    assert aapl.sector == "Information Technology"
    assert aapl.cik == "0000320193"  # zero-padded to 10 digits


def test_parse_universe_empty_raises() -> None:
    with pytest.raises(ValueError, match="no usable rows"):
        parse_universe([{"Symbol": ""}])
