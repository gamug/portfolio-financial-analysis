"""Read-only accessor for the point-in-time S&P 500 universe (``universe.db``).

``universe.db`` holds one SCD-2 table, ``universe_membership``, keyed by
``symbol`` (Wikipedia style, e.g. ``BRK.B`` -- matches ``assets.ticker``). It is
single-universe: every row is S&P 500. Membership as of a date ``D`` is

    ``valid_from <= D AND (valid_to IS NULL OR valid_to > D)``

This module never writes. Turning a symbol into an ``assets.id`` is a pure read
(:func:`resolve_asset_ids`); creating a brand-new ``assets`` row stays with the
two agents that own that identity table (``fundamental_agent`` / ``pricing_agent``
``db.sync_universe``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_UNIVERSE = "SP500"
_ID_CHUNK = 900  # keep under SQLite's bound-parameter limit


@dataclass(frozen=True)
class UniverseMember:
    """One membership stint from ``universe.db`` (superset of the old ``Company``)."""

    symbol: str  # -> assets.ticker
    security: str  # -> assets.company_name
    cik: str | None  # -> assets.cik (already zero-padded to 10 upstream)
    gics_sector: str | None  # -> sectors.name
    gics_sub_industry: str | None  # -> assets.sub_industry
    hq_location: str | None
    date_added: str | None
    founded: str | None
    valid_from: str
    valid_to: str | None


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open *path* strictly read-only so the universe writer is untouched."""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _check_universe(universe: str) -> None:
    if universe != SUPPORTED_UNIVERSE:
        raise ValueError(f"universe.db is single-universe ({SUPPORTED_UNIVERSE}); got {universe!r}")


def members_asof(
    universe_conn: sqlite3.Connection,
    analysis_date: str,
    *,
    universe: str = SUPPORTED_UNIVERSE,
) -> list[UniverseMember]:
    """Members with an open stint as of *analysis_date* (ISO ``YYYY-MM-DD``).

    A symbol that left and rejoined has one row per stint; only the stint covering
    *analysis_date* matches. If the data ever carries overlapping stints for a
    symbol, the one with the latest ``valid_from`` wins.
    """
    _check_universe(universe)
    rows = universe_conn.execute(
        """
        SELECT symbol, security, cik, gics_sector, gics_sub_industry,
               hq_location, date_added, founded, valid_from, valid_to
        FROM universe_membership
        WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY symbol, valid_from
        """,
        (analysis_date, analysis_date),
    ).fetchall()
    by_symbol: dict[str, UniverseMember] = {}
    for r in rows:
        by_symbol[str(r["symbol"])] = UniverseMember(
            symbol=str(r["symbol"]),
            security=str(r["security"]),
            cik=r["cik"],
            gics_sector=r["gics_sector"],
            gics_sub_industry=r["gics_sub_industry"],
            hq_location=r["hq_location"],
            date_added=r["date_added"],
            founded=r["founded"],
            valid_from=str(r["valid_from"]),
            valid_to=r["valid_to"],
        )
    return [by_symbol[s] for s in sorted(by_symbol)]


def symbols_asof(
    universe_conn: sqlite3.Connection,
    analysis_date: str,
    *,
    universe: str = SUPPORTED_UNIVERSE,
) -> list[str]:
    """Just the ticker symbols with an open stint as of *analysis_date*, sorted."""
    return [m.symbol for m in members_asof(universe_conn, analysis_date, universe=universe)]


def resolve_asset_ids(
    financial_conn: sqlite3.Connection, symbols: Iterable[str]
) -> tuple[dict[str, int], list[str]]:
    """Map *symbols* to ``assets.id`` by case-insensitive ticker match.

    Pure read -- never inserts. Returns ``(mapping, missing)`` where *missing*
    lists the input symbols with no ``assets`` row yet (they need an agent run to
    create their identity first).
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        key = s.upper()
        if key not in seen:
            seen.add(key)
            wanted.append(key)

    mapping: dict[str, int] = {}
    for start in range(0, len(wanted), _ID_CHUNK):
        chunk = wanted[start : start + _ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = financial_conn.execute(
            f"SELECT id, ticker FROM assets WHERE UPPER(ticker) IN ({placeholders})",  # noqa: S608
            chunk,
        )
        for r in rows:
            mapping[str(r["ticker"]).upper()] = int(r["id"])

    missing = [s for s in wanted if s not in mapping]
    return mapping, missing
