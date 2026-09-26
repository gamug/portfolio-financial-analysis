"""SQLite persistence for the fundamental analysis agent.

``assets`` and ``sectors`` may already be owned by another process, so they are only
ever created when missing -- never altered or dropped. Everything else in this module
is owned by this agent. ``fundamental_snapshot`` is append-only: one immutable row per
``(asset, form, fiscal_period)``, which is also what makes re-runs resumable.
"""

from __future__ import annotations

import calendar
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from portfolio_common.db import Database, Row

import kg_schema
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.statements import REGISTRY, Statements
from kg_schema.queries import UniverseMember
from kg_schema.trading_calendar import available_from

# Candidate XBRL scale/decimals-tagging defects: a filer (or, per SEC OSD staff
# guidance, the tagging tool it used) reports a share count off by an exact
# power of ten from its own prior filings -- see docs/model_fixes.md, F1.
# Every power of ten from 1e-9 to 1e9 (excluding 1e0, which would mean "no
# defect") -- not just the thousands-grouped ones (1e3/1e6/1e9) MCD/WAT
# happened to show; nothing rules out a filer being off by 10x or 100x.
_SHARE_SCALE_FACTORS: tuple[float, ...] = tuple(10.0**i for i in range(-9, 10) if i != 0)
# Generous on purpose: adjacent candidates are at least 10x apart, so precision
# buys nothing, but a loose band keeps the check robust when a real scale
# defect coincides with a few percent of ordinary organic share-count drift
# (buybacks/issuance) landing on a different overlapping period than the one
# being corrected.
_SHARE_SCALE_TOLERANCE = 0.10
# The EPS cross-check (T-103) only trusts an as-filed diluted EPS inside this range. Below the
# floor, rounding to the cent alone can exceed half the tolerance (MCHP FY2025: -$0.01); above
# the ceiling, the per-share figure is itself a scale defect (HAL 2022Q3 tags $0.60 as 600,000).
_EPS_MIN_ABS = 0.10
_EPS_MAX_ABS = 10_000.0
# Shares outstanding (a period-end balance) and weighted diluted shares (a period average) of
# the same filing differ by one period's buybacks, issuance and dilution: within 25% of a power
# of ten they are that power apart (T-103). Wider would snap a *different* concept -- NVR tags
# shares issued (20.6M) where outstanding is ~3.5M -- onto the wrong decade.
_CROSS_ITEM_BAND = 1.25

# Bump when the fact extraction or ratio engine changes in a way that should
# produce a *new* immutable row rather than silently colliding with the old one.
# ``metrics-v2`` (T-088): F1/F2/F4/C1 and T-092/T-094 changed the ratios and the filing set, so a
# row computed before them is distinguishable from one computed after.
# ``metrics-v3`` (T-102): utilities' total operating revenue now resolves, so every
# revenue-denominated ratio of AWK/DTE/DUK/NEE/SRE/XEL changes from NULL to a value; and
# (T-103) the share-scale check is point in time, so e.g. MCD FY2023-2025Q2's market caps
# are corrected; and (T-105) FCF yields, net debt / EBITDA and ROIC are annualized on 10-Qs.
# All three landed before any ``metrics-v3`` row was persisted.
FACTS_ENGINE_VERSION = "facts-v1"
METRICS_ENGINE_VERSION = "metrics-v3"


@dataclass(frozen=True)
class FilingKey:
    """Natural key for a ``sec_filings`` row."""

    asset_id: int
    form: str
    fiscal_year: int
    fiscal_period: str


@dataclass(frozen=True)
class FilingMeta:
    """Mutable metadata for a filing, refreshed on every fetch."""

    filing_date: str | None = None
    accession_number: str | None = None
    period_end: str | None = None


@dataclass(frozen=True)
class SnapshotRow:
    """A complete immutable fundamental snapshot ready to persist."""

    asset_id: int
    filing_id: int
    form: str
    fiscal_period: str
    score: float
    rating: str
    narrative: str
    strengths: Sequence[str]
    risks: Sequence[str]
    model: str
    metrics: dict[str, float | None]
    event_time: str  # the filing's period-end -- what the score is *about*


@dataclass(frozen=True)
class RunError:
    """One failure to record against a run."""

    ticker: str
    form: str | None
    fiscal_period: str | None
    stage: str
    message: str


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

CREATE TABLE IF NOT EXISTS sec_filings (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    form             TEXT NOT NULL,
    fiscal_year      INTEGER NOT NULL,
    fiscal_period    TEXT NOT NULL,
    filing_date      TEXT,
    accession_number TEXT,
    period_end       TEXT,
    retrieved_at     TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);

-- One filing is one row (T-120): an accession number reports one period, so no two rows of
-- an asset may share it. Before T-091 one Q3 10-Q per year was stored as Q1, Q2 and Q3 rows
-- with its accession and filing date; `fundamental_agent repair-accessions` removes those.
-- Existing rows are not checked here (a trigger never is); `run` refuses to start while any
-- remain (`shared_accession_filings`).
CREATE TRIGGER IF NOT EXISTS trg_sf_accession_insert BEFORE INSERT ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND NOT (o.form = NEW.form AND o.fiscal_period = NEW.fiscal_period)
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;
CREATE TRIGGER IF NOT EXISTS trg_sf_accession_update
BEFORE UPDATE OF asset_id, accession_number ON sec_filings
WHEN NEW.accession_number IS NOT NULL AND EXISTS (
    SELECT 1 FROM sec_filings o
    WHERE o.asset_id = NEW.asset_id AND o.accession_number = NEW.accession_number
      AND o.id <> NEW.id
)
BEGIN SELECT RAISE(ABORT, 'sec_filings: accession_number already used by another filing of this asset'); END;

