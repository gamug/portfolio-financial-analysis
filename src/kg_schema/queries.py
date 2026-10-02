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

Read (metric versions -- which engine versions of fundamental_metrics are stored):
    metric_versions_present  {metric_group: [engine_version, ...]} actually stored; the
                              input to kg_schema.versions' resolver (T-090)

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


# -- market capitalisation inputs (T-132) ------------------------------------


def cover_share_rows(db: Database, asset_ids: list[int], as_of: str) -> list[Row]:
    """The cover-page share counts usable on *as_of*: their filing's ``available_at`` and the
    count's own ``as_of_date`` are both on or before it (T-107, no lookahead). Empty when the
    table does not exist yet (a database no ingest has touched)."""
    try:
        return db.execute(
            """
            SELECT sf.asset_id, a.cik, sf.id AS filing_id, sf.available_at,
                   c.class_member, c.value, c.as_of_date
            FROM filing_cover_shares c JOIN sec_filings sf ON sf.id = c.filing_id
            JOIN assets a ON a.id = sf.asset_id
            WHERE sf.asset_id IN (SELECT value FROM json_each(?))
              AND sf.available_at IS NOT NULL AND sf.available_at <= ? AND c.as_of_date <= ?
            ORDER BY sf.asset_id, sf.id, c.class_member, c.as_of_date
            """,
            (json.dumps(sorted(set(asset_ids))), as_of, as_of),
        ).fetchall()
    except DatabaseError:
        return []


def last_closes(db: Database, asset_ids: list[int], *, as_of: str, floor: str) -> list[Row]:
    """Each asset's last stored close on or before *as_of* and on or after *floor*."""
    try:
        return db.execute(
            """
            SELECT p.asset_id, p.date, p.close
            FROM price_daily p
            JOIN (
                SELECT asset_id, MAX(date) AS d FROM price_daily
                WHERE asset_id IN (SELECT value FROM json_each(?))
                  AND date <= ? AND date >= ? AND close IS NOT NULL
                GROUP BY asset_id
            ) last ON last.asset_id = p.asset_id AND last.d = p.date
            """,
            (json.dumps(sorted(set(asset_ids))), as_of, floor),
        ).fetchall()
    except DatabaseError:
        return []


def last_price_dates(db: Database, asset_ids: list[int]) -> dict[int, str]:
    """The newest stored bar per asset: the date the gateway's split adjustment is current to."""
    try:
        rows = db.execute(
            "SELECT asset_id, MAX(date) AS d FROM price_daily "
            "WHERE asset_id IN (SELECT value FROM json_each(?)) GROUP BY asset_id",
            (json.dumps(sorted(set(asset_ids))),),
        ).fetchall()
    except DatabaseError:
        return {}
    return {int(r["asset_id"]): str(r["d"]) for r in rows}


def split_rows(db: Database, asset_ids: list[int], *, engine_version: str) -> list[Row]:
    """The recorded splits (ex-date, ratio) of one corporate-action engine build."""
    try:
        return db.execute(
            "SELECT DISTINCT asset_id, ex_date, value FROM corporate_action "
            "WHERE asset_id IN (SELECT value FROM json_each(?)) "
            "AND action_type = 'SPLIT' AND engine_version = ? ORDER BY asset_id, ex_date",
            (json.dumps(sorted(set(asset_ids))), engine_version),
        ).fetchall()
    except DatabaseError:
        return []


# -- data-coverage report -----------------------------------------------------


def metric_versions_present(conn: Database) -> dict[str, list[str]]:
    """``{metric_group: [engine_version, ...]}`` for every version stored in
    ``fundamental_metrics`` (unordered; :mod:`kg_schema.versions` orders them). A row with a
    NULL ``engine_version`` predates versioning and is ignored. Empty for a database with
    no such table or no metric rows."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT metric_group, engine_version FROM fundamental_metrics "
            "WHERE engine_version IS NOT NULL"
        ).fetchall()
    except DatabaseError:
        return {}
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(str(row["metric_group"]), []).append(str(row["engine_version"]))
    return out


def metric_version_stats(conn: Database) -> list[tuple[str, str, int, str | None, str | None]]:
    """``(metric_group, engine_version, rows, first computed_at, last computed_at)`` for every
    version stored in ``fundamental_metrics`` -- the listing behind ``quant versions`` (T-093).
    Like :func:`metric_versions_present`, it deliberately reads across versions. Empty for a
    database with no such table."""
    try:
        rows = conn.execute(
            "SELECT metric_group, engine_version, COUNT(*) AS n_rows, MIN(computed_at) AS "
            "first_at, MAX(computed_at) AS last_at FROM fundamental_metrics "
            "WHERE engine_version IS NOT NULL GROUP BY metric_group, engine_version"
        ).fetchall()
    except DatabaseError:
        return []
    return [
        (
            str(r["metric_group"]),
            str(r["engine_version"]),
            int(r["n_rows"]),
            None if r["first_at"] is None else str(r["first_at"]),
            None if r["last_at"] is None else str(r["last_at"]),
        )
        for r in rows
    ]


def _distinct_ids(db: Database, sql: str, params: tuple[object, ...]) -> set[int]:
    try:
        return {int(r[0]) for r in db.execute(sql, params)}
    except DatabaseError:  # table not created in this DB yet
        return set()


class StaleAsOf(RuntimeError):
    """A ``quant``/``cycle`` ``as_of`` is past the price spine's last stored date (T-110)."""


