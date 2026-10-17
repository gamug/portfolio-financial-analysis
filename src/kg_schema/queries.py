"""Every SQL query in the ``kg_schema`` domain, in one place.

Trace of capabilities -- every query function here, grouped read/write:

Read (``universe.db`` -- point-in-time S&P 500 membership, never written here):
    connect_ro           strictly read-only open of universe.db (re-exports
                          kg_schema.db.connect_ro so callers keep importing it
                          from here)
    members_asof          open membership stints as of a date, as UniverseMember rows
    symbols_asof           just the ticker symbols from members_asof
    members_over_range    every company that was a member at any time in a date window,
                          one entry per CIK line, with the line's own membership span
                          (T-153 part 3a)
    resolve_asset_ids      member (symbol + CIK) -> assets.id lookup, by the CIK's ticker line
                            as of a date (T-153 part 2; `json_each`, never inserts)

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from portfolio_common.db import Database, DatabaseError, Row

from . import ticker_history
from .coverage import DEFAULT_MIN_OBSERVATION_DAYS, CoverageReport, SymbolCoverage
from .db import connect_ro as _kg_connect_ro
from .ddl import (
    ALLOWED_CYCLE_TYPES,
    INVALID_SEC_FILINGS_ACCESSIONS_QUERY,
    RUN_REFERENCE_MAP,
    UNMAPPED_RUN_KIND_QUERIES,
    UNMAPPED_RUN_KIND_SAMPLE_QUERIES,
    UNRESOLVED_REFERENCE_QUERIES,
    UNRESOLVED_REFERENCE_SAMPLE_QUERIES,
    WRONG_CYCLE_TYPE_QUERIES,
    WRONG_CYCLE_TYPE_SAMPLE_QUERIES,
)
from .identity import normalize_cik

SUPPORTED_UNIVERSE = "SP500"

#: The backtest window's first session (T-153 part 3a): every ingestion step must cover
#: every company that was a member at any time from here to the run's analysis date.
WINDOW_START = "2022-01-03"

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
    """Members with a stint covering *analysis_date* (ISO ``YYYY-MM-DD``).

    ``valid_to`` is the last day of membership (the day before the removal takes
    effect), inclusive; ``NULL`` = still a member. A symbol that left and rejoined
    has one row per stint; only the stint covering *analysis_date* matches. If the
    data ever carries overlapping stints for a symbol, the one with the latest
    ``valid_from`` wins.
    """
    _check_universe(universe)
    rows = universe_db.execute(
        """
        SELECT symbol, security, cik, gics_sector, gics_sub_industry,
               hq_location, date_added, founded, valid_from, valid_to
        FROM universe_membership
        WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
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
    """Just the ticker symbols with a stint covering *analysis_date*, sorted."""
    return [m.symbol for m in members_asof(universe_db, analysis_date, universe=universe)]


@dataclass(frozen=True)
class WindowMember:
    """One CIK line that was a member at some point in a date window (T-153 part 3a).

    *member* is the line's representative stint -- the latest one inside the window, its
    ``symbol`` the line's canonical (part 2) -- so ``sync_universe`` stores it like any
    member. ``window_from``/``window_to`` are the line's own membership span inside the
    window (``window_to is None`` = still a member), the bound every ingestion step
    fetches to: nothing filed or traded after ``window_to`` is fetched for the line.
    """

    member: UniverseMember
    window_from: str
    window_to: str | None
    stints: int


