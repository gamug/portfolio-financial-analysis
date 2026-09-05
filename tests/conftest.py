"""Shared fixtures. The ``financials_*.json`` files are real EDGAR gateway captures."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.statements import Statements
from pricing_agent import db as pricing_db
from quant import db as quant_db

FIXTURES = Path(__file__).parent / "fixtures"

_UNIVERSE_DDL = """
CREATE TABLE universe_membership (
    symbol TEXT NOT NULL, security TEXT NOT NULL,
    gics_sector TEXT, gics_sub_industry TEXT, hq_location TEXT,
    date_added TEXT, cik TEXT, founded TEXT,
    valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL
);
CREATE INDEX idx_membership_symbol ON universe_membership (symbol);
CREATE INDEX idx_membership_valid ON universe_membership (valid_from, valid_to);
"""


def write_universe_db(
    path: Path, members: Iterable[tuple[str, str, str | None]], *, source: str = "test"
) -> Path:
    """Build a minimal ``universe.db`` at *path*. Each member is
    ``(symbol, valid_from, valid_to)``; extra columns are left NULL/derived."""
    conn = sqlite3.connect(path)
    conn.executescript(_UNIVERSE_DDL)
    conn.executemany(
        "INSERT INTO universe_membership "
        "(symbol, security, gics_sector, gics_sub_industry, valid_from, valid_to, source) "
        "VALUES (?, ?, 'S1', 'SI0', ?, ?, ?)",
        [(sym, f"{sym} Inc.", vf, vt, source) for sym, vf, vt in members],
    )
    conn.commit()
    conn.close()
    return path


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
def universe_db(tmp_path: Path) -> Callable[..., Path]:
    """Factory: write a temp ``universe.db`` and return its path.

    ``universe_db([("AAPL", "2020-01-01", None), ...])`` -- ``(symbol, valid_from,
    valid_to)`` per row."""

    def _make(members: Iterable[tuple[str, str, str | None]], name: str = "universe.db") -> Path:
        return write_universe_db(tmp_path / name, members)

    return _make


def _memory_database() -> Database:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture
def memory_db() -> Database:
    conn = _memory_database()
    db.ensure_schema(conn)
    return conn


@pytest.fixture
def memory_pricing_db() -> Database:
    conn = _memory_database()
    pricing_db.ensure_schema(conn)
    return conn


@pytest.fixture
def memory_quant_db() -> Database:
    conn = _memory_database()
    pricing_db.ensure_schema(conn)  # assets, sectors, price_daily, price_window + kg_schema
    conn.executescript(_QUANT_EXTRA_DDL)
    quant_db.ensure_schema(conn)  # quant_run + (re-)runs kg_schema.ensure
    return conn


@pytest.fixture
def quant_seed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Database]:
    """Return a seeder: fixed-seed geometric random walk into price_daily /
    price_observation for *n_assets* names across 2 sectors, open SP500 membership,
    and (optionally) a couple of quarterly dividends per asset.

    Also writes a companion ``universe.db`` (open stint per seeded name) and points
    ``KG_UNIVERSE_DB`` at it, so the point-in-time universe reads resolve locally."""

    def _seed(  # noqa: PLR0913 - fixture knobs, all keyword-only with defaults
        conn: Database,
        *,
        n_assets: int = 6,
        n_days: int = 300,
        start: str = "2024-01-01",
        with_dividends: bool = True,
        seed: int = 7,
    ) -> Database:
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

        udb = tmp_path / "quant_seed_universe.db"
        if not udb.exists():
            write_universe_db(udb, [(f"AS{a:02d}", days[0], None) for a in range(1, n_assets + 1)])
        monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))
        return conn

    return _seed
