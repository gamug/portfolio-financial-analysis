"""Shared fixtures. The ``financials_*.json`` files are real EDGAR gateway captures."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from fundamental_agent import db
from fundamental_agent.statements import Statements
from pricing_agent import db as pricing_db
from quant import db as quant_db

FIXTURES = Path(__file__).parent / "fixtures"

# Tables the fundamental agent owns that quant reads but pricing_agent doesn't create.
_QUANT_EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS sec_filings (
    id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id),
    form TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_period TEXT NOT NULL,
    filing_date TEXT, accession_number TEXT, period_end TEXT, retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_facts (
    id INTEGER PRIMARY KEY, filing_id INTEGER NOT NULL REFERENCES sec_filings(id),
    statement TEXT NOT NULL, concept TEXT NOT NULL, standard_concept TEXT, label TEXT,
    period_key TEXT NOT NULL, value REAL, filing_version TEXT NOT NULL DEFAULT 'pre-v1',
    event_time TEXT NOT NULL DEFAULT '', ingested_at TEXT,
    UNIQUE (filing_id, statement, concept, period_key, filing_version)
);
"""


def _load(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / name).read_text())
    return cast("dict[str, Any]", payload["data"])


@pytest.fixture
def aapl_10k() -> Statements:
    return Statements.from_payload(_load("financials_AAPL_10-K_2023.json"))


@pytest.fixture
def jpm_10k() -> Statements:
    return Statements.from_payload(_load("financials_JPM_10-K_2023.json"))


@pytest.fixture
def msft_10q() -> Statements:
    return Statements.from_payload(_load("financials_MSFT_10-Q_2024.json"))


@pytest.fixture
def nvda_10k() -> Statements:
    return Statements.from_payload(_load("financials_NVDA_10-K_2024.json"))


@pytest.fixture
def raw_aapl_payload() -> dict[str, Any]:
    return _load("financials_AAPL_10-K_2023.json")


@pytest.fixture
def constituents_html() -> str:
    return (FIXTURES / "sp500_constituents.html").read_text()


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


@pytest.fixture
def memory_pricing_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pricing_db.ensure_schema(conn)
    return conn


@pytest.fixture
def memory_quant_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pricing_db.ensure_schema(conn)  # assets, sectors, price_daily, price_window + kg_schema
    conn.executescript(_QUANT_EXTRA_DDL)
    quant_db.ensure_schema(conn)  # quant_run + (re-)runs kg_schema.ensure
    return conn


@pytest.fixture
def quant_seed() -> Callable[..., sqlite3.Connection]:
    """Return a seeder: fixed-seed geometric random walk into price_daily /
    price_observation for *n_assets* names across 2 sectors, open SP500 membership,
    and (optionally) a couple of quarterly dividends per asset."""

    def _seed(  # noqa: PLR0913 - fixture knobs, all keyword-only with defaults
        conn: sqlite3.Connection,
        *,
        n_assets: int = 6,
        n_days: int = 300,
        start: str = "2024-01-01",
        with_dividends: bool = True,
        seed: int = 7,
    ) -> sqlite3.Connection:
        rng = random.Random(seed)
        conn.execute("INSERT OR IGNORE INTO sectors (id, name) VALUES (1, 'S1'), (2, 'S2')")
        d0 = date.fromisoformat(start)
        # trading days = weekdays
        days: list[str] = []
        d = d0
        while len(days) < n_days:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += timedelta(days=1)

        for a in range(1, n_assets + 1):
            conn.execute(
                "INSERT INTO assets (id, ticker, company_name, sector_id, sub_industry) "
                "VALUES (?, ?, ?, ?, ?)",
                (a, f"AS{a:02d}", f"Asset {a}", 1 + a % 2, f"SI{a % 3}"),
            )
            conn.execute(
                "INSERT INTO universe_membership "
                "(asset_id, universe, valid_from, detected_at, source) "
                "VALUES (?, 'SP500', ?, ?, 'test')",
                (a, days[0], days[0] + "T00:00:00Z"),
            )
            price = 50.0 + 10.0 * a
            drift = 0.0003 * (a - n_assets / 2)
            vol = 0.010 + 0.004 * (a % 3)
            prev: float | None = None
            for obs_date in days:
                shock = rng.gauss(drift, vol)
                price = max(1.0, price * (1.0 + shock))
                logret = None if prev is None else math.log(price / prev)
                conn.execute(
                    "INSERT INTO price_daily (asset_id, date, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (a, obs_date, price, price * 1.01, price * 0.99, price, 1_000_000),
                )
                conn.execute(
                    "INSERT INTO price_observation "
                    "(asset_id, obs_date, close, prev_close, log_return, dollar_volume, "
                    " event_time, computed_at, engine_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'priceobs-v1')",
                    (
                        a,
                        obs_date,
                        price,
                        prev,
                        logret,
                        price * 1_000_000,
                        obs_date,
                        obs_date + "T00:00:00Z",
                    ),
                )
                prev = price

            if with_dividends:
                fid = conn.execute(
                    "INSERT INTO sec_filings "
                    "(asset_id, form, fiscal_year, fiscal_period, period_end, retrieved_at) "
                    "VALUES (?, '10-K', 2024, 'FY', ?, ?) RETURNING id",
                    (a, days[-1], days[-1] + "T00:00:00Z"),
                ).fetchone()["id"]
                conn.execute(
                    "INSERT INTO financial_facts "
                    "(filing_id, statement, concept, period_key, value, event_time) "
                    "VALUES (?, 'income_statement', "
                    "'us-gaap_CommonStockDividendsPerShareDeclared', ?, ?, ?)",
                    (fid, f"{days[-1]} (FY)", 1.20 + 0.1 * a, days[-1]),
                )
        conn.commit()
        return conn

    return _seed