def last_price_date(conn: Database) -> str | None:
    """The most recent date ``price_daily`` actually holds a bar for -- the price spine's
    right edge. ``None`` if ``price_daily`` has no rows yet (or doesn't exist in this DB)."""
    try:
        row = conn.execute("SELECT MAX(date) AS d FROM price_daily").fetchone()
    except DatabaseError:
        return None
    return str(row["d"]) if row and row["d"] is not None else None


def stale_as_of_reason(conn: Database, as_of: str) -> str | None:
    """Why *as_of* is past the price spine's last stored date (T-110) -- or ``None`` when
    it's safe to proceed. A run dated past what prices actually exist for reads/analyzes
    data that is, at best, weeks stale while claiming to be "as of" a later date.

    ``None`` is also returned when ``price_daily`` has no rows at all: a completely empty
    spine is a different problem (no price data whatsoever) than a stale *as_of*, and
    surfaces on its own via the gate/panel/universe checks each caller already has."""
    last = last_price_date(conn)
    if last is None or as_of <= last:
        return None
    return f"{as_of} is past price_daily's last stored date ({last})"


class StaleGateVersion(RuntimeError):
    """``data_quality_issue`` holds rows under an older Ring-1 gate version but none under the
    current one -- a gate-methodology bump (T-065/T-116) landed without its re-gate (``python -m
    fundamental_agent quality``) having run yet."""


def stale_gate_version_reason(conn: Database, gate_version: str) -> str | None:
    """Why reading Ring-1 quarantines/HARD issues under *gate_version* would be unsafe right
    now -- or ``None`` when it's safe to proceed.

    A consumer (``cycle``) reads only rows of the *current* ``gate_version`` (T-065's own
    versioning: a threshold change re-gates as a parallel, append-only set of rows rather than
    reclassifying the old ones in place). If the database already has issues recorded under an
    *older* version but none yet under the current one, the gate bumped without its one-time
    re-gate running -- every quarantine and every HARD ``DQ_*``/``DATA_QUALITY`` veto would
    silently vanish, not because the underlying filings got cleaner, but because nothing has
    judged them under the new version yet.

    ``None`` is also returned when ``data_quality_issue`` has no rows at all: Ring-1 has simply
    never run on this database, a different (bootstrap) situation the gate's own absence
    already makes visible, not a version regression to guard against here."""
    try:
        current = conn.execute(
            "SELECT COUNT(*) FROM data_quality_issue WHERE gate_version = ?", (gate_version,)
        ).fetchone()[0]
    except DatabaseError:  # table not created in this DB yet
        return None
    if current:
        return None
    older = {
        str(r[0]) for r in conn.execute("SELECT DISTINCT gate_version FROM data_quality_issue")
    }
    if not older:
        return None
    return (
        f"data_quality_issue holds rows under {', '.join(sorted(older))} but none under "
        f"{gate_version} yet -- run `python -m fundamental_agent quality` to re-gate first"
    )


# -- veto stints: one shared point-in-time predicate (T-125) ----------------------------
#
# `veto` holds stints (raised_on / cleared_on / last_seen_on -- cycle dates), not per-date
# events: a stint is active at cutoff C iff `raised_on <= C AND (cleared_on IS NULL OR
# cleared_on > C)`. Both `cycle` (the HARD filter, the SOFT penalty) and `quant` (the
# liquidity/universe gate) read this same predicate, at C = the cycle/as-of date's T-1, so
# there is exactly one definition of "active" rather than the two independent SQL copies
# that predated T-125 (`cycle.writers` and `quant.db` each had their own).


