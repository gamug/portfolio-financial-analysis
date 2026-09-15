"""SQLite persistence for the fundamental analysis agent.

``assets`` and ``sectors`` may already be owned by another process, so they are only
ever created when missing -- never altered or dropped. Everything else in this module
is owned by this agent. ``fundamental_snapshot`` is append-only: one immutable row per
``(asset, form, fiscal_period)``, which is also what makes re-runs resumable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from portfolio_common.db import Allowlist, Database, Row

import kg_schema
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.statements import REGISTRY, Statements
from kg_schema.queries import UniverseMember

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
# The only columns `bump_run_counter` may interpolate into an UPDATE ... SET.
_COUNTER_COLUMNS = Allowlist("completed_units", "skipped_units", "failed_units")


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
    conn: Database, key: FilingKey, meta: FilingMeta, *, run_id: int | None = None
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
    conn: Database,
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
    filing restated the same period."""
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
        WHERE sf.asset_id = ?
          AND ff.filing_id != ?
          AND ff.concept IN ({concept_placeholders})
          AND ff.period_key IN ({key_placeholders})
          AND ff.value IS NOT NULL
        ORDER BY sf.filing_date ASC, ff.id ASC
        """,  # noqa: S608 -- interpolated segments are only "?" placeholders, see above
        [asset_id, exclude_filing_id, *concepts, *keys],
    ).fetchall()
    out: dict[str, float] = {}
    for row in rows:
        out[str(row["period_key"])] = float(row["value"])  # last (most recent) wins
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
    ``docs/model_fixes.md``, F1."""
    net_income = stmts.get_all("net_income")
    eps = stmts.get_all("eps_diluted")
    out: dict[str, float] = {}
    for period_key, ni in net_income.items():
        e = eps.get(period_key)
        if not e:
            continue
        implied = ni / e
        if implied > 0:  # a share count is never negative or zero
            out[period_key] = implied
    return out


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
            eps_clean, eps_factor = False, None
        if history_clean or eps_clean:
            continue  # direct evidence says the target period itself is fine
        candidates = {f for f in (history_factor, eps_factor) if f is not None}
        if len(candidates) == 1:
            result[item] = candidates.pop()
    return result


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


def insert_snapshot(conn: Database, row: SnapshotRow, *, run_id: int | None = None) -> None:
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
    if column not in _COUNTER_COLUMNS:
        raise ValueError(f"not a counter column: {column}")
    conn.execute(
        f"UPDATE analysis_run SET {column} = {column} + 1 WHERE id = ?",  # noqa: S608
        (run_id,),
    )
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