def members_over_range(
    universe_db: Database,
    fin_db: Database,
    start: str,
    end: str,
    *,
    universe: str = SUPPORTED_UNIVERSE,
) -> list[WindowMember]:
    """Every company that was a member at any time in ``[start, end]`` (T-153 part 3a).

    One entry per **CIK line** (part 2, APP-10a): a company's several stints collapse to one
    line (CDAY then DAY is one asset), while coexisting share classes stay separate lines
    (DISCA continues as WBD and is one line; DISCK is another). *fin_db* is read for the
    tracked ticker history (``ticker_history``), which names each line's canonical symbol;
    ``universe_db`` is read strictly as membership, never as identity.

    ``window_from`` is clamped to *start* and ``window_to`` is ``None`` for a line still a
    member at *end* -- the caller bounds its fetch by the line's own span, so a company that
    left gets nothing dated after its exit. Sorted by canonical symbol."""
    _check_universe(universe)
    rows = universe_db.execute(
        """
        SELECT symbol, security, cik, gics_sector, gics_sub_industry,
               hq_location, date_added, founded, valid_from, valid_to
        FROM universe_membership
        WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
        ORDER BY symbol, valid_from
        """,
        (end, start),
    ).fetchall()

    grouped: dict[tuple[str, str], list[UniverseMember]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        stint = UniverseMember(
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
        canonical = ticker_history.canonical_for_member(fin_db, symbol=stint.symbol, cik=stint.cik)
        own = normalize_cik(stint.cik)
        # A CIK's line is keyed by (CIK, canonical); a member with no CIK is its own line.
        key = (own, canonical.upper()) if own is not None else ("", canonical.upper())
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(stint)

    out: list[WindowMember] = []
    for key in order:
        stints = grouped[key]
        representative = max(stints, key=lambda s: (s.valid_from, s.symbol))
        canonical = ticker_history.canonical_for_member(
            fin_db, symbol=representative.symbol, cik=representative.cik
        )
        if canonical != representative.symbol:
            representative = replace(representative, symbol=canonical)
        window_from = max(min(s.valid_from for s in stints), start)
        window_to = (
            None
            if any(s.valid_to is None for s in stints)
            else max(s.valid_to for s in stints if s.valid_to is not None)
        )
        out.append(WindowMember(representative, window_from, window_to, len(stints)))
    return sorted(out, key=lambda w: w.member.symbol)


def resolve_asset_ids(
    db: Database,
    members: Iterable[tuple[str, str | None]],
    *,
    as_of: str,
) -> tuple[dict[str, int], list[str]]:
    """Map members to ``assets.id`` by CIK, as of a date (T-153 part 2).

    *members* is an iterable of ``(symbol, cik)`` pairs. A member whose CIK has a recorded
    ticker line (``ticker_history``, APP-10a) is matched by the line's **canonical** symbol, so a
    rename (FB -> META) resolves to the one asset the whole line shares. A matched asset whose
    stored CIK differs from the member's is **missing**, never merged -- a ticker is reassigned
    (the symbol ``FB`` is ProShares Trust, CIK 0001174610, today). A member with no CIK falls
    back to the line named by *as_of* (else the symbol itself) and a case-insensitive bare
    ticker match, as before.

    Pure read -- never inserts. Returns ``(mapping, missing)`` keyed by the input symbol
    (upper-cased); *missing* lists the input symbols with no matching ``assets`` row (they need
    an agent run to create their identity first).
    """
    wanted: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for symbol, cik in members:
        key = symbol.upper()
        if key not in seen:
            seen.add(key)
            wanted.append((key, cik))

    canonical = {
        key: ticker_history.canonical_for_member(db, symbol=key, cik=cik, as_of=as_of)
        for key, cik in wanted
    }
    tickers = sorted({canonical[key] for key, _ in wanted})

    by_ticker: dict[str, int] = {}
    cik_by_id: dict[int, str | None] = {}
    if tickers:
        rows = db.execute(
            "SELECT id, ticker, cik FROM assets "
            "WHERE UPPER(ticker) IN (SELECT value FROM json_each(?))",
            (json.dumps(tickers),),
        )
        for r in rows:
            by_ticker[str(r["ticker"]).upper()] = int(r["id"])
            cik_by_id[int(r["id"])] = r["cik"]

    mapping: dict[str, int] = {}
    missing: list[str] = []
    for key, cik in wanted:
        asset_id = by_ticker.get(canonical[key].upper())
        member_cik = normalize_cik(cik)
        stored_cik = normalize_cik(cik_by_id.get(asset_id)) if asset_id is not None else None
        if asset_id is None or (member_cik is not None and stored_cik not in (None, member_cik)):
            missing.append(key)
            continue
        mapping[key] = asset_id
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
    """Each asset's last stored close on or before *as_of* and on or after *floor*.

    Carries the bar's ``source``, so a reader can tell a gateway close from the private Tiingo
    import's (T-153 part 3d, A3)."""
    try:
        return db.execute(
            """
            SELECT p.asset_id, p.date, p.close, p.source
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


#: The tables the private import marks, and the statement that counts one of them. A static
#: dict of literal SQL: no identifier is ever interpolated (constitution Code & Git #10).
TIINGO_MARKED_SQL: dict[str, str] = {
    "price_daily": "SELECT COUNT(*) AS n FROM price_daily WHERE source = 'tiingo'",
    "price_observation": "SELECT COUNT(*) AS n FROM price_observation WHERE source = 'tiingo'",
    "quant_return_daily": "SELECT COUNT(*) AS n FROM quant_return_daily WHERE vendor = 'tiingo'",
    "corporate_action": "SELECT COUNT(*) AS n FROM corporate_action WHERE source = 'tiingo'",
}


def tiingo_marked_rows(db: Database) -> dict[str, int]:
    """How many rows of each marked table the private import wrote (T-153 part 3d, D9/R6).

    Rule 5 says the file and every row built from it are deleted, and D9's procedure is to discard
    the database and rebuild without the import -- so this is the *check* that a database holds
    none of them (0 in every table), not a postcondition of a deletion command. A table that does
    not exist yet counts 0."""
    out: dict[str, int] = {}
    for name, sql in TIINGO_MARKED_SQL.items():
        try:
            row = db.execute(sql).fetchone()
        except DatabaseError:  # a partial database
            out[name] = 0
            continue
        out[name] = int(row["n"]) if row is not None else 0
    return out


def tiingo_asset_ids(db: Database) -> list[int]:
    """The assets whose prices came from the private Tiingo import (T-153 part 3d, D8).

    The one way to name the imported lines on a database: rule 2's own per-line marker, the
    ``source`` of the line's stored bars. T-100 reports every evaluation result twice around
    this list, and ``verify_pilot``'s T-153f check reads it. Empty when nothing was imported
    (or when the table does not exist yet -- a fresh database)."""
    try:
        rows = db.execute(
            "SELECT DISTINCT asset_id FROM price_daily WHERE source = 'tiingo' ORDER BY asset_id"
        ).fetchall()
    except DatabaseError:
        return []
    return [int(r["asset_id"]) for r in rows]


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
    return split_rows_from(db, asset_ids, engine_versions=(engine_version,))


def split_rows_from(
    db: Database, asset_ids: list[int], *, engine_versions: Sequence[str]
) -> list[Row]:
    """The recorded splits of one or more engine builds, with the issuer's CIK (T-153 part 3d).

    The CIK is what the tracked event list keys an event by (a CIK's two share classes share one
    event); a database whose ``assets`` table has no row for the asset reports a blank CIK, and
    the row is then unclassifiable -- refused, never guessed. Only placeholders are interpolated
    (one per engine), never an identifier (constitution Code & Git #10)."""
    engines = tuple(dict.fromkeys(engine_versions))
    if not engines:
        return []
    marks = ", ".join("?" for _ in engines)
    sql = f"SELECT DISTINCT c.asset_id, c.ex_date, c.value, a.cik FROM corporate_action c JOIN assets a ON a.id = c.asset_id WHERE c.asset_id IN (SELECT value FROM json_each(?)) AND c.action_type = 'SPLIT' AND c.engine_version IN ({marks}) ORDER BY c.asset_id, c.ex_date"  # noqa: S608
    try:
        return db.execute(sql, (json.dumps(sorted(set(asset_ids))), *engines)).fetchall()
    except DatabaseError:
        return []


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
    members = members_asof(universe_db, as_of, universe=universe)
    symbols = [m.symbol for m in members]
    id_by_symbol, _missing = resolve_asset_ids(
        fin_db, [(m.symbol, m.cik) for m in members], as_of=as_of
    )

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
    The frozen local table keeps the old exclusive ``valid_to`` (the effective date
    of the removal); ``members_asof`` reads ``universe.db``, never this table. If
    this writer is ever revived it must write the day before the removal.
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


def unresolved_run_references(db: Database) -> list[dict[str, Any]]:
    """Find any non-null run/filing ID referencing a target table that does not exist,
    references with wrong cycle_type, or rows with an unmapped run_kind.

    Returns an empty list when all references resolve correctly.
    """
    violations: list[dict[str, Any]] = []

    # 1. Missing targets
    for (table, col, run_kind), target in RUN_REFERENCE_MAP.items():
        if not db.relation_exists(table) or not db.relation_exists(target):
            continue
        cnt_row = db.execute(UNRESOLVED_REFERENCE_QUERIES[(table, col, run_kind)]).fetchone()
        cnt = int(cnt_row[0]) if cnt_row and cnt_row[0] is not None else 0
        if cnt == 0:
            continue
        max_id = int(cnt_row[1]) if cnt_row and cnt_row[1] is not None else 0
        sample_rows = db.execute(
            UNRESOLVED_REFERENCE_SAMPLE_QUERIES[(table, col, run_kind)]
        ).fetchall()
        sample_ids = [int(r[0]) for r in sample_rows]
        violations.append(
            {
                "table": table,
                "column": col,
                "run_kind": run_kind,
                "target": target,
                "target_table": target,
                "reason": "missing",
                "unresolved_count": cnt,
                "max_unresolved_id": max_id,
                "sample_ids": sample_ids,
            }
        )

    # 2. Wrong cycle_type targets
    for (table, col, run_kind), allowed_types in ALLOWED_CYCLE_TYPES.items():
        if not db.relation_exists(table) or not db.relation_exists("cycle_run"):
            continue
        cnt_row = db.execute(WRONG_CYCLE_TYPE_QUERIES[(table, col, run_kind)]).fetchone()
        cnt = int(cnt_row[0]) if cnt_row and cnt_row[0] is not None else 0
        if cnt == 0:
            continue
        max_id = int(cnt_row[1]) if cnt_row and cnt_row[1] is not None else 0
        sample_rows = db.execute(WRONG_CYCLE_TYPE_SAMPLE_QUERIES[(table, col, run_kind)]).fetchall()
        sample_ids = [int(r[0]) for r in sample_rows]
        found_types = sorted(set(str(r[1]) for r in sample_rows))
        violations.append(
            {
                "table": table,
                "column": col,
                "run_kind": run_kind,
                "target": "cycle_run",
                "target_table": "cycle_run",
                "reason": "wrong_cycle_type",
                "cycle_type": ", ".join(found_types),
                "expected_types": list(allowed_types),
                "unresolved_count": cnt,
                "max_unresolved_id": max_id,
                "sample_ids": sample_ids,
            }
        )

    # 3. Unmapped run_kind rows
    for table in UNMAPPED_RUN_KIND_QUERIES:
        if not db.relation_exists(table):
            continue
        cnt_row = db.execute(UNMAPPED_RUN_KIND_QUERIES[table]).fetchone()
        cnt = int(cnt_row[0]) if cnt_row and cnt_row[0] is not None else 0
        if cnt == 0:
            continue
        max_id = int(cnt_row[1]) if cnt_row and cnt_row[1] is not None else 0
        sample_rows = db.execute(UNMAPPED_RUN_KIND_SAMPLE_QUERIES[table]).fetchall()
        sample_ids = [int(r[0]) for r in sample_rows]
        found_kinds = sorted(set(str(r[1]) for r in sample_rows))
        violations.append(
            {
                "table": table,
                "column": "run_id",
                "run_kind": ", ".join(found_kinds),
                "target": "unknown",
                "target_table": "unknown",
                "reason": "unmapped_run_kind",
                "unresolved_count": cnt,
                "max_unresolved_id": max_id,
                "sample_ids": sample_ids,
            }
        )

    return violations


def invalid_sec_filings_accessions(db: Database) -> list[dict[str, Any]]:
    """Find any sec_filings rows with invalid accession format or duplicate accession per asset.

    Routes through :data:`kg_schema.ddl.INVALID_SEC_FILINGS_ACCESSIONS_QUERY`.
    Returns an empty list when all accessions are valid and unique per asset.
    """
    if not db.relation_exists("sec_filings"):
        return []
    rows = db.execute(INVALID_SEC_FILINGS_ACCESSIONS_QUERY).fetchall()
    return [
        {
            "id": r[0],
            "asset_id": r[1],
            "form": r[2],
            "fiscal_year": r[3],
            "fiscal_period": r[4],
            "filing_date": r[5],
            "accession_number": r[6],
            "issue": r[7],
        }
        for r in rows
    ]


__all__ = [
    "SUPPORTED_UNIVERSE",
    "WINDOW_START",
    "StaleAsOf",
    "UniverseMember",
    "WindowMember",
    "check_coverage",
    "connect_ro",
    "current_version",
    "ensure",
    "invalid_sec_filings_accessions",
    "last_price_date",
    "members_asof",
    "members_over_range",
    "persist_coverage",
    "reconcile",
    "record",
    "resolve_asset_ids",
    "stale_as_of_reason",
    "symbols_asof",
    "unresolved_run_references",
]