class VetoSchemaStale(RuntimeError):
    """``veto`` exists but predates migration m009's stint columns (``raised_on``/
    ``cleared_on``/``last_seen_on``) -- production `migrate` is deliberately deferred by
    T-125's own PR, so a cycle/quant run against the un-migrated table would otherwise
    either silently read no vetoes at all (the old bare ``except DatabaseError`` swallowed
    "no such column" the same as "no such table") or crash on it mid-write (PR #103
    review). Raised instead, with an actionable fix, the same way
    :class:`StaleGateVersion` covers a stale Ring-1 gate version."""


def require_veto_stint_columns(conn: Database) -> bool:
    """``True`` once ``veto`` has m009's stint shape; ``False`` when the table does not
    exist at all yet (every caller's own empty/``None`` default already covers a database
    that has simply never run a cycle). Raises :class:`VetoSchemaStale` when ``veto``
    exists but is still the old per-(asset, rule, cycle_date) hit-row shape. Shared by
    every veto reader here and by :func:`cycle.writers.write_vetoes` before it writes."""
    if not conn.relation_exists("veto"):
        return False
    if "raised_on" not in conn.table_columns("veto"):
        raise VetoSchemaStale(
            "veto exists but predates m009 (no raised_on/cleared_on/last_seen_on stint "
            "columns) -- run `python -m fundamental_agent migrate` (or `pricing_agent "
            "migrate`) to apply it first"
        )
    return True


def hard_vetoed_as_of(conn: Database, cutoff_date: str) -> set[int]:
    """asset_ids with an open HARD veto stint active at *cutoff_date* -- the T-1 contagion
    filter (a cycle/as-of on N reads cutoff N-1)."""
    if not require_veto_stint_columns(conn):
        return set()  # no veto table in this DB
    rows = conn.execute(
        """
        SELECT DISTINCT asset_id FROM veto
        WHERE severity = 'HARD' AND raised_on <= ? AND (cleared_on IS NULL OR cleared_on > ?)
        """,
        (cutoff_date, cutoff_date),
    ).fetchall()
    return {int(r["asset_id"]) for r in rows}


def active_soft_vetoes(conn: Database, cutoff_date: str) -> dict[int, list[str]]:
    """asset_id -> the distinct SOFT rule_ids with an open stint active at *cutoff_date*.
    One entry per rule the stint is still open under -- never once per cycle it has held,
    since there is at most one open stint per (asset_id, rule_id) (T-125)."""
    if not require_veto_stint_columns(conn):
        return {}
    rows = conn.execute(
        """
        SELECT asset_id, rule_id FROM veto
        WHERE severity = 'SOFT' AND raised_on <= ? AND (cleared_on IS NULL OR cleared_on > ?)
        """,
        (cutoff_date, cutoff_date),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["asset_id"]), []).append(str(r["rule_id"]))
    return out


def veto_out_of_order_reason(conn: Database, cycle_date: str) -> str | None:
    """Why writing veto transitions at *cycle_date* would be out-of-order -- or ``None``
    when it's safe (T-125 f, the same rule T-097 applies to ``portfolio_position``).

    ``veto`` is not isolated between a live ``select``/``monitor`` run and a ``cycle
    backfill`` REPLAY the way ``portfolio_position``/``portfolio_position_replay`` are
    (T-115) -- both write the same shared stints table -- so this checks the latest
    transition date recorded anywhere in it (``raised_on``/``cleared_on``/``last_seen_on``),
    regardless of which run wrote it. *cycle_date* equal to that latest date is safe: that
    is the ordinary same-date re-run, which ``write_vetoes`` makes idempotent by undoing and
    redoing its own transitions first."""
    if not require_veto_stint_columns(conn):
        return None
    row = conn.execute(
        """
        SELECT MAX(d) AS latest FROM (
            SELECT raised_on AS d FROM veto
            UNION ALL SELECT cleared_on AS d FROM veto WHERE cleared_on IS NOT NULL
            UNION ALL SELECT last_seen_on AS d FROM veto
        )
        """
    ).fetchone()
    latest = row["latest"] if row else None
    if latest is None or cycle_date >= str(latest):
        return None
    return f"cycle_date {cycle_date} is older than the latest veto transition ({latest})"


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
        "WHERE score_type = 'FUNDAMENTAL' AND available_at <= ?",
        (as_of,),
    )
    metric_ids = _distinct_ids(
        fin_db,
        "SELECT DISTINCT f.asset_id FROM fundamental_metrics m "
        "JOIN sec_filings f ON f.id = m.filing_id WHERE m.available_at <= ?",
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
    "StaleAsOf",
    "UniverseMember",
    "check_coverage",
    "connect_ro",
    "current_version",
    "ensure",
    "last_price_date",
    "members_asof",
    "persist_coverage",
    "reconcile",
    "record",
    "resolve_asset_ids",
    "stale_as_of_reason",
    "symbols_asof",
]
