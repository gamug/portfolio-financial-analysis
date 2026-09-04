"""SQLite persistence for the fundamental analysis agent.

``assets`` and ``sectors`` may already be owned by another process, so they are only
ever created when missing -- never altered or dropped. Everything else in this module
is owned by this agent. ``fundamental_snapshot`` is append-only: one immutable row per
``(asset, form, fiscal_period)``, which is also what makes re-runs resumable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from portfolio_common import kg_schema
from portfolio_common.kg_schema.universe_source import UniverseMember

from fundamental_agent.metrics.base import MetricResult

# Bump when the fact extraction or ratio engine changes in a way that should
# produce a *new* immutable row rather than silently colliding with the old one.
FACTS_ENGINE_VERSION = "facts-v1"
METRICS_ENGINE_VERSION = "metrics-v1"


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


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables and verify a pre-existing ``assets`` is usable."""
    conn.executescript(SCHEMA)
    conn.commit()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk); index by
    # position so this works regardless of the connection's row_factory.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
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


def sync_universe(conn: sqlite3.Connection, members: Iterable[UniverseMember]) -> int:
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
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
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
    conn: sqlite3.Connection, key: FilingKey, meta: FilingMeta, *, run_id: int | None = None
) -> int:
    conn.execute(
        """
        INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period,
                                 filing_date, accession_number, period_end, retrieved_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, form, fiscal_period) DO UPDATE SET
            filing_date      = excluded.filing_date,
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
    conn.commit()
    return int(row["id"])


def append_financial_facts(  # noqa: PLR0913 - keyword-only provenance fields
    conn: sqlite3.Connection,
    filing_id: int,
    facts: Iterable[dict[str, Any]],
    *,
    filing_version: str = FACTS_ENGINE_VERSION,
    event_time: str | None = None,
    run_id: int | None = None,
) -> int:
    """Append facts for *filing_id* -- never delete. Re-runs of the same
    *filing_version* collide on the unique key and are ignored; a restatement under
    a new *filing_version* coexists (post-``migrate``; pre-``migrate`` the older
    unique key still wins, which is the documented limitation)."""
    has_versioned = "filing_version" in {
        r[1] for r in conn.execute("PRAGMA table_info(financial_facts)")
    }
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
    conn.commit()
    return len(rows)


def record_metrics(  # noqa: PLR0913 - keyword-only provenance fields
    conn: sqlite3.Connection,
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
    has_versioned = "engine_version" in {
        r[1] for r in conn.execute("PRAGMA table_info(fundamental_metrics)")
    }
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
                 engine_version, event_time, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*row, engine_version, event_time or now, run_id) for row in base],
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
    conn: sqlite3.Connection,
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


def filings_with_sections(conn: sqlite3.Connection) -> set[int]:
    """``filing_id`` values that already have at least one extracted section."""
    return {int(r[0]) for r in conn.execute("SELECT DISTINCT filing_id FROM sec_filing_section")}


# -- snapshots & resume -------------------------------------------------


def completed_units(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
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


def insert_snapshot(
    conn: sqlite3.Connection, row: SnapshotRow, *, run_id: int | None = None
) -> None:
    """Append one immutable FUNDAMENTAL ``score_snapshot`` row. Resume-safe:
    a repeat ``(asset_id, 'FUNDAMENTAL', event_time)`` is ignored."""
    conn.execute(
        """
        INSERT INTO score_snapshot
            (asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, filing_id, rating, narrative, strengths_json, risks_json,
             run_kind, run_id)
        VALUES (?, 'FUNDAMENTAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'analysis', ?)
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
        ),
    )
    conn.commit()


# -- run log ------------------------------------------------------------


def start_run(
    conn: sqlite3.Connection,
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


def update_run_plan(
    conn: sqlite3.Connection, run_id: int, *, universe_size: int, planned_units: int
) -> None:
    conn.execute(
        "UPDATE analysis_run SET universe_size = ?, planned_units = ? WHERE id = ?",
        (universe_size, planned_units, run_id),
    )
    conn.commit()


def bump_run_counter(conn: sqlite3.Connection, run_id: int, column: str) -> None:
    if column not in {"completed_units", "skipped_units", "failed_units"}:
        raise ValueError(f"not a counter column: {column}")
    conn.execute(
        f"UPDATE analysis_run SET {column} = {column} + 1 WHERE id = ?",  # noqa: S608
        (run_id,),
    )
    conn.commit()


_MAX_ERROR_CHARS = 2000


def record_error(conn: sqlite3.Connection, run_id: int, error: RunError) -> None:
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


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str) -> None:
    conn.execute(
        "UPDATE analysis_run SET finished_at = ?, status = ? WHERE id = ?",
        (_now(), status, run_id),
    )
    conn.commit()
