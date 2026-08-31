"""SQLite persistence for the pricing collector.

Writes into the same ``KG_FINANCIAL_DB`` as the fundamental agent but owns a disjoint
set of tables. ``assets`` / ``sectors`` are created only when missing and never
altered, so either module can populate the universe first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import kg_schema
from kg_schema import universe_membership as kg_universe_membership
from pricing_agent.observations import Observation
from pricing_agent.pricing_client import Candle
from pricing_agent.stats import WindowStats
from pricing_agent.universe import Company

PRICE_OBSERVATION_ENGINE_VERSION = "priceobs-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sectors (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY,
    ticker       TEXT NOT NULL UNIQUE,
    company_name TEXT,
    cik          TEXT,
    sector_id    INTEGER REFERENCES sectors(id),
    sub_industry TEXT
);

CREATE TABLE IF NOT EXISTS price_window (
    id                    INTEGER PRIMARY KEY,
    asset_id              INTEGER NOT NULL REFERENCES assets(id),
    start_date            TEXT NOT NULL,
    end_date              TEXT NOT NULL,
    label                 TEXT NOT NULL,          -- 'full' or a calendar year
    first_trading_date    TEXT,
    last_trading_date     TEXT,
    first_close           REAL,
    last_close            REAL,
    period_return         REAL,
    trading_days          INTEGER,
    daily_return_std      REAL,
    annualized_volatility REAL,
    min_close             REAL,
    max_close             REAL,
    avg_volume            REAL,
    source                TEXT,
    warning               TEXT,
    retrieved_at          TEXT NOT NULL,
    UNIQUE (asset_id, start_date, end_date, label)
);

CREATE TABLE IF NOT EXISTS price_daily (
    id       INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    date     TEXT NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    source   TEXT,
    UNIQUE (asset_id, date)
);

CREATE TABLE IF NOT EXISTS pricing_run (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    universe_size   INTEGER,
    planned_units   INTEGER,
    completed_units INTEGER DEFAULT 0,
    skipped_units   INTEGER DEFAULT 0,
    failed_units    INTEGER DEFAULT 0,
    params_json     TEXT,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_run_error (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES pricing_run(id) ON DELETE CASCADE,
    ticker     TEXT,
    label      TEXT,
    stage      TEXT,
    message    TEXT,
    created_at TEXT NOT NULL
);
"""

_REQUIRED_ASSET_COLUMNS = {"id", "ticker", "company_name", "cik", "sector_id", "sub_industry"}


@dataclass(frozen=True)
class PriceWindowRow:
    """A fully-computed price window ready to persist."""

    asset_id: int
    start_date: str
    end_date: str
    label: str
    stats: WindowStats
    source: str
    warning: str | None


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # NOTE: no WAL. KG_FINANCIAL_DB can live on a bind mount whose shared-memory
    # (-shm) support is unreliable, where WAL raises "disk I/O error"; the default
    # rollback journal works there. This collector is single-writer anyway.
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
    missing = _REQUIRED_ASSET_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "existing 'assets' table is missing columns required by this collector: "
            f"{', '.join(sorted(missing))}"
        )
    # Shared cross-repo schema (price_observation, universe_membership, views, ...).
    kg_schema.ensure(conn)


# -- universe -------------------------------------------------------------


def sync_universe(
    conn: sqlite3.Connection, companies: Iterable[Company], *, as_of: str | None = None
) -> int:
    seen_ids: set[int] = set()
    count = 0
    for company in companies:
        sector_id = _upsert_sector(conn, company.sector)
        conn.execute(
            """
            INSERT INTO assets (ticker, company_name, cik, sector_id, sub_industry)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = COALESCE(NULLIF(excluded.company_name, ''), assets.company_name),
                cik          = COALESCE(NULLIF(excluded.cik, ''), assets.cik),
                sector_id    = COALESCE(excluded.sector_id, assets.sector_id),
                sub_industry = COALESCE(NULLIF(excluded.sub_industry, ''), assets.sub_industry)
            """,
            (
                company.symbol,
                company.name,
                company.cik,
                sector_id,
                company.sub_industry,
            ),
        )
        row = conn.execute("SELECT id FROM assets WHERE ticker = ?", (company.symbol,)).fetchone()
        if row is not None:
            seen_ids.add(int(row["id"]))
        count += 1
    conn.commit()
    kg_universe_membership.reconcile(
        conn,
        "SP500",
        seen_ids,
        as_of=as_of or _now()[:10],
        run_kind="pricing",
        source="pricing-gateway",
    )
    return count


