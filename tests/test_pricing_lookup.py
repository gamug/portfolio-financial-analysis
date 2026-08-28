"""Resolving a period-end close from the shared ``price_daily`` table."""

from __future__ import annotations

import sqlite3

import pytest

from fundamental_agent.pricing import ClosePrice, close_on_or_before


@pytest.fixture
def priced_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE price_daily (
            id INTEGER PRIMARY KEY, asset_id INTEGER, date TEXT, close REAL,
            UNIQUE (asset_id, date)
        )
        """
    )
    conn.executemany(
        "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, ?)",
        [
            (1, "2023-09-27", 170.43),
            (1, "2023-09-28", 170.69),
            (1, "2023-09-29", 171.21),  # Friday; 2023-09-30 is a Saturday
            (1, "2023-10-02", 173.75),
            (2, "2023-09-29", 99.0),
        ],
    )
    conn.commit()
    return conn


def test_exact_date_hit(priced_db: sqlite3.Connection) -> None:
    assert close_on_or_before(priced_db, 1, "2023-09-29") == ClosePrice("2023-09-29", 171.21)


def test_falls_back_to_the_last_close_before_a_weekend_period_end(
    priced_db: sqlite3.Connection,
) -> None:
    got = close_on_or_before(priced_db, 1, "2023-09-30")
    assert got == ClosePrice("2023-09-29", 171.21)


def test_never_looks_forward(priced_db: sqlite3.Connection) -> None:
    # 2023-10-02 exists but is after the period-end -- must not be picked.
    got = close_on_or_before(priced_db, 1, "2023-09-30")
    assert got is not None
    assert got.date == "2023-09-29"


def test_returns_none_beyond_the_lookback_window(priced_db: sqlite3.Connection) -> None:
    assert close_on_or_before(priced_db, 1, "2023-10-20", max_lookback_days=7) is None


def test_returns_none_for_an_asset_with_no_rows(priced_db: sqlite3.Connection) -> None:
    assert close_on_or_before(priced_db, 999, "2023-09-29") is None


def test_missing_price_table_is_tolerated() -> None:
    bare = sqlite3.connect(":memory:")
    assert close_on_or_before(bare, 1, "2023-09-29") is None
