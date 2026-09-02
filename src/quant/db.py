"""SQLite persistence for ``quant``.

Writes into the shared ``KG_FINANCIAL_DB`` and owns the ``quant_*`` tables;
``corporate_action`` / ``quant_return_daily`` / ``risk_free_rate`` /
``benchmark_series`` are additive shared tables created by :func:`kg_schema.ensure`.
Reads of other packages' tables are plain ``SELECT``s -- no ``cycle`` import.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import kg_schema

# quant-owned tables. Additive; grown milestone by milestone. ``kg_schema.ensure``
# creates the shared tables and the read-contract views.
SCHEMA = """
CREATE TABLE IF NOT EXISTS quant_run (
    id             INTEGER PRIMARY KEY,
    command        TEXT NOT NULL,
    as_of          TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,                -- 'running' | 'completed' | 'failed'
    engine_version TEXT NOT NULL,
    params_json    TEXT,
    error          TEXT
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # No WAL: KG_FINANCIAL_DB may sit on a bind mount with unreliable -shm.
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    kg_schema.ensure(conn)


# -- corporate actions --------------------------------------------------------


@dataclass(frozen=True)
class CorporateAction:
    """One dividend or split. ``value`` is cash/share (USD) for a dividend, the
    split ratio for a split (2-for-1 -> 2.0)."""

    asset_id: int
    action_type: str  # 'DIVIDEND' | 'SPLIT'
    ex_date: str
    value: float
    currency: str = "USD"
    declared_date: str | None = None
    record_date: str | None = None
    pay_date: str | None = None
    frequency: str | None = None
    source: str = "pricing-gateway"


@dataclass
class ActionsReport:
    source: str
    engine_version: str
    assets_seen: int = 0
    dividends: int = 0
    splits: int = 0
    inserted: int = 0
    gateway_probe_failed: bool = False
    errors: list[str] = field(default_factory=list)


def upsert_corporate_actions(
    conn: sqlite3.Connection, rows: Iterable[CorporateAction], *, engine_version: str
) -> int:
    """``INSERT OR IGNORE`` on ``(asset_id, action_type, ex_date, engine_version)``.

    Re-running with the same *engine_version* is a no-op; a better source later
    writes parallel rows under a new version.
    """
    now = _now()
    inserted = 0
    for r in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO corporate_action
                (asset_id, action_type, ex_date, value, currency, declared_date,
                 record_date, pay_date, frequency, source, engine_version, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.asset_id,
                r.action_type,
                r.ex_date,
                r.value,
                r.currency,
                r.declared_date,
                r.record_date,
                r.pay_date,
                r.frequency,
                r.source,
                engine_version,
                now,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


# -- reads (plain SQL; no cycle import) --------------------------------------


def load_universe_asset_ids(conn: sqlite3.Connection, *, universe: str, as_of: str) -> list[int]:
    """Open ``universe_membership`` as of *as_of*; fall back to every ``assets`` row
    when the membership table is empty (mirrors ``cycle.data.active_universe``)."""
    rows = conn.execute(
        """
        SELECT DISTINCT asset_id FROM universe_membership
        WHERE universe = ? AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY asset_id
        """,
        (universe, as_of, as_of),
    ).fetchall()
    if rows:
        return [int(r["asset_id"]) for r in rows]
    return [int(r["id"]) for r in conn.execute("SELECT id FROM assets ORDER BY id")]


def load_assets(conn: sqlite3.Connection, *, universe: str, as_of: str) -> list[tuple[int, str]]:
    """``(asset_id, ticker)`` for the gated universe as of *as_of*."""
    ids = set(load_universe_asset_ids(conn, universe=universe, as_of=as_of))
    return [
        (int(r["id"]), str(r["ticker"]))
        for r in conn.execute("SELECT id, ticker FROM assets ORDER BY ticker")
        if int(r["id"]) in ids
    ]


def load_daily_closes(
    conn: sqlite3.Connection, asset_id: int, *, start: str, end: str
) -> list[tuple[str, float]]:
    """``(date, close)`` from ``price_daily`` in ``[start, end]``, ascending."""
    return [
        (str(r["date"]), float(r["close"]))
        for r in conn.execute(
            "SELECT date, close FROM price_daily "
            "WHERE asset_id = ? AND date >= ? AND date <= ? AND close IS NOT NULL "
            "ORDER BY date",
            (asset_id, start, end),
        )
    ]
