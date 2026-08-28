"""Wikipedia constituents parsing."""

from __future__ import annotations

import pytest

from fundamental_agent.universe import parse_constituents


def test_parses_rows_with_cik_and_sector(constituents_html: str) -> None:
    companies = parse_constituents(constituents_html)
    assert len(companies) >= 10
    mmm = next(c for c in companies if c.symbol == "MMM")
    assert mmm.name == "3M"
    assert mmm.sector == "Industrials"
    assert mmm.cik == "0000066740"  # zero-padded to 10 digits


def test_keeps_share_class_dot_in_symbol(constituents_html: str) -> None:
    symbols = {c.symbol for c in parse_constituents(constituents_html)}
    assert "BRK.B" in symbols


def test_missing_table_raises() -> None:
    with pytest.raises(ValueError, match="constituents"):
        parse_constituents("<html><body><p>no table here</p></body></html>")