def _upsert_sector(conn: sqlite3.Connection, name: str) -> int | None:
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO sectors (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM sectors WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


def load_universe(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    query = "SELECT id, ticker, company_name FROM assets"
    params: list[Any] = []
    if tickers:
        placeholders = ", ".join("?" * len(tickers))
        query += f" WHERE ticker IN ({placeholders})"
        params.extend(t.upper() for t in tickers)
    query += " ORDER BY ticker"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(query, params))


# -- price windows & daily bars -----------------------------------------


def completed_windows(conn: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    """``(ticker, start_date, end_date, label)`` windows already stored."""
    rows = conn.execute(
        """
        SELECT a.ticker AS ticker, w.start_date AS s, w.end_date AS e, w.label AS l
        FROM price_window w JOIN assets a ON a.id = w.asset_id
        """
    )
    return {(r["ticker"], r["s"], r["e"], r["l"]) for r in rows}


def upsert_price_window(conn: sqlite3.Connection, row: PriceWindowRow) -> None:
    s = row.stats
    conn.execute(
        """
        INSERT INTO price_window
            (asset_id, start_date, end_date, label, first_trading_date, last_trading_date,
             first_close, last_close, period_return, trading_days, daily_return_std,
             annualized_volatility, min_close, max_close, avg_volume, source, warning,
             retrieved_at, event_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, start_date, end_date, label) DO UPDATE SET
            first_trading_date    = excluded.first_trading_date,
            last_trading_date     = excluded.last_trading_date,
            first_close           = excluded.first_close,
            last_close            = excluded.last_close,
            period_return         = excluded.period_return,
            trading_days          = excluded.trading_days,
            daily_return_std      = excluded.daily_return_std,
            annualized_volatility = excluded.annualized_volatility,
            min_close             = excluded.min_close,
            max_close             = excluded.max_close,
            avg_volume            = excluded.avg_volume,
            source                = excluded.source,
            warning               = excluded.warning,
            retrieved_at          = excluded.retrieved_at,
            event_time            = excluded.event_time
        """,
        (
            row.asset_id,
            row.start_date,
            row.end_date,
            row.label,
            s.first_date,
            s.last_date,
            s.first_close,
            s.last_close,
            s.period_return,
            s.trading_days,
            s.daily_return_std,
            s.annualized_volatility,
            s.min_close,
            s.max_close,
            s.avg_volume,
            row.source,
            row.warning,
            _now(),
            row.end_date,
        ),
    )
    conn.commit()


def upsert_price_observations(
    conn: sqlite3.Connection,
    asset_id: int,
    observations: Iterable[Observation],
    *,
    engine_version: str = PRICE_OBSERVATION_ENGINE_VERSION,
    run_id: int | None = None,
) -> int:
    """Write the derived per-day price analytics. Immutable per
    ``(asset_id, obs_date, engine_version)`` -- a re-run with the same version is a no-op."""
    now = _now()
    rows = [
        (
            asset_id,
            o.obs_date,
            o.close,
            o.prev_close,
            o.log_return,
            o.true_range,
            o.atr_14,
            o.realized_vol_21d,
            o.realized_vol_90d,
            o.max_drawdown_90d,
            o.momentum_21d,
            o.momentum_63d,
            o.momentum_252d,
            o.dollar_volume,
            o.obs_date,
            now,
            engine_version,
            run_id,
            "pricing",
        )
        for o in observations
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO price_observation
            (asset_id, obs_date, close, prev_close, log_return, true_range, atr_14,
             realized_vol_21d, realized_vol_90d, max_drawdown_90d, momentum_21d, momentum_63d,
             momentum_252d, dollar_volume, event_time, computed_at, engine_version, run_id, run_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def replace_daily_prices(conn: sqlite3.Connection, asset_id: int, candles: Iterable[Candle]) -> int:
    now = _now()
    rows = [
        (asset_id, c.date, c.open, c.high, c.low, c.close, c.volume, c.source, c.date, now)
        for c in candles
    ]
    conn.executemany(
        """
        INSERT INTO price_daily
            (asset_id, date, open, high, low, close, volume, source, event_time, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, date) DO UPDATE SET
            open = excluded.open, high = excluded.high, low = excluded.low,
            close = excluded.close, volume = excluded.volume, source = excluded.source,
            event_time = excluded.event_time, ingested_at = excluded.ingested_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


# -- run log ------------------------------------------------------------


@dataclass(frozen=True)
class RunError:
    ticker: str
    label: str | None
    stage: str
    message: str


_MAX_ERROR_CHARS = 2000


def start_run(conn: sqlite3.Connection, *, params_json: str) -> int:
    cur = conn.execute(
        "INSERT INTO pricing_run (started_at, params_json, status) VALUES (?, ?, 'running')",
        (_now(), params_json),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_run_plan(
    conn: sqlite3.Connection, run_id: int, *, universe_size: int, planned_units: int
) -> None:
    conn.execute(
        "UPDATE pricing_run SET universe_size = ?, planned_units = ? WHERE id = ?",
        (universe_size, planned_units, run_id),
    )
    conn.commit()


def bump_run_counter(conn: sqlite3.Connection, run_id: int, column: str) -> None:
    if column not in {"completed_units", "skipped_units", "failed_units"}:
        raise ValueError(f"not a counter column: {column}")
    conn.execute(
        f"UPDATE pricing_run SET {column} = {column} + 1 WHERE id = ?",  # noqa: S608
        (run_id,),
    )
    conn.commit()


def record_error(conn: sqlite3.Connection, run_id: int, error: RunError) -> None:
    conn.execute(
        """
        INSERT INTO pricing_run_error (run_id, ticker, label, stage, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            error.ticker,
            error.label,
            error.stage,
            error.message[:_MAX_ERROR_CHARS],
            _now(),
        ),
    )
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str) -> None:
    conn.execute(
        "UPDATE pricing_run SET finished_at = ?, status = ? WHERE id = ?",
        (_now(), status, run_id),
    )
    conn.commit()
