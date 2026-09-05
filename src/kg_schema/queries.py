"""Every SQL query in the ``kg_schema`` domain, in one place.

Trace of capabilities -- every query function here, grouped read/write:

Read (``universe.db`` -- point-in-time S&P 500 membership, never written here):
    connect_ro           strictly read-only open of universe.db (re-exports
                          kg_schema.db.connect_ro so callers keep importing it
                          from here)
    members_asof          open membership stints as of a date, as UniverseMember rows
    symbols_asof           just the ticker symbols from members_asof
    resolve_asset_ids      case-insensitive ticker -> assets.id lookup (financial DB,
                            chunked IN (...), never inserts)

Read (data-coverage report -- does the dated universe have core data yet):
    _distinct_ids           one SELECT DISTINCT per required-data table, tolerant of
                             a table that doesn't exist yet in a partial DB
    check_coverage           per-member coverage across assets/fundamental/metrics/
                             pricing/observations/returns, as of a date

Write (data-coverage persistence):
    persist_coverage         upsert one universe_coverage row per member

Write (schema_version -- the monotonic floor other repos assert against):
    ensure                   create the schema_version table if missing
    current_version           highest recorded version, or 0
    record                    mark a version applied (idempotent)

Write (universe_membership -- assets -> append-only membership history):
    reconcile                open memberships for newcomers, close them for the
                              departed (a select-then-insert/update diff)

Pure data (no SQL, returned by the queries above):
    UniverseMember            one universe.db membership stint

Business logic that consumes these queries' results
(``SymbolCoverage``/``CoverageReport``'s roll-up properties) lives in
:mod:`kg_schema.coverage`, not here -- this module is SQL only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from portfolio_common.db import Database, DatabaseError, Row, in_clause

from .coverage import DEFAULT_MIN_OBSERVATION_DAYS, CoverageReport, SymbolCoverage
from .db import connect_ro as _kg_connect_ro

SUPPORTED_UNIVERSE = "SP500"
_ID_CHUNK = 900  # keep under SQLite's bound-parameter limit

VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


# -- universe.db: point-in-time membership (read-only) ----------------------


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


def connect_ro(path: str | Path) -> Database:
    """Open *path* strictly read-only so the universe writer is untouched.

    The shared read-only factory (:func:`kg_schema.db.connect_ro`), re-exposed
    here so callers keep importing ``connect_ro`` from ``kg_schema.queries``
    (previously ``kg_schema.universe_source``)."""
    return _kg_connect_ro(path)


def _check_universe(universe: str) -> None:
    if universe != SUPPORTED_UNIVERSE:
        raise ValueError(f"universe.db is single-universe ({SUPPORTED_UNIVERSE}); got {universe!r}")


def members_asof(
    universe_db: Database,
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
    rows = universe_db.execute(
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
    universe_db: Database,
    analysis_date: str,
    *,
    universe: str = SUPPORTED_UNIVERSE,
) -> list[str]:
    """Just the ticker symbols with an open stint as of *analysis_date*, sorted."""
    return [m.symbol for m in members_asof(universe_db, analysis_date, universe=universe)]


def resolve_asset_ids(
    financial_db: Database, symbols: Iterable[str]
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
        placeholders = in_clause(chunk)
        rows = financial_db.execute(
            f"SELECT id, ticker FROM assets WHERE UPPER(ticker) IN {placeholders}",  # noqa: S608
            chunk,
        )
        for r in rows:
            mapping[str(r["ticker"]).upper()] = int(r["id"])

    missing = [s for s in wanted if s not in mapping]
    return mapping, missing


# -- data-coverage report -----------------------------------------------------


def _distinct_ids(db: Database, sql: str, params: tuple[object, ...]) -> set[int]:
    try:
        return {int(r[0]) for r in db.execute(sql, params)}
    except DatabaseError:  # table not created in this DB yet
        return set()


def check_coverage(
    fin_db: Database,
    universe_db: Database,
    as_of: str,
    *,
    universe: str = "SP500",
    min_observation_days: int = DEFAULT_MIN_OBSERVATION_DAYS,
) -> CoverageReport:
    """Per-member core-data coverage for *universe* as of *as_of*."""
    symbols = symbols_asof(universe_db, as_of, universe=universe)
    id_by_symbol, _missing = resolve_asset_ids(fin_db, symbols)

    fundamental_ids = _distinct_ids(
        fin_db,
        "SELECT DISTINCT asset_id FROM score_snapshot "
        "WHERE score_type = 'FUNDAMENTAL' AND event_time <= ?",
        (as_of,),
    )
    metric_ids = _distinct_ids(
        fin_db,
        "SELECT DISTINCT f.asset_id FROM fundamental_metrics m "
        "JOIN sec_filings f ON f.id = m.filing_id "
        "WHERE f.period_end IS NOT NULL AND f.period_end <= ?",
        (as_of,),
    )
    price_ids = _distinct_ids(
        fin_db, "SELECT DISTINCT asset_id FROM price_daily WHERE date <= ?", (as_of,)
    )
    return_ids = _distinct_ids(
        fin_db, "SELECT DISTINCT asset_id FROM quant_return_daily WHERE obs_date <= ?", (as_of,)
    )
    obs_counts: dict[int, int] = {}
    try:
        for r in fin_db.execute(
            "SELECT asset_id, COUNT(*) FROM price_observation WHERE obs_date <= ? GROUP BY asset_id",
            (as_of,),
        ):
            obs_counts[int(r[0])] = int(r[1])
    except DatabaseError:
        pass

    rows: list[SymbolCoverage] = []
    for symbol in symbols:
        aid = id_by_symbol.get(symbol.upper())
        checks = {
            "assets": aid is not None,
            "fundamental": aid in fundamental_ids,
            "metrics": aid in metric_ids,
            "pricing": aid in price_ids,
            "observations": obs_counts.get(aid or -1, 0) >= min_observation_days,
            "returns": aid in return_ids,
        }
        rows.append(SymbolCoverage(symbol=symbol, asset_id=aid, checks=checks))

    return CoverageReport(
        as_of=as_of,
        universe=universe,
        min_observation_days=min_observation_days,
        rows=rows,
    )


def persist_coverage(fin_db: Database, report: CoverageReport, *, run_id: int | None = None) -> int:
    """Upsert one ``universe_coverage`` row per member (keyed by
    ``(as_of, universe, symbol)``). Returns the row count written."""
    now = _now()
    payload = [
        (
            report.as_of,
            report.universe,
            r.symbol,
            r.asset_id,
            int(r.checks["assets"]),
            int(r.checks["fundamental"]),
            int(r.checks["metrics"]),
            int(r.checks["pricing"]),
            int(r.checks["observations"]),
            int(r.checks["returns"]),
            int(r.covered),
            json.dumps(r.missing),
            now,
            run_id,
        )
        for r in report.rows
    ]
    fin_db.executemany(
        """
        INSERT INTO universe_coverage
            (as_of, universe, symbol, asset_id, in_assets, has_fundamental, has_metrics,
             has_pricing, has_observations, has_returns, covered, missing_json, checked_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of, universe, symbol) DO UPDATE SET
            asset_id = excluded.asset_id, in_assets = excluded.in_assets,
            has_fundamental = excluded.has_fundamental, has_metrics = excluded.has_metrics,
            has_pricing = excluded.has_pricing, has_observations = excluded.has_observations,
            has_returns = excluded.has_returns, covered = excluded.covered,
            missing_json = excluded.missing_json, checked_at = excluded.checked_at,
            run_id = excluded.run_id
        """,
        payload,
    )
    fin_db.commit()
    return len(payload)


# -- schema_version: the monotonic floor other repos assert against ---------


def ensure(db: Database) -> None:
    """Create the ``schema_version`` table if it is missing."""
    db.create_schema(VERSION_DDL)
    db.commit()


def current_version(db: Database) -> int:
    """Highest recorded schema version, or ``0`` when nothing has been applied."""
    ensure(db)
    row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    value = row["v"] if isinstance(row, Row) else (row[0] if row else None)
    return int(value) if value is not None else 0


def record(db: Database, version: int, description: str) -> None:
    """Mark *version* as applied. Idempotent -- a re-recorded version is ignored."""
    db.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
        (version, _now(), description),
    )
    db.commit()


# -- universe_membership: assets -> append-only membership history ----------


def reconcile(  # noqa: PLR0913 - keyword-only provenance fields, all with defaults
    db: Database,
    universe: str,
    present_asset_ids: set[int],
    *,
    as_of: str,
    run_id: int | None = None,
    run_kind: str | None = None,
    source: str,
) -> tuple[int, int]:
    """Open memberships for newcomers, close them for the departed.

    Returns ``(opened, closed)`` counts. *as_of* is the effective date for both
    ``valid_from`` on new rows and ``valid_to`` on closed ones (ISO date string).
    """
    open_rows = db.execute(
        "SELECT asset_id FROM universe_membership WHERE universe = ? AND valid_to IS NULL",
        (universe,),
    ).fetchall()
    open_ids = {int(r[0]) for r in open_rows}

    appeared = present_asset_ids - open_ids
    vanished = open_ids - present_asset_ids
    now = _now()

    for asset_id in sorted(appeared):
        db.execute(
            """
            INSERT OR IGNORE INTO universe_membership
                (asset_id, universe, valid_from, valid_to, detected_at, run_id, run_kind, source)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (asset_id, universe, as_of, now, run_id, run_kind, source),
        )
    if vanished:
        db.executemany(
            """
            UPDATE universe_membership SET valid_to = ?
            WHERE universe = ? AND asset_id = ? AND valid_to IS NULL
            """,
            [(as_of, universe, asset_id) for asset_id in sorted(vanished)],
        )
    db.commit()
    return len(appeared), len(vanished)


__all__ = [
    "SUPPORTED_UNIVERSE",
    "UniverseMember",
    "check_coverage",
    "connect_ro",
    "current_version",
    "ensure",
    "members_asof",
    "persist_coverage",
    "reconcile",
    "record",
    "resolve_asset_ids",
    "symbols_asof",
]