CREATE TABLE IF NOT EXISTS financial_facts (
    id               INTEGER PRIMARY KEY,
    filing_id        INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    concept          TEXT NOT NULL,
    standard_concept TEXT,
    label            TEXT,
    period_key       TEXT NOT NULL,
    value            REAL,
    UNIQUE (filing_id, statement, concept, period_key)
);

CREATE TABLE IF NOT EXISTS fundamental_metrics (
    id          INTEGER PRIMARY KEY,
    filing_id   INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    inputs_json TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE (filing_id, metric_group, metric_name)
);

CREATE TABLE IF NOT EXISTS fundamental_snapshot (
    id             INTEGER PRIMARY KEY,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    filing_id      INTEGER NOT NULL REFERENCES sec_filings(id),
    form           TEXT NOT NULL,
    fiscal_period  TEXT NOT NULL,
    score          REAL NOT NULL,
    rating         TEXT NOT NULL,
    narrative      TEXT NOT NULL,
    strengths_json TEXT,
    risks_json     TEXT,
    model          TEXT NOT NULL,
    metrics_json   TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (asset_id, form, fiscal_period)
);

CREATE TABLE IF NOT EXISTS analysis_run (
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

CREATE TABLE IF NOT EXISTS analysis_run_error (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES analysis_run(id) ON DELETE CASCADE,
    ticker        TEXT,
    form          TEXT,
    fiscal_period TEXT,
    stage         TEXT,
    message       TEXT,
    created_at    TEXT NOT NULL
);
"""

_REQUIRED_ASSET_COLUMNS = {"id", "ticker", "company_name", "cik", "sector_id", "sub_industry"}
# Every literal, pre-written UPDATE `bump_run_counter` may run -- the column
# name is never interpolated into SQL at runtime, only used as a dict key, so
# there is no dynamic SQL construction from caller input to review at all.
_COUNTER_UPDATE_SQL = {
    "completed_units": "UPDATE analysis_run SET completed_units = completed_units + 1 WHERE id = ?",
    "skipped_units": "UPDATE analysis_run SET skipped_units = skipped_units + 1 WHERE id = ?",
    "failed_units": "UPDATE analysis_run SET failed_units = failed_units + 1 WHERE id = ?",
}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def ensure_schema(conn: Database) -> None:
    """Create any missing tables and verify a pre-existing ``assets`` is usable."""
    conn.create_schema(SCHEMA)
    conn.commit()
    columns = set(conn.table_columns("assets"))
    missing = _REQUIRED_ASSET_COLUMNS - columns
    if missing:
        raise RuntimeError(
            "existing 'assets' table is missing columns required by this agent: "
            f"{', '.join(sorted(missing))}"
        )
    # Shared cross-repo schema (score_snapshot, universe_membership, views, ...).
    # Additive only -- non-additive rebuilds run via `python -m fundamental_agent migrate`.
    kg_schema.ensure(conn)


# -- universe ---------------------------------------------------------------


def sync_universe(conn: Database, members: Iterable[UniverseMember]) -> int:
    """Insert/update ``assets`` and ``sectors`` from *members* -- the write-once
    identity path where a brand-new S&P 500 symbol first gets its ``assets.id``.

    Point-in-time membership itself is no longer kept here; it is read straight
    from ``universe.db`` as of the run's ``analysis_date``. Returns the count."""
    count = 0
    for m in members:
        sector_id = _upsert_sector(conn, m.gics_sector or "")
        conn.execute(
            """
            INSERT INTO assets (ticker, company_name, cik, sector_id, sub_industry)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = excluded.company_name,
                cik          = excluded.cik,
                sector_id    = excluded.sector_id,
                sub_industry = excluded.sub_industry
            """,
            (m.symbol, m.security, m.cik, sector_id, m.gics_sub_industry),
        )
        count += 1
    conn.commit()
    return count


def _upsert_sector(conn: Database, name: str) -> int | None:
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO sectors (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM sectors WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


def load_universe(
    conn: Database,
    *,
    tickers: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[Row]:
    """Return asset rows, restricted to the point-in-time universe *symbols* (and,
    if given, further to *tickers*), capped at *limit*."""
    query = "SELECT id, ticker, company_name, cik FROM assets"
    params: list[Any] = []
    clauses: list[str] = []
    if symbols is not None:
        placeholders = ", ".join("?" * len(symbols))
        clauses.append(f"UPPER(ticker) IN ({placeholders})")
        params.extend(s.upper() for s in symbols)
    if tickers:
        placeholders = ", ".join("?" * len(tickers))
        clauses.append(f"ticker IN ({placeholders})")
        params.extend(t.upper() for t in tickers)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY ticker"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(query, params))


# -- filings & facts ------------------------------------------------------


def upsert_filing(
    conn: Database,
    key: FilingKey,
    meta: FilingMeta,
    *,
    run_id: int | None = None,
    commit: bool = True,
) -> int:
    conn.execute(
        """
        INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date,
                                 available_at, accession_number, period_end, retrieved_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, form, fiscal_period) DO UPDATE SET
            filing_date      = excluded.filing_date,
            available_at     = excluded.available_at,
            accession_number = excluded.accession_number,
            period_end       = excluded.period_end,
            retrieved_at     = excluded.retrieved_at,
            run_id           = excluded.run_id
        """,
        (
            key.asset_id,
            key.form,
            key.fiscal_year,
            key.fiscal_period,
            meta.filing_date,
            available_from(meta.filing_date),  # T-107: the trading day after
            meta.accession_number,
            meta.period_end,
            _now(),
            run_id,
        ),
    )
    row = conn.execute(
        "SELECT id FROM sec_filings WHERE asset_id = ? AND form = ? AND fiscal_period = ?",
        (key.asset_id, key.form, key.fiscal_period),
    ).fetchone()
    if commit:
        conn.commit()
    return int(row["id"])


def append_financial_facts(  # noqa: PLR0913 - keyword-only provenance fields
    conn: Database,
    filing_id: int,
    facts: Iterable[dict[str, Any]],
    *,
    filing_version: str = FACTS_ENGINE_VERSION,
    event_time: str | None = None,
    run_id: int | None = None,
    commit: bool = True,
) -> int:
    """Append facts for *filing_id* -- never delete. Re-runs of the same
    *filing_version* collide on the unique key and are ignored; a restatement under
    a new *filing_version* coexists (post-``migrate``; pre-``migrate`` the older
    unique key still wins, which is the documented limitation)."""
    has_versioned = "filing_version" in conn.table_columns("financial_facts")
    now = _now()
    rows = [
        (
            filing_id,
            fact["statement"],
            fact["concept"],
            fact.get("standard_concept"),
            fact.get("label"),
            fact["period_key"],
            fact.get("value"),
            filing_version,
            event_time or (fact["period_key"][:10] if fact.get("period_key") else now),
            now,
            run_id,
        )
        for fact in facts
    ]
    if has_versioned:
        conn.executemany(
            """
            INSERT OR IGNORE INTO financial_facts
                (filing_id, statement, concept, standard_concept, label, period_key, value,
                 filing_version, event_time, ingested_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    else:  # pragma: no cover - only before kg_schema.ensure has run
        conn.executemany(
            """
            INSERT OR IGNORE INTO financial_facts
                (filing_id, statement, concept, standard_concept, label, period_key, value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [r[:7] for r in rows],
        )
    if commit:
        conn.commit()
    return len(rows)


def overlapping_history(
    conn: Database,
    asset_id: int,
    concepts: Sequence[str],
    period_keys: Iterable[str],
    *,
    exclude_filing_id: int,
) -> dict[str, float]:
    """Already-ingested ``financial_facts`` values for *asset_id* on any of
    *concepts*, restricted to whichever *period_keys* this filing's own payload
    also reports, excluding *exclude_filing_id* -- the filing currently being
    analyzed, whose own (possibly defective) facts are already appended by the
    time this runs (see :func:`detect_share_scale_factors`). One value per
    period_key: the most recently *filed* row wins when more than one earlier
    filing restated the same period.

    **Point in time (T-103)**: only filings filed strictly *before*
    *exclude_filing_id* count. A later filing is not history -- reading it was a
    look-ahead, and worse, a later filing that restates this period with the same
    scale defect then looked like first-hand proof the period was clean, vetoing
    the EPS signal (MCD FY2023-2025Q2, docs/model_fixes.md). When the current
    filing has no ``filing_date`` the old, undated behaviour applies; an earlier
    row with no ``filing_date`` cannot be proven earlier and is skipped."""
    keys = list(dict.fromkeys(period_keys))  # de-dup, keep order
    if not keys or not concepts:
        return {}
    # Only "?" placeholder characters are interpolated below (never a value), one
    # per item in *concepts*/*keys* -- every actual value is bound through the
    # params list, matching load_universe()'s established IN-clause pattern.
    concept_placeholders = ", ".join("?" * len(concepts))
    key_placeholders = ", ".join("?" * len(keys))
    rows = conn.execute(
        f"""
        SELECT ff.period_key, ff.value
        FROM financial_facts ff
        JOIN sec_filings sf ON sf.id = ff.filing_id
        LEFT JOIN sec_filings cur ON cur.id = ?
        WHERE sf.asset_id = ?
          AND ff.filing_id != ?
          AND (cur.filing_date IS NULL OR sf.filing_date < cur.filing_date)
          AND ff.concept IN ({concept_placeholders})
          AND ff.period_key IN ({key_placeholders})
          AND ff.value IS NOT NULL
        ORDER BY sf.filing_date ASC, ff.id ASC
        """,  # noqa: S608 -- interpolated segments are only "?" placeholders, see above
        [exclude_filing_id, asset_id, exclude_filing_id, *concepts, *keys],
    ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        out[str(row["period_key"])] = float(row["value"])  # last (most recent) wins
    return out


# Which already-recorded metric row's `inputs_json` carries each flow item's raw,
# never-TTM-adjusted value -- see :func:`ttm_flows`, F4 (docs/model_fixes.md). Both
# groups are always computed for every filing (fundamental_agent.metrics.CORE_GROUPS/
# OPTIONAL_GROUPS), so either metric name works as an anchor regardless of whether
# its own ratio value came out null.
_TTM_FLOW_SOURCE: dict[str, tuple[str, str]] = {
    "net_income": ("profitability", "return_on_assets"),
    "revenue": ("profitability", "return_on_assets"),
    "cogs": ("efficiency", "asset_turnover"),
    # T-105: the flows behind FCF yield, net debt / EBITDA and ROIC.
    "operating_cash_flow": ("cashflow", "free_cash_flow_margin"),
    "capital_expenditure": ("cashflow", "free_cash_flow_margin"),
    "operating_income": ("leverage", "net_debt_to_ebitda"),
    "depreciation_amortization": ("leverage", "net_debt_to_ebitda"),
    "interest_expense": ("leverage", "interest_coverage"),
    "stock_based_compensation": ("valuation", "free_cash_flow_yield"),
}


@dataclass(frozen=True)
class YTDPair:
    """One flow's year-to-date value at this 10-Q's period end, and the same year-to-date one
    year earlier -- both columns of the filing itself -- with that earlier period's end."""

    current: float | None
    prior: float | None
    prior_end: str | None


# A fiscal quarter/year end can drift from "exactly N months earlier": 52/53-week calendars
# (AAPL's quarters end on a Saturday) land within a few days. Adjacent quarters are ~91 days
# apart, so a window this wide still identifies exactly one.
_PERIOD_TOLERANCE_DAYS = 20
_QUARTER_MONTHS = 3


def _months_before(iso_date: str, months: int) -> date:
    """*iso_date* moved back *months* calendar months; a month-end stays a month-end
    (2023-08-31 - 3 -> 2023-05-31, 2024-02-29 - 3 -> 2023-11-30)."""
    d = date.fromisoformat(iso_date)
    year, month0 = divmod(d.year * 12 + (d.month - 1) - months, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    was_month_end = d.day == calendar.monthrange(d.year, d.month)[1]
    return date(year, month, last if was_month_end else min(d.day, last))


def _filing_near(conn: Database, asset_id: int, form: str, target: date) -> tuple[int, str] | None:
    """``(filing id, period_end)`` of the asset's *form* filing whose period ends closest
    to *target* (within :data:`_PERIOD_TOLERANCE_DAYS`), or ``None``.

    Filings are located by **date**, never by their ``fiscal_period`` label. The labels are
    ``FY{y}`` / ``{y}Q{n}`` with *y* the calendar year the period ends in, so a fiscal year
    and its own quarters carry different years unless the year ends in December -- STZ's
    ``FY2024`` (ending Feb-2024) owns the quarters ``2023Q1``-``Q3`` (T-094)."""
    iso = target.isoformat()
    row = conn.execute(
        """
        SELECT id, period_end FROM sec_filings
        WHERE asset_id = ? AND form = ? AND period_end IS NOT NULL
          AND ABS(julianday(period_end) - julianday(?)) <= ?
        ORDER BY ABS(julianday(period_end) - julianday(?))
        LIMIT 1
        """,
        (asset_id, form, iso, _PERIOD_TOLERANCE_DAYS, iso),
    ).fetchone()
    return (int(row["id"]), str(row["period_end"])) if row else None


def _recorded_flow(
    conn: Database, filing_id: int, item: str, engine_version: str = METRICS_ENGINE_VERSION
) -> float | None:
    """The single-quarter (or FY) *item* value already recorded for a filing, read from the
    metric row's own audit ``inputs_json`` -- already resolved through
    :meth:`Statements.get`'s full concept-selection logic (F2) at the time that filing was
    processed, so this never re-derives concept resolution itself.

    Pinned to *engine_version* -- the version the running engine writes (T-105 review): a
    TTM must never combine this engine's values with an older engine's, which may resolve a
    concept differently. A prior filing not yet recorded under this version reads as missing,
    so the caller falls through to its next method."""
    group, name = _TTM_FLOW_SOURCE[item]
    row = conn.execute(
        """
        SELECT inputs_json FROM fundamental_metrics
        WHERE filing_id = ? AND metric_group = ? AND metric_name = ? AND engine_version = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (filing_id, group, name, engine_version),
    ).fetchone()
    if row is None or not row["inputs_json"]:
        return None
    value = json.loads(row["inputs_json"]).get(item)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _quarter_flow_ending(
    conn: Database,
    asset_id: int,
    quarter_end: date,
    item: str,
    engine_version: str = METRICS_ENGINE_VERSION,
) -> float | None:
    """The single-quarter value of *item* for the fiscal quarter ending near *quarter_end*.

    A 10-Q reports quarters 1-3. A fiscal **Q4** has no 10-Q -- the 10-K reports only the
    full-year total -- so it is derived as that 10-K's FY flow minus the three 10-Q quarters
    inside the same fiscal year, all located by date, and only when all four are ingested;
    otherwise ``None`` and the caller falls back to ``x4`` (F4, docs/model_fixes.md)."""
    quarter = _filing_near(conn, asset_id, "10-Q", quarter_end)
    if quarter is not None:
        return _recorded_flow(conn, quarter[0], item, engine_version)
    fiscal_year = _filing_near(conn, asset_id, "10-K", quarter_end)
    if fiscal_year is None:
        return None
    fy = _recorded_flow(conn, fiscal_year[0], item, engine_version)
    if fy is None:
        return None
    total = fy
    for months in (_QUARTER_MONTHS * 3, _QUARTER_MONTHS * 2, _QUARTER_MONTHS):
        q = _filing_near(conn, asset_id, "10-Q", _months_before(fiscal_year[1], months))
        flow = _recorded_flow(conn, q[0], item, engine_version) if q else None
        if flow is None:
            return None
        total -= flow
    return total


def _fiscal_year_between(conn: Database, asset_id: int, after: str, before: str) -> int | None:
    """The asset's 10-K whose fiscal year ends strictly between *after* and *before* -- the
    year a prior-year year-to-date column belongs to -- or ``None``."""
    row = conn.execute(
        "SELECT id FROM sec_filings WHERE asset_id = ? AND form = '10-K' "
        "AND period_end > ? AND period_end < ? ORDER BY period_end DESC LIMIT 1",
        (asset_id, after, before),
    ).fetchone()
    return int(row["id"]) if row else None


def ttm_detail(  # noqa: PLR0913 - the filing's identity, its flows, and the version pin
    conn: Database,
    asset_id: int,
    *,
    period_end: str,
    current: dict[str, float],
    ytd: dict[str, YTDPair] | None = None,
    engine_version: str = METRICS_ENGINE_VERSION,
) -> dict[str, TTMFlow]:
    """Each flow's trailing-twelve-month value on a 10-Q, by the first method that works:

    1. ``ytd`` (T-105): ``FY(prior 10-K) - YTD(last year) + YTD(this year)``. Needs only the
       filing's own two year-to-date columns and the recorded prior 10-K, so it also covers a
       filer whose cash-flow statement has no quarterly column at all (XOM);
    2. ``quarters`` (F4): this quarter plus the three before it, all recorded;
    3. ``x4``: this quarter times four -- flagged downstream as crude annualization.

    A flow with none of the three (no current value, no usable year-to-date pair) is absent.
    When the identity *and* four quarters are both computable, the four-quarter sum rides
    along as :attr:`TTMFlow.alt` for the cross-check -- the identity stays the value: it uses
    the filing's own restated comparative, so it survives a restated prior year that the
    as-filed quarters do not (AT&T's WarnerMedia spin-off, see
    :func:`fundamental_agent.quality.record_ttm_crosscheck`). Every recorded value read is of
    *engine_version* only."""
    out: dict[str, TTMFlow] = {}
    for item in sorted(set(current) | set(ytd or {})):
        summed = (
            _trailing_quarters(
                conn, asset_id, period_end, item, current[item], engine_version=engine_version
            )
            if item in current
            else None
        )
        pair = (ytd or {}).get(item)
        if pair and pair.current is not None and pair.prior is not None and pair.prior_end:
            fy_filing = _fiscal_year_between(conn, asset_id, pair.prior_end, period_end)
            fy = (
                _recorded_flow(conn, fy_filing, item, engine_version)
                if fy_filing is not None
                else None
            )
            if fy is not None:
                out[item] = TTMFlow(fy - pair.prior + pair.current, "ytd", alt=summed)
                continue
        if summed is not None:
            out[item] = TTMFlow(summed, "quarters")
        elif item in current:
            out[item] = TTMFlow(current[item] * 4.0, "x4")
    return out


def _trailing_quarters(  # noqa: PLR0913 - one quarter's identity plus the version pin
    conn: Database,
    asset_id: int,
    period_end: str,
    item: str,
    value: float,
    *,
    engine_version: str = METRICS_ENGINE_VERSION,
) -> float | None:
    """*value* (this quarter) plus the three recorded quarters before it, or ``None`` when
    any of them is missing."""
    total = value
    for k in (1, 2, 3):
        end = _months_before(period_end, _QUARTER_MONTHS * k)
        flow = _quarter_flow_ending(conn, asset_id, end, item, engine_version)
        if flow is None:
            return None
        total += flow
    return total


def ttm_flows(
    conn: Database,
    asset_id: int,
    *,
    period_end: str,
    current: dict[str, float],
    engine_version: str = METRICS_ENGINE_VERSION,
) -> dict[str, float]:
    """Trailing-twelve-month value of each *current* flow (this 10-Q's own single-quarter
    value, whose period ends on *period_end*), keyed the same way (F4,
    docs/model_fixes.md): a 10-Q reports a 3-month flow, but ROA/ROE/turnover ratios divide
    it by an instantaneous balance-sheet stock, so it must be measured on the same annual
    basis first. Sums the filing's own quarter plus the three quarters ending 3, 6 and 9
    months earlier when all three are already ingested (a prior quarter's own
    already-recorded, never-TTM-adjusted value -- see :func:`_recorded_flow` -- so this never
    compounds a prior TTM adjustment into the new one); otherwise falls back to
    ``current * 4`` for that item alone (a fresh ticker, or a not-yet-derivable Q4).

    The quarters are found by **date** relative to *period_end*, so it works for any fiscal
    calendar (T-094); it used to do arithmetic on the ``fiscal_period`` labels, which only
    line up for a December year-end."""
    out: dict[str, float] = {}
    for item, value in current.items():
        summed = _trailing_quarters(
            conn, asset_id, period_end, item, value, engine_version=engine_version
        )
        out[item] = summed if summed is not None else value * 4.0
    return out


def _scale_factor_between(new_value: float | None, anchor_value: float | None) -> float | None:
    """The candidate power-of-ten factor under which ``new_value * factor``
    matches ``anchor_value`` (within ``_SHARE_SCALE_TOLERANCE``), or ``None``
    if no candidate fits -- including the "clean miss" case where the two
    values already agree (no scale defect between this particular pair)."""
    if new_value is None or anchor_value is None or new_value <= 0 or anchor_value <= 0:
        return None
    ratio = anchor_value / new_value  # factor to multiply new_value by
    for factor in _SHARE_SCALE_FACTORS:
        if abs(ratio - factor) <= factor * _SHARE_SCALE_TOLERANCE:
            return factor
    return None


def _unanimous_factor(reported: dict[str, float], anchors: dict[str, float]) -> float | None:
    """The single power-of-ten factor *every* period_key present in both
    ``reported`` and ``anchors`` agrees on -- ``None`` if there's no overlap,
    if the agreeing periods don't all pick the same factor, or if even one
    compared period doesn't fit *any* candidate factor (it may look
    correctly scaled already, which is exactly the case that must block
    borrowing a factor found from a *different* period, not be silently
    skipped -- see docs/model_fixes.md, F1)."""
    matched: set[float] = set()
    for period_key, anchor_value in anchors.items():
        new_value = reported.get(period_key)
        if new_value is None:
            continue
        factor = _scale_factor_between(new_value, anchor_value)
        if factor is None:
            return None  # this period doesn't fit any known defect shape -- disqualify
        matched.add(factor)
    return matched.pop() if len(matched) == 1 else None


def _signal_factor(
    reported: dict[str, float], anchors: dict[str, float], target_key: str
) -> tuple[bool, float | None]:
    """One anchor source's read on ``reported[target_key]``: ``(target_is_clean,
    factor)``. Direct evidence *at the target period itself* is decisive --
    ``factor`` is set only if it fits a known defect shape, and
    ``target_is_clean`` is ``True`` exactly when it doesn't, meaning this
    source has first-hand proof the target period needs no correction (which
    must not be overridden by a factor inferred from some *other* period --
    see docs/model_fixes.md, F1). With no direct anchor at the target, a
    factor may still be inferred from this filing's *other* reported periods,
    requiring unanimous agreement (:func:`_unanimous_factor`); in that case
    there is no first-hand opinion on the target, so ``target_is_clean`` is
    always ``False``."""
    target_value = reported.get(target_key)
    if target_value is None:
        return False, None
    target_anchor = anchors.get(target_key)
    if target_anchor is not None:
        factor = _scale_factor_between(target_value, target_anchor)
        return factor is None, factor
    others = {k: v for k, v in reported.items() if k != target_key}
    return False, _unanimous_factor(others, anchors)


def _eps_implied_diluted_shares(stmts: Statements) -> dict[str, float]:
    """``net_income / as-filed diluted EPS``, per period -- an independent,
    same-filing anchor needing no filing history at all: net income and
    diluted EPS are both GAAP-required disclosures already ingested from this
    filing's own payload (``us-gaap_EarningsPerShareDiluted`` -- see
    ``fundamental_agent.statements.REGISTRY["eps_diluted"]``), and their ratio
    is the filer's own weighted-average diluted share count, independent of
    whatever ``diluted_shares`` itself reports. This is what catches a defect
    that has already persisted long enough (or a filing cadence sparse enough)
    that no clean prior filing remains to compare against -- see
    ``docs/model_fixes.md``, F1.

    T-103: the numerator is net income *available to common* where the filing
    reports it (EPS's own numerator; plain net income otherwise), and an EPS
    outside ``[_EPS_MIN_ABS, _EPS_MAX_ABS]`` is not used -- see those constants."""
    net_income = {**stmts.get_all("net_income"), **stmts.get_all("net_income_to_common")}
    eps = stmts.get_all("eps_diluted")
    out: dict[str, float] = {}
    for period_key, ni in net_income.items():
        e = eps.get(period_key)
        if e is None or not _EPS_MIN_ABS <= abs(e) <= _EPS_MAX_ABS:
            continue
        implied = ni / e
        if implied > 0:  # a share count is never negative or zero
            out[period_key] = implied
    return out


def _magnitude_offset(value: float | None, reference: float | None) -> float | None:
    """The power of ten (``1.0`` = none) putting *value* in *reference*'s order of
    magnitude, when the two agree to within ``_CROSS_ITEM_BAND`` of it; ``None`` when
    either is missing or non-positive, or the ratio sits between powers of ten."""
    if value is None or reference is None or value <= 0 or reference <= 0:
        return None
    log_ratio = math.log10(reference / value)
    k = round(log_ratio)
    if abs(log_ratio - k) > math.log10(_CROSS_ITEM_BAND):
        return None
    return 10.0**k


def _cross_item_signal(
    stmts: Statements, period_key: str, eps_implied: dict[str, float]
) -> tuple[bool, float | None]:
    """``shares_outstanding``'s read against the same filing's weighted diluted count --
    used only when EPS independently confirms that diluted count at the target period
    (T-103). ``(True, None)``: same order of magnitude, so outstanding is clean;
    ``(False, factor)``: it is off by ``factor``. Otherwise no opinion."""
    out_key = stmts.resolve_column("shares_outstanding", period_key)
    outstanding = stmts.get_all("shares_outstanding").get(out_key) if out_key else None
    diluted = stmts.get_all("diluted_shares").get(period_key)
    implied = eps_implied.get(period_key)
    if diluted is None or implied is None or abs(implied / diluted - 1.0) > _SHARE_SCALE_TOLERANCE:
        return False, None  # diluted itself is not confirmed at the target
    offset = _magnitude_offset(outstanding, diluted)
    if offset is None:
        return False, None
    return (True, None) if offset == 1.0 else (False, offset)


def detect_share_scale_factors(
    conn: Database, asset_id: int, stmts: Statements, period_key: str, *, exclude_filing_id: int
) -> dict[str, float]:
    """For ``shares_outstanding`` and ``diluted_shares`` independently: the
    power-of-ten factor *this filing's own* ``period_key`` value needs to be
    *multiplied by* to correct a known SEC XBRL share-count scale/tagging
    defect (see ``docs/model_fixes.md``, F1) -- never a real capital-structure
    event, which never lands within a wide 10% tolerance of an exact power of
    ten. Two independent signals are checked, either one sufficient: (1)
    already-ingested history for an overlapping period from a prior filing,
    and, for ``diluted_shares`` only, (2) this same filing's own
    EPS-implied share count (:func:`_eps_implied_diluted_shares`), which needs
    no history and so also catches a defect that has already aged out of
    every filing's reporting window, or a sparse filing cadence with no
    genuinely overlapping period at all.

    Direct evidence *at* ``period_key`` from either signal is decisive and
    wins outright -- including when it shows the target period needs no
    correction, which overrides a factor a signal might otherwise infer from
    a *different*, genuinely defective period in the same filing (a filing
    can restate an old period incorrectly while its own current period is
    fine, or vice versa; borrowing a factor across periods without checking
    this would risk a false correction -- see docs/model_fixes.md, F1).
    Returns ``{}`` for an item with no evidence from either signal, or where
    the two signals disagree on the factor (ambiguous; the caller leaves that
    item's value untouched rather than guessing)."""
    result: dict[str, float] = {}
    eps_implied = _eps_implied_diluted_shares(stmts)
    for item in ("shares_outstanding", "diluted_shares"):
        reported = stmts.get_all(item)
        target_key = stmts.resolve_column(item, period_key)
        if target_key is None or target_key not in reported:
            continue
        history = overlapping_history(
            conn,
            asset_id,
            REGISTRY[item].concepts,
            reported,
            exclude_filing_id=exclude_filing_id,
        )
        history_clean, history_factor = _signal_factor(reported, history, target_key)
        if item == "diluted_shares":
            # EPS is a weighted-average, duration-based measure -- it only
            # corroborates diluted_shares (also duration-based), never the
            # point-in-time shares_outstanding balance-sheet figure.
            eps_clean, eps_factor = _signal_factor(reported, eps_implied, target_key)
        else:
            # ...but an EPS-confirmed diluted count of the same filing is a magnitude
            # reference for shares_outstanding (T-103).
            eps_clean, eps_factor = _cross_item_signal(stmts, period_key, eps_implied)
        if history_clean or eps_clean:
            continue  # direct evidence says the target period itself is fine
        candidates = {f for f in (history_factor, eps_factor) if f is not None}
        if len(candidates) == 1:
            result[item] = candidates.pop()
    return result


def filing_available_at(conn: Database, filing_id: int | None) -> str | None:
    """The filing's ``available_at`` (T-107), which its metrics and FUNDAMENTAL score copy."""
    if filing_id is None:
        return None
    row = conn.execute("SELECT available_at FROM sec_filings WHERE id = ?", (filing_id,)).fetchone()
    return None if row is None else row["available_at"]


def record_metrics(  # noqa: PLR0913 - keyword-only provenance fields
    conn: Database,
    filing_id: int,
    results: Iterable[tuple[str, MetricResult]],
    *,
    engine_version: str = METRICS_ENGINE_VERSION,
    event_time: str | None = None,
    run_id: int | None = None,
) -> None:
    """Append computed metrics -- one immutable row per
    ``(filing_id, group, name, engine_version)``. Recomputing with the same
    *engine_version* is a no-op; a new version writes a parallel row."""
    now = _now()
    available_at = filing_available_at(conn, filing_id)
    has_versioned = "engine_version" in conn.table_columns("fundamental_metrics")
    base = [
        (
            filing_id,
            group,
            result.name,
            result.value,
            result.unit,
            json.dumps(result.inputs),
            now,
        )
        for group, result in results
    ]
    if has_versioned:
        conn.executemany(
            """
            INSERT OR IGNORE INTO fundamental_metrics
                (filing_id, metric_group, metric_name, value, unit, inputs_json, computed_at,
                 engine_version, event_time, run_id, available_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*row, engine_version, event_time or now, run_id, available_at) for row in base],
        )
    else:  # pragma: no cover - only before kg_schema.ensure has run
        conn.executemany(
            """
            INSERT OR IGNORE INTO fundamental_metrics
                (filing_id, metric_group, metric_name, value, unit, inputs_json, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            base,
        )
    conn.commit()


# -- filing sections (narrative text) ---------------------------------


# v2: block-aware flattening (heals span-split "Item" markers), title-only heading
# detection for cross-reference-style filers, filer-CIK archive paths, body clamp.
SECTIONS_ENGINE_VERSION = "edgar-html-item-split-v2"


def insert_filing_sections(  # noqa: PLR0913 - keyword-only provenance fields
    conn: Database,
    filing_id: int,
    sections: Iterable[Any],
    *,
    engine_version: str = SECTIONS_ENGINE_VERSION,
    event_time: str,
    source_url: str | None = None,
    run_id: int | None = None,
) -> int:
    """Append extracted narrative sections. Immutable per
    ``(filing_id, section_type, ordinal, engine_version)``. *sections* items are
    :class:`fundamental_agent.sections.Section`."""
    now = _now()
    rows = [
        (
            filing_id,
            s.section_type,
            s.item_number,
            s.heading,
            s.ordinal,
            s.char_start,
            s.char_end,
            s.text,
            s.sha256,
            s.word_count,
            engine_version,
            source_url,
            event_time,
            now,
            engine_version,
            run_id,
        )
        for s in sections
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO sec_filing_section
            (filing_id, section_type, item_number, heading, ordinal, char_start, char_end,
             text, text_sha256, word_count, extraction_method, source_url, event_time,
             retrieved_at, engine_version, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def filings_with_sections(conn: Database) -> set[int]:
    """``filing_id`` values that already have at least one extracted section."""
    return {int(r[0]) for r in conn.execute("SELECT DISTINCT filing_id FROM sec_filing_section")}


def shared_accession_filings(conn: Database) -> list[Row]:
    """Every filing row whose accession number another row of the same asset also carries
    -- the legacy pre-T-091 shape (T-120). Oldest period first within each accession."""
    return list(
        conn.execute(
            """
            SELECT f.id, f.asset_id, a.ticker, f.form, f.fiscal_year, f.fiscal_period,
                   f.period_end, f.filing_date, f.accession_number
            FROM sec_filings f
            JOIN assets a ON a.id = f.asset_id
            JOIN (
                SELECT asset_id, accession_number FROM sec_filings
                WHERE accession_number IS NOT NULL
                GROUP BY asset_id, accession_number HAVING COUNT(*) > 1
            ) s ON s.asset_id = f.asset_id AND s.accession_number = f.accession_number
            ORDER BY a.ticker, f.accession_number, f.period_end, f.id
            """
        )
    )


# -- snapshots & resume -------------------------------------------------


def completed_units(conn: Database) -> set[tuple[str, str, str]]:
    """``(ticker, form, fiscal_period)`` triples that already have a FUNDAMENTAL score."""
    rows = conn.execute(
        """
        SELECT a.ticker AS ticker, f.form AS form, f.fiscal_period AS fiscal_period
        FROM score_snapshot s
        JOIN assets a ON a.id = s.asset_id
        JOIN sec_filings f ON f.id = s.filing_id
        WHERE s.score_type = 'FUNDAMENTAL'
        """
    )
    return {(r["ticker"], r["form"], r["fiscal_period"]) for r in rows}


def completed_accessions(conn: Database) -> set[str]:
    """Accession numbers whose filing already has a FUNDAMENTAL score -- lets a resumed
    run skip the ``financials`` call for a filing it has already ingested (T-092: a year
    now holds up to three 10-Qs, each fetched by its own accession)."""
    rows = conn.execute(
        """
        SELECT DISTINCT f.accession_number AS accession
        FROM score_snapshot s
        JOIN sec_filings f ON f.id = s.filing_id
        WHERE s.score_type = 'FUNDAMENTAL' AND f.accession_number IS NOT NULL
        """
    )
    return {str(r["accession"]) for r in rows}


def insert_snapshot(conn: Database, row: SnapshotRow, *, run_id: int | None = None) -> None:
    """Append one immutable FUNDAMENTAL ``score_snapshot`` row. Resume-safe:
    a repeat ``(asset_id, 'FUNDAMENTAL', event_time)`` is ignored."""
    conn.execute(
        """
        INSERT INTO score_snapshot
            (asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, filing_id, rating, narrative, strengths_json, risks_json,
             run_kind, run_id, available_at)
        VALUES (?, 'FUNDAMENTAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'analysis', ?, ?)
        ON CONFLICT (asset_id, score_type, event_time) DO NOTHING
        """,
        (
            row.asset_id,
            row.score,
            row.score,
            row.event_time,
            _now(),
            row.model,
            json.dumps(row.metrics),
            row.filing_id,
            row.rating,
            row.narrative,
            json.dumps(list(row.strengths)),
            json.dumps(list(row.risks)),
            run_id,
            filing_available_at(conn, row.filing_id),
        ),
    )
    conn.commit()


# -- run log ------------------------------------------------------------


def start_run(
    conn: Database,
    *,
    params: dict[str, Any],
    as_of: str | None = None,
    code_version: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_run (started_at, params_json, status, as_of, code_version) "
        "VALUES (?, ?, 'running', ?, ?)",
        (_now(), json.dumps(params), as_of, code_version),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_run_plan(conn: Database, run_id: int, *, universe_size: int, planned_units: int) -> None:
    conn.execute(
        "UPDATE analysis_run SET universe_size = ?, planned_units = ? WHERE id = ?",
        (universe_size, planned_units, run_id),
    )
    conn.commit()


def bump_run_counter(conn: Database, run_id: int, column: str) -> None:
    try:
        sql = _COUNTER_UPDATE_SQL[column]
    except KeyError:
        raise ValueError(f"not a counter column: {column}") from None
    conn.execute(sql, (run_id,))
    conn.commit()


_MAX_ERROR_CHARS = 2000


def record_error(conn: Database, run_id: int, error: RunError) -> None:
    conn.execute(
        """
        INSERT INTO analysis_run_error
            (run_id, ticker, form, fiscal_period, stage, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            error.ticker,
            error.form,
            error.fiscal_period,
            error.stage,
            error.message[:_MAX_ERROR_CHARS],
            _now(),
        ),
    )
    conn.commit()


def finish_run(conn: Database, run_id: int, *, status: str) -> None:
    conn.execute(
        "UPDATE analysis_run SET finished_at = ?, status = ? WHERE id = ?",
        (_now(), status, run_id),
    )
    conn.commit()
