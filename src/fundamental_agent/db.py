"""SQLite persistence for the fundamental analysis agent.

``assets`` and ``sectors`` may already be owned by another process, so they are only
ever created when missing -- never altered or dropped. Everything else in this module
is owned by this agent. ``fundamental_snapshot`` is append-only: one immutable row per
``(asset, form, fiscal_period)``. Re-runs resume by ``(ticker, form, period_end)`` -- the date,
never the label (T-140) -- see :func:`completed_units`.
"""

from __future__ import annotations

import ast
import itertools
import json
import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from portfolio_common.db import Database, Row

import kg_schema
from fundamental_agent import fiscal
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.statements import REGISTRY, Capital, CoverShares, Statements
from kg_schema import identity, migrations, ticker_history
from kg_schema.company_profile import profile_as_of
from kg_schema.queries import UniverseMember
from kg_schema.trading_calendar import available_from
from kg_schema.versions import RULE_INPUT_GROUP, VALUE_INPUTS_METRIC

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
# are corrected; and (T-105) FCF yields, net debt / EBITDA and ROIC are annualized on 10-Qs;
# and (T-117) a revenue ``total_concepts`` match that the statement's own later, smaller
# "total"-labeled row contradicts is corrected or rejected, not trusted outright -- APA's
# FY2023-2025 revenue changes from ~2x too large to the statement's own derived figure.
# All four landed before any ``metrics-v3`` row was persisted.
# ``metrics-v5`` (T-140): a revenue total the gateway dropped (T-042: a filer that tags it only
# with a dimension) is rebuilt from the statement's own "Total revenues and other" line, so APA's
# 2021Q1-2023Q3 revenue, and every ratio over it, changes from missing (or a $0 component) to a
# value; and a 10-Q's fiscal quarter is read from the fiscal year end, not the gateway's tag, so
# a 52/53-week filer's quarters stop sharing (or leaving unstored) a label.
# ``metrics-v6`` (T-148, part 1 of 2): line items are read in the sign they were filed in
# (ID-11/ID-12: the displayed value times edgartools' ``preferred_sign``), so income tax, cost of
# goods, interest expense and capex change sign where they were displayed negated, and the ``abs()``
# repairs are gone; concepts are chosen by precedence (exact concepts in the spec's order, then
# ``standard``, then label: ID-02a), parent net income is ``NetIncomeLoss`` or ``ProfitLoss`` less
# the noncontrolling interest and never the "available to common" line (ID-03, N13), income tax is
# the total or current + deferred (ID-02b), debt follows MET-07 / D-01 / D-06(d) / N23 and a
# missing debt or cash leaves EV and ROIC empty instead of 0 (MET-00), and a balance sheet is read
# only at its period end (N6). Part 2 keeps ``metrics-v6`` (no non-scratch database is built between
# the two PRs).
# ``metrics-v7`` (T-151 part 1 of 3): the line items gain their sourced component maps. D&A is the
# filed ``DepreciationDepletionAndAmortization`` / ``DepreciationAndAmortization`` /
# ``DepreciationAmortizationAndAccretionNet``, else the sum of the filed D&A-nature children of the
# FASB 2026 cash-flow arc (``Depreciation`` + ``Depletion`` / ``OtherDepreciationAndAmortization`` /
# ``AmortizationOfIntangibleAssets``), refused when any other D&A-named line is on the face
# (C1); long-term debt falls back to the filed children of ``LongTermDebtNoncurrent`` (sfp-cls arc)
# or ``LongTermDebt`` (sfp-dbo arc, flagged ``includes_current_maturities``) -- the REIT
# ``SecuredDebt`` + ``UnsecuredDebt`` shape resolves -- refused when a debt-named balance-sheet line
# is outside the tree used (C2); the net capex element
# ``PaymentsForProceedsFromProductiveAssets`` is read only when filed above zero, a net-proceeds
# value being skipped for the caption fallback (C3); and a 10-Q's quarter label follows the
# company's own fiscal year end, overriding a payload balance-sheet instant that disagrees (C4).
# Every new flag reaches the metrics as ``flag_<item>_<flag>`` inputs, as T-148's do (C6).
# ``metrics-v8`` (T-156): the D&A resolver reads the income statement when the cash-flow statement
# files no D&A fact at all (E1, flagged ``da_income_statement``), and the depreciation-only filers
# recover through the existing sourced sum when its amortization child is on the income statement
# (E2, flagged ``da_sum_cross_statement``). A company-extension caption equal to the standard label
# of an accepted D&A element is read through the shared caption fallback (E3, flagged
# ``caption_fallback`` + ``extension_concept``), and operating income is formed from the FASB 2026
# ``soi`` calculation identities when its terms are filed (E4, flagged ``oi_from_calc_identity``).
# ``metrics-v9`` (T-159a): a line item the filing reports under **another standard concept** is
# read from a substitute, after the item's own concepts (ID-02a precedence, so an item that
# resolves today resolves to the same value): the receivables, cost of sales, inventory, redeemable
# NCI, interest expense, minority interest, cash and net income totals, the ASC 320
# short-term-investment classes (disjoint, so they are summed), the sole member of a component
# family (inventory), the long-term debt components and the short-term debt lines (MET-07), and
# the exact two-part sums (D&A = ``Depreciation`` + ``AmortizationOfIntangibleAssets``; pre-tax
# income = domestic + foreign). The short-term investments the filing reports are no longer read
# as 0 (the only defaulted input). Flags: ``cogs_excludes_da``, ``net_income_to_common``.
# ``metrics-v11`` (T-161): a D&A aggregate that equals the filing's ``AmortizationOfIntangibleAssets``
# for the same period, with ``Depreciation`` filed separately, is that filer's amortization alone,
# not D&A: the reading becomes ``Depreciation`` + amortization (flagged
# ``da_amortization_only_sum``), and when the depreciation fact is ambiguous the item is refused
# (``da_amortization_only_refused``) instead of storing the mixed value. NDSN FY2025: the 10-K's
# D&A 79,264,000 becomes 150,523,000 and its TTM follows (T-161).
FACTS_ENGINE_VERSION = "facts-v1"
METRICS_ENGINE_VERSION = "metrics-v11"


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
    cik: str | None = (
        None  # the CIK the filing was listed under (T-149): the asset's or a predecessor's
    )


@dataclass(frozen=True)
class SnapshotRow:
    """A complete immutable fundamental snapshot ready to persist."""

    asset_id: int
    filing_id: int
    form: str
    fiscal_period: str
    # T-073 (D1): None when an applicable pillar has no value -- the score is empty, the row
    # records why, and the member is unscored (T-119).
    score: float | None
    rating: str | None
    narrative: str
    strengths: Sequence[str]
    risks: Sequence[str]
    model: str
    metrics: dict[str, float | None]
    event_time: str  # the filing's period-end -- what the score is *about*
    prompt_hash: str  # T-113: sha256 of the LLM interaction that produced (or failed into) this
    # T-073 (D4): the pillar block (`pillars`, `pillar_weights`, `pillar_weights_version`,
    # `coverage`), stored beside the flat metrics inside `score_snapshot.inputs_json`.
    pillars: dict[str, Any] = field(default_factory=dict)


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
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- T-157: `financial_facts` and `fundamental_metrics` are append-only **per version**, and the key
-- SPEC §5 declares (m002's `filing_version`, m003's `engine_version`) is the shape a fresh database
-- is born with -- so a new version's rows coexist from the first run, never only after a manual
-- `migrate`. An older database keeps its narrower key and is rebuilt by m002 / m003 (T-154's guards
-- unchanged); writing one is refused by `_require_target_key` below, never silently ignored.
CREATE TABLE IF NOT EXISTS financial_facts (
    id               INTEGER PRIMARY KEY,
    filing_id        INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    concept          TEXT NOT NULL,
    standard_concept TEXT,
    label            TEXT,
    period_key       TEXT NOT NULL,
    value            REAL,
    filing_version   TEXT NOT NULL DEFAULT 'pre-v1',
    event_time       TEXT NOT NULL DEFAULT '',
    ingested_at      TEXT,
    run_id           INTEGER,
    correction_rule  TEXT,
    preferred_sign   REAL,
    UNIQUE (filing_id, statement, concept, period_key, filing_version)
);

CREATE TABLE IF NOT EXISTS fundamental_metrics (
    id           INTEGER PRIMARY KEY,
    filing_id    INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL,
    metric_name  TEXT NOT NULL,
    value        REAL,
    unit         TEXT,
    inputs_json  TEXT,
    computed_at  TEXT NOT NULL,
    engine_version TEXT NOT NULL DEFAULT 'pre-v1',
    event_time   TEXT NOT NULL DEFAULT '',
    run_id       INTEGER,
    available_at TEXT,
    UNIQUE (filing_id, metric_group, metric_name, engine_version)
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
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    # T-113: how many of this run's scores fell back to the rule-based synthesis
    # (the LLM's JSON verdict never came back parseable), additive on `analysis_run`.
    "fallback_units": "UPDATE analysis_run SET fallback_units = fallback_units + 1 WHERE id = ?",
}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class LegacyAppendOnlyKey(RuntimeError):
    """A write to an append-only table whose ``UNIQUE`` key is still the pre-migration one.

    ``financial_facts`` and ``fundamental_metrics`` are append-only **per version** (SPEC §5; m002
    and m003): their key carries ``filing_version`` / ``engine_version``, so a new version's rows
    coexist with the older ones. A database that predates the rebuild carries the narrower key,
    where the ``INSERT OR IGNORE`` of a new version's row is dropped **in silence**. T-157 refuses
    the write instead, naming the migration that rebuilds the table."""


def _require_target_key(conn: Database, table: str) -> None:
    """Refuse to write an append-only table whose key is not the migrations' target (T-157).

    A database :func:`ensure_schema` created is born at the target shape (:data:`SCHEMA` declares it);
    one created before T-157, or by an older release, is upgraded by
    ``python -m fundamental_agent migrate`` (m002 / m003 rebuild the table)."""
    wanted = migrations.target_unique_key(table)
    if wanted is None or migrations.at_target_unique_key(conn, table):
        return
    raise LegacyAppendOnlyKey(
        f"{table} carries its pre-migration UNIQUE key, narrower than ({', '.join(wanted)}): a row "
        "of a new version would be dropped in silence by INSERT OR IGNORE. Run `python -m "
        "fundamental_agent migrate` to rebuild it (m002/m003), or start from a database "
        "`fundamental_agent run` created."
    )


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


def sync_universe(conn: Database, members: Iterable[UniverseMember]) -> ticker_history.SyncResult:
    """Insert/update ``assets`` and ``sectors`` from *members* -- the write-once
    identity path where a brand-new S&P 500 symbol first gets its ``assets.id``.

    A ticker's CIK never changes silently (T-149): a member arriving with a CIK other than the
    stored one raises :class:`kg_schema.identity.CikConflict` before anything is written, and a
    stored CIK is never overwritten or emptied (a missing one is filled in).

    A rename is one asset (T-153 part 2): each member is stored under its CIK's line *canonical*
    symbol (``kg_schema.ticker_history``), so FB and META share one ``assets.id``. A member whose
    CIK already has an asset under another symbol is a share class (another same-CIK member in
    this batch), a recorded rename the database predates (adopted in place, returned in the
    result), or refused (:class:`kg_schema.ticker_history.TickerConflict`) -- before any write.

    Point-in-time membership itself is no longer kept here; it is read straight
    from ``universe.db`` as of the run's ``analysis_date``. Returns the count and any adoptions."""
    members = list(members)
    identity.refuse_cik_changes(conn, members)
    plans = ticker_history.plan_members(conn, [(m.symbol, m.cik) for m in members])
    adoptions: list[ticker_history.Adoption] = []
    count = 0
    for m, plan in zip(members, plans, strict=True):
        if plan.action == "adopt" and plan.asset_id is not None and plan.adopt_from is not None:
            # C4: retitle the one existing asset of the line -- its id and cik stay unchanged.
            # Adoption is not only for old databases: every future rename needs it. The ordinary
            # upsert below then refreshes the member's name/sector like any other member.
            conn.execute("UPDATE assets SET ticker = ? WHERE id = ?", (plan.ticker, plan.asset_id))
            adoptions.append(
                ticker_history.Adoption(plan.adopt_from, plan.ticker, plan.asset_id, str(plan.cik))
            )
        sector_id = _upsert_sector(conn, m.gics_sector or "")
        conn.execute(
            """
            INSERT INTO assets (ticker, company_name, cik, sector_id, sub_industry)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = excluded.company_name,
                cik          = COALESCE(NULLIF(assets.cik, ''), excluded.cik),
                sector_id    = excluded.sector_id,
                sub_industry = excluded.sub_industry
            """,
            (
                plan.ticker,
                m.security,
                identity.normalize_cik(m.cik),
                sector_id,
                m.gics_sub_industry,
            ),
        )
        count += 1
    conn.commit()
    return ticker_history.SyncResult(count, adoptions)


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


class FilingLabelCollision(RuntimeError):
    """Two different periods of one asset were given the same ``fiscal_period`` label (T-140)."""


def _refuse_label_collision(conn: Database, key: FilingKey, meta: FilingMeta) -> None:
    """The upsert below is keyed on the label, so a second period under a stored label would
    overwrite the first row in place -- its facts and score keep pointing at a filing that now
    reports another period. That is how a repeated gateway quarter tag once lost a filing without
    a trace; it must be loud instead. The usual cause now is a database labelled before T-140
    (a shifted fiscal calendar's quarters carry the gateway's tag): re-ingest it fresh."""
    stored = conn.execute(
        "SELECT period_end, accession_number FROM sec_filings "
        "WHERE asset_id = ? AND form = ? AND fiscal_period = ?",
        (key.asset_id, key.form, key.fiscal_period),
    ).fetchone()
    if (
        stored is not None
        and stored["period_end"]
        and meta.period_end
        and stored["period_end"] != meta.period_end
    ):
        raise FilingLabelCollision(
            f"{key.form} {key.fiscal_period} already holds the filing for period end "
            f"{stored['period_end']} (accession {stored['accession_number']}); refusing to "
            f"overwrite it with the one for {meta.period_end} (accession "
            f"{meta.accession_number})"
        )


class FilingReplacementRefused(RuntimeError):
    """A stored filing would be replaced by another accession of the same period (T-149)."""


def _refuse_replacement(conn: Database, key: FilingKey, meta: FilingMeta) -> None:
    """A period's value is the one in its original filing (PER-02, L-04): a stored row never
    changes accession. The upsert below is keyed on the period label, so a later filing of the
    same period -- an amendment, or a listing that now orders differently -- would overwrite the
    original's accession, date and period end while its facts stayed under the old one. A row
    with no accession yet (legacy) takes its first one; the same accession again is a re-run."""
    stored = conn.execute(
        "SELECT accession_number, filing_date FROM sec_filings "
        "WHERE asset_id = ? AND form = ? AND fiscal_period = ?",
        (key.asset_id, key.form, key.fiscal_period),
    ).fetchone()
    if (
        stored is not None
        and stored["accession_number"]
        and meta.accession_number
        and stored["accession_number"] != meta.accession_number
    ):
        raise FilingReplacementRefused(
            f"{key.form} {key.fiscal_period} already holds the original filing "
            f"{stored['accession_number']} (filed {stored['filing_date']}); refusing to replace "
            f"it with {meta.accession_number} (filed {meta.filing_date}): the original is the "
            "period's value (PER-02)"
        )


def upsert_filing(
    conn: Database,
    key: FilingKey,
    meta: FilingMeta,
    *,
    run_id: int | None = None,
    commit: bool = True,
) -> int:
    _refuse_label_collision(conn, key, meta)
    _refuse_replacement(conn, key, meta)
    conn.execute(
        """
        INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date,
                                 available_at, accession_number, period_end, retrieved_at, run_id, cik)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (asset_id, form, fiscal_period) DO UPDATE SET
            filing_date      = excluded.filing_date,
            available_at     = excluded.available_at,
            accession_number = excluded.accession_number,
            period_end       = excluded.period_end,
            retrieved_at     = excluded.retrieved_at,
            run_id           = excluded.run_id,
            cik              = COALESCE(sec_filings.cik, excluded.cik)
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
            meta.cik,
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
    a new *filing_version* coexists. The key is the one SPEC §5 declares (m002's
    ``filing_version``): a database born by :func:`ensure_schema` has it, and one that
    still carries the pre-migration key is refused (:class:`LegacyAppendOnlyKey`,
    T-157) rather than silently dropping the new version's rows.

    T-118: a *fact* carrying ``correction_rule`` (set by
    :func:`fundamental_agent.statements.iter_facts` from the gateway's own
    ``data["corrections"]``) is persisted with that rule id in the like-named
    column, so a value the gateway derived rather than returned as filed never
    lands in ``financial_facts`` indistinguishable from a real filed fact."""
    _require_target_key(conn, "financial_facts")
    columns = conn.table_columns("financial_facts")
    has_versioned = "filing_version" in columns
    has_correction = "correction_rule" in columns
    has_sign = "preferred_sign" in columns
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
            fact.get("correction_rule"),
            fact.get("preferred_sign"),
        )
        for fact in facts
    ]
    if has_versioned and has_correction and has_sign:
        conn.executemany(
            """
            INSERT OR IGNORE INTO financial_facts
                (filing_id, statement, concept, standard_concept, label, period_key, value,
                 filing_version, event_time, ingested_at, run_id, correction_rule, preferred_sign)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    elif has_versioned and has_correction:
        conn.executemany(
            """
            INSERT OR IGNORE INTO financial_facts
                (filing_id, statement, concept, standard_concept, label, period_key, value,
                 filing_version, event_time, ingested_at, run_id, correction_rule)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [r[:12] for r in rows],
        )
    elif has_versioned:
        conn.executemany(
            """
            INSERT OR IGNORE INTO financial_facts
                (filing_id, statement, concept, standard_concept, label, period_key, value,
                 filing_version, event_time, ingested_at, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [r[:11] for r in rows],
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


def insert_cover_shares(
    conn: Database,
    filing_id: int,
    shares: Iterable[CoverShares],
    *,
    event_time: str,
    run_id: int | None = None,
) -> int:
    """Append *filing_id*'s cover-page share counts (T-132); a re-run collides on the unique key
    and is ignored. Returns how many rows were offered."""
    now = _now()
    rows = [
        (filing_id, c.class_member, c.value, c.as_of_date, event_time, now, run_id) for c in shares
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO filing_cover_shares
            (filing_id, class_member, value, as_of_date, event_time, ingested_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


#: T-159c round 2: the cover-count flag is only the x1,000 / x1/1,000 defect, and it is measured
#: against the **median** of the class's counts in the company's up-to-4 prior filings (filed
#: strictly before). Within 10 % of either factor. A stored flag only -- the MKT guard stays the
#: value rule, so the market cap is unchanged.
_COVER_SCALE_TOLERANCE = 0.10
_COVER_SCALE_FACTORS = (1_000.0, 0.001)
_COVER_PRIOR_FILINGS = 4


def cover_share_scale_suspect(
    conn: Database,
    asset_id: int,
    shares: Iterable[CoverShares],
    *,
    filing_id: int,
    filing_date: str | None,
) -> bool:
    """Whether one of *shares* is within 10 % of x1,000 (or x1/1,000) the same class's **median**
    count in the company's up-to-4 prior filings (T-159c round 2). Point in time: only filings
    filed strictly before *filing_date* count. A stored flag for the reader; it never changes a
    value (the MKT guard decides the cap)."""
    entries = [entry for entry in shares if entry.value > 0]
    if not entries or not filing_date:
        return False
    rows = conn.execute(
        """
        SELECT fcs.filing_id, fcs.class_member, fcs.value
        FROM filing_cover_shares fcs
        JOIN sec_filings sf ON sf.id = fcs.filing_id
        JOIN sec_filings cur ON cur.id = ?
        WHERE sf.asset_id = ?
          AND fcs.filing_id != ?
          AND sf.filing_date IS NOT NULL
          AND sf.filing_date < cur.filing_date
          AND fcs.value > 0
        ORDER BY sf.filing_date DESC, sf.period_end DESC, fcs.id DESC
        """,
        (filing_id, asset_id, filing_id),
    ).fetchall()
    prior: dict[str, list[float]] = {}
    seen: dict[str, set[int]] = {}
    for row in rows:
        member = str(row["class_member"])
        prior_filing = int(row["filing_id"])
        if (
            len(seen.setdefault(member, set())) >= _COVER_PRIOR_FILINGS
            and prior_filing not in seen[member]
        ):
            continue
        seen[member].add(prior_filing)
        prior.setdefault(member, []).append(float(row["value"]))
    for entry in entries:
        values = prior.get(entry.class_member)
        if not values:
            continue
        median = statistics.median(values)
        if median <= 0:
            continue
        ratio = entry.value / median
        if any(
            abs(ratio / factor - 1.0) <= _COVER_SCALE_TOLERANCE for factor in _COVER_SCALE_FACTORS
        ):
            return True
    return False


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


# The audit input a 10-Q's anchor metric row carries beside the item's single-quarter value: the
# item's year-to-date at the filing's period end (PER-05, T-149) -- three, six or nine months.
YTD_INPUT_PREFIX = "ytd_"

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
    # F1 (T-151 part 2 fix 1): the roic group's effective_tax_rate row carries both on the filing's
    # own period, so a REIT's TTM rate can be built the way the other TTM flows are.
    "income_tax": ("roic", "effective_tax_rate"),
    "pretax_income": ("roic", "effective_tax_rate"),
}


@dataclass(frozen=True)
class YTDPair:
    """One flow's year-to-date value at this 10-Q's period end, and the same year-to-date one
    year earlier -- both columns of the filing itself -- with that earlier period's end."""

    current: float | None
    prior: float | None
    prior_end: str | None


_QUARTER_MONTHS = 3


def _months_before(iso_date: str, months: int) -> date:
    """*iso_date* moved back *months* calendar months; a month-end stays a month-end
    (2023-08-31 - 3 -> 2023-05-31, 2024-02-29 - 3 -> 2023-11-30)."""
    return fiscal.months_before(date.fromisoformat(iso_date), months)


def _filing_near(conn: Database, asset_id: int, form: str, target: date) -> tuple[int, str] | None:
    """``(filing id, period_end)`` of the asset's *form* filing whose period end rounds to the
    same month end as *target* (PER-08, :func:`fundamental_agent.fiscal.same_period_end`), the
    closest one if several do, or ``None``. It was a 20-day window; a window admits a period a
    month off, and the rounding admits a 52/53-week end days from the calendar one.

    Filings are located by **date**, never by their ``fiscal_period`` label. The labels are
    ``FY{y}`` / ``{y}Q{n}`` with *y* the calendar year the period ends in, so a fiscal year
    and its own quarters carry different years unless the year ends in December -- STZ's
    ``FY2024`` (ending Feb-2024) owns the quarters ``2023Q1``-``Q3`` (T-094)."""
    rows = conn.execute(
        "SELECT id, period_end FROM sec_filings "
        "WHERE asset_id = ? AND form = ? AND period_end IS NOT NULL",
        (asset_id, form),
    ).fetchall()
    matching = [
        (abs((date.fromisoformat(r["period_end"]) - target).days), int(r["id"]), r["period_end"])
        for r in rows
        if fiscal.same_period_end(date.fromisoformat(r["period_end"]), target)
    ]
    if not matching:
        return None
    _, filing_id, period_end = min(matching)
    return filing_id, str(period_end)


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
    return _recorded_input(conn, filing_id, item, item, engine_version)


def _recorded_ytd(
    conn: Database, filing_id: int, item: str, engine_version: str = METRICS_ENGINE_VERSION
) -> float | None:
    """The year-to-date *item* value a 10-Q recorded beside its single-quarter one (the
    ``ytd_<item>`` audit input :func:`ytd_inputs` stamps): nine months on a third quarter. Missing
    on a filing recorded before PER-05 (T-149), whose Q4 then falls back to the quarters."""
    return _recorded_input(conn, filing_id, item, YTD_INPUT_PREFIX + item, engine_version)


def _recorded_input(
    conn: Database, filing_id: int, item: str, key: str, engine_version: str
) -> float | None:
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
    value = json.loads(row["inputs_json"]).get(key)
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
    full-year total -- so it is the 10-K's FY flow minus the nine-month year-to-date of the
    third-quarter 10-Q (PER-05, checklist D-08), which needs no other quarter recorded. That
    10-Q must close one quarter before the fiscal year end (PER-08: [89, 98] days, the same month
    end). A third quarter recorded before the year-to-date was kept falls back to the FY flow minus
    the three 10-Q quarters, and only when all four are ingested; otherwise ``None`` and the
    caller falls back to ``x4`` (F4, docs/model_fixes.md)."""
    quarter = _filing_near(conn, asset_id, "10-Q", quarter_end)
    if quarter is not None:
        return _recorded_flow(conn, quarter[0], item, engine_version)
    fiscal_year = _filing_near(conn, asset_id, "10-K", quarter_end)
    if fiscal_year is None:
        return None
    fy = _recorded_flow(conn, fiscal_year[0], item, engine_version)
    if fy is None:
        return None
    ytd = _nine_month_ytd(conn, asset_id, fiscal_year[1], item, engine_version)
    if ytd is not None:
        return fy - ytd
    total = fy
    for months in (_QUARTER_MONTHS * 3, _QUARTER_MONTHS * 2, _QUARTER_MONTHS):
        q = _filing_near(conn, asset_id, "10-Q", _months_before(fiscal_year[1], months))
        flow = _recorded_flow(conn, q[0], item, engine_version) if q else None
        if flow is None:
            return None
        total -= flow
    return total


def _nine_month_ytd(
    conn: Database, asset_id: int, fiscal_year_end: str, item: str, engine_version: str
) -> float | None:
    """The nine-month year-to-date *item* of the 10-Q that closes the quarter before
    *fiscal_year_end*, or ``None``: no such 10-Q, one that is not a quarter before the year end
    (a changed fiscal year), or one recorded without its year-to-date."""
    year_end = date.fromisoformat(fiscal_year_end)
    third = _filing_near(conn, asset_id, "10-Q", _months_before(fiscal_year_end, _QUARTER_MONTHS))
    if third is None or not fiscal.is_quarter_apart(date.fromisoformat(third[1]), year_end):
        return None
    return _recorded_ytd(conn, third[0], item, engine_version)


def _quarter_columns(
    conn: Database, asset_id: int, period_end: str
) -> tuple[tuple[str, str | None, str | None], ...]:
    """H4: the four quarters a ``quarters`` TTM summed, each ``(label, period_end, accession)`` --
    the filing near each quarter end (a fiscal Q4 comes from the 10-K less the third quarter's nine
    months, PER-05)."""
    out: list[tuple[str, str | None, str | None]] = []
    for k in (0, 1, 2, 3):
        end = period_end if k == 0 else _months_before(period_end, _QUARTER_MONTHS * k).isoformat()
        filing = _filing_near(conn, asset_id, "10-Q", date.fromisoformat(end))
        if filing is None:
            filing = _filing_near(conn, asset_id, "10-K", date.fromisoformat(end))
        out.append(
            (
                f"quarter_{k}",
                filing[1] if filing else None,
                _filing_accession(conn, filing[0]) if filing else None,
            )
        )
    return tuple(out)


def _fy_item_concept(conn: Database, filing_id: int, item: str, engine_version: str) -> str | None:
    """The concept the 10-K's own rows resolved *item* to. The ``_TTM_FLOW_SOURCE`` row first,
    then any other row of the filing: the source row's provenance does not always list the item
    (``return_on_assets`` names neither revenue nor net income -- audit F-A11, APP 2025-Q2)."""
    group, name = _TTM_FLOW_SOURCE[item]
    rows = conn.execute(
        "SELECT metric_group, metric_name, inputs_json FROM fundamental_metrics "
        "WHERE filing_id = ? AND engine_version = ? ORDER BY id DESC",
        (filing_id, engine_version),
    ).fetchall()
    rows = sorted(rows, key=lambda r: (r["metric_group"], r["metric_name"]) != (group, name))
    for row in rows:
        if not row["inputs_json"]:
            continue
        provenance = json.loads(row["inputs_json"]).get("provenance")
        if isinstance(provenance, str):
            try:
                provenance = ast.literal_eval(provenance)
            except (ValueError, SyntaxError):
                continue
        entries = ((provenance or {}).get("items") or {}).get(item) or []
        concept = next((e.get("concept") for e in entries if e.get("concept")), None)
        if concept:
            return str(concept)
    return None


def _perimeter_mixed(  # noqa: PLR0913 - the identity's parts and the version pin
    conn: Database,
    asset_id: int,
    item: str,
    pair: YTDPair,
    fy_filing: int | None,
    engine_version: str,
    fy: float,
    period_end: str,
) -> bool:
    """Audit F-A11, without lookahead: the YTD identity's terms come from two perimeters.

    True when (a) this 10-Q's prior-year YTD differs by more than 2 % from the YTD the prior-year
    10-Q itself reported (the company recast that year), **and** (b) the 10-K that supplies the prior
    fiscal year did **not** restate its own comparative year (its prior-year column equals the year
    the previous 10-K reported, within 0.5 %): the recast came after that 10-K, so its FY is still on
    the old perimeter -- or it did, but a 10-Q of the current year recast again
    (:func:`_recast_within_year`). With no previous 10-K stored (the first year), (b) cannot be shown
    and the observed recast decides. No prior-year 10-Q or no comparative column: not called mixed."""
    if fy_filing is None or pair.prior is None or not pair.prior_end:
        return False
    old_q = _filing_near(conn, asset_id, "10-Q", date.fromisoformat(pair.prior_end))
    if old_q is None:
        return False
    original = _recorded_ytd(conn, old_q[0], item, engine_version)
    if original is None or original == 0 or abs(pair.prior - original) <= 0.02 * abs(original):
        return False
    if abs(pair.prior - original) < 0.01 * abs(fy):
        return False  # immaterial against the year (ABNB 2023: thousands -> millions rounding)
    fy_end = _filing_period_end(conn, fy_filing)
    if not fy_end:
        return False
    previous = _filing_near(conn, asset_id, "10-K", _months_before(fy_end, 12))
    if previous is None:
        # the company's first stored year: (b) cannot be tested, and the recast is observed -- the
        # identity's FY is not shown to be on the new perimeter (audit: 27 mixed / 17 consistent)
        return True
    previous_fy = _recorded_flow(conn, previous[0], item, engine_version)
    if previous_fy is None or previous_fy == 0:
        return False
    concept = _fy_item_concept(conn, fy_filing, item, engine_version)
    if not concept:
        return False
    parts = [c.strip() for c in str(concept).split(" + ")]
    comparative = 0.0
    found = set()
    for fact in conn.execute(
        "SELECT concept, value FROM financial_facts WHERE filing_id = ? AND period_key = ?",
        (fy_filing, f"{previous[1]} (FY)"),
    ):
        if fact["concept"] in parts and fact["concept"] not in found and fact["value"] is not None:
            found.add(fact["concept"])
            comparative += float(fact["value"])
    if found != set(parts):
        return False
    if abs(abs(comparative) - abs(previous_fy)) <= 0.005 * abs(previous_fy):
        return True
    # the 10-K restated its comparative year -- for a change up to the 10-K. A recast a later 10-Q
    # of the same year shows (GE's Vernova spin after the GEHC-restated FY2023 10-K) is not in it.
    return _recast_within_year(conn, asset_id, fy_end, period_end, parts, fy)


def _period_sum(conn: Database, filing_id: int, key: str, parts: list[str]) -> float | None:
    values: dict[str, float] = {}
    for fact in conn.execute(
        "SELECT concept, value FROM financial_facts WHERE filing_id = ? AND period_key = ?",
        (filing_id, key),
    ):
        if fact["concept"] in parts and fact["concept"] not in values and fact["value"] is not None:
            values[fact["concept"]] = float(fact["value"])
    return sum(values.values()) if len(values) == len(parts) else None


def _recast_within_year(  # noqa: PLR0913 - the identity's parts
    conn: Database, asset_id: int, fy_end: str, period_end: str, parts: list[str], fy: float
) -> bool:
    """Audit F-A11, no lookahead: whether a 10-Q of the current fiscal year (after the 10-K) recast
    the prior year again. For each two consecutive 10-Qs after the fiscal year end, up to and
    including this one (its facts are stored before its metrics), the later one's prior-year YTD
    minus its prior-year quarter is the prior-year YTD through the earlier quarter on the later
    basis; it must equal what the earlier 10-Q itself showed (within 2 %, or under 1 % of the year).
    Measured on the audit's labelled TTMs: 21 mixed found, 0 of 409 consistent flagged."""
    chain = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM sec_filings WHERE asset_id = ? AND form = '10-Q' "
            "AND period_end > ? AND period_end <= ? ORDER BY period_end",
            (asset_id, fy_end, period_end),
        ).fetchall()
    ]
    for earlier, later in itertools.pairwise(chain):
        keys1 = sorted(
            k
            for k in _period_keys(conn, earlier)
            if k[:10] <= fy_end and k.endswith(("(YTD)", "(Q1)"))
        )
        keys2 = _period_keys(conn, later)
        ytd2 = sorted(k for k in keys2 if k.endswith("(YTD)") and k[:10] <= fy_end)
        if not keys1 or not ytd2:
            continue
        q2 = sorted(
            k
            for k in keys2
            if "(" in k and not k.endswith(("(YTD)", "(FY)")) and keys1[-1][:10] < k[:10] <= fy_end
        )
        if not q2:
            continue
        a = _period_sum(conn, earlier, keys1[-1], parts)
        b = _period_sum(conn, later, ytd2[-1], parts)
        c = _period_sum(conn, later, q2[-1], parts)
        if a is None or b is None or c is None or a == 0:
            continue
        implied = b - c
        if abs(implied - a) > 0.02 * abs(a) and abs(implied - a) >= 0.01 * abs(fy):
            return True
    return False


def _period_keys(conn: Database, filing_id: int) -> list[str]:
    return [
        str(r["period_key"])
        for r in conn.execute(
            "SELECT DISTINCT period_key FROM financial_facts WHERE filing_id = ?", (filing_id,)
        ).fetchall()
    ]


def _filing_period_end(conn: Database, filing_id: int) -> str | None:
    row = conn.execute("SELECT period_end FROM sec_filings WHERE id = ?", (filing_id,)).fetchone()
    return str(row["period_end"]) if row and row["period_end"] else None


def _filing_accession(conn: Database, filing_id: int) -> str | None:
    row = conn.execute(
        "SELECT accession_number FROM sec_filings WHERE id = ?", (filing_id,)
    ).fetchone()
    return str(row["accession_number"]) if row and row["accession_number"] else None


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
    accession: str | None = None,
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
        flow_flags: tuple[str, ...] = ()
        if pair and pair.current is not None and pair.prior is not None and pair.prior_end:
            fy_filing = _fiscal_year_between(conn, asset_id, pair.prior_end, period_end)
            fy = (
                _recorded_flow(conn, fy_filing, item, engine_version)
                if fy_filing is not None
                else None
            )
            if fy is not None and _perimeter_mixed(
                conn, asset_id, item, pair, fy_filing, engine_version, fy, period_end
            ):
                # Audit F-A11 (T-161's rule: never store a mixed value): this 10-Q recast its prior
                # year (a divestiture, discontinued operations) after the 10-K, whose FY still holds
                # the old perimeter -- FY(old) - YTD(new) + YTD(new) mixes the two.
                out[item] = TTMFlow(
                    0.0, "refused", ytd=pair.current, flags=("ttm_perimeter_change",)
                )
                continue
            if fy is not None:
                # H4: the terms and the columns the identity combined, with the accession of the
                # filing that supplied each (the prior 10-K, then this filing's own two columns).
                out[item] = TTMFlow(
                    fy - pair.prior + pair.current,
                    "ytd",
                    alt=summed,
                    ytd=pair.current,
                    terms=(
                        ("prior_fiscal_year", fy),
                        ("prior_ytd", pair.prior),
                        ("ytd", pair.current),
                    ),
                    columns=(
                        (
                            "prior_fiscal_year",
                            _filing_period_end(conn, fy_filing) if fy_filing else None,
                            _filing_accession(conn, fy_filing) if fy_filing else None,
                        ),
                        ("prior_ytd", f"{pair.prior_end} (YTD)", accession),
                        ("ytd", f"{period_end} (YTD)", accession),
                    ),
                )
                continue
            if item == "depreciation_amortization" and fy_filing is not None:
                p_flag = _recorded_input(
                    conn,
                    fy_filing,
                    item,
                    "flag_depreciation_amortization_depreciation_only_not_da",
                    engine_version,
                )
                if p_flag == 1.0:
                    flow_flags = ("depreciation_only_not_da",)
                else:
                    # T-161: the prior 10-K's D&A was the amortization alone and could not be
                    # sourced (two different Depreciation facts): never combine the mixed value.
                    p_flag = _recorded_input(
                        conn,
                        fy_filing,
                        item,
                        "flag_depreciation_amortization_da_amortization_only_refused",
                        engine_version,
                    )
                    if p_flag == 1.0:
                        flow_flags = ("da_amortization_only_refused",)
        ytd_now = pair.current if pair else None
        quarters = _quarter_columns(conn, asset_id, period_end)
        if summed is not None:
            out[item] = TTMFlow(
                summed, "quarters", ytd=ytd_now, flags=flow_flags, columns=tuple(quarters)
            )
        elif item in current:
            out[item] = TTMFlow(
                current[item] * 4.0, "x4", ytd=ytd_now, flags=flow_flags, columns=tuple(quarters)
            )
        elif flow_flags:
            out[item] = TTMFlow(
                0.0, "refused", ytd=ytd_now, flags=flow_flags, columns=tuple(quarters)
            )
    return out


def stamp_ytd(metrics: Sequence[tuple[str, MetricResult]], flows: dict[str, TTMFlow]) -> None:
    """Record each flow's year-to-date in the audit inputs of its anchor metric row -- beside the
    item's single-quarter value, where :func:`_recorded_ytd` reads it back (PER-05, T-149). A
    10-Q's nine months are what a later Q4 is taken from. Only inputs change: no metric value."""
    for item, flow in flows.items():
        if flow.ytd is None or item not in _TTM_FLOW_SOURCE:
            continue
        group, name = _TTM_FLOW_SOURCE[item]
        for result_group, result in metrics:
            if result_group == group and result.name == name:
                result.inputs[YTD_INPUT_PREFIX + item] = flow.ytd


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
    *engine_version* is a no-op; a new version writes a parallel row. The key is the one
    SPEC §5 declares (m003's ``engine_version``): a database born by :func:`ensure_schema` has it, and
    one that still carries the pre-migration key is refused (:class:`LegacyAppendOnlyKey`, T-157)
    rather than silently dropping the new version's rows."""
    _require_target_key(conn, "fundamental_metrics")
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
            SELECT f.id, f.asset_id, a.ticker, a.cik, f.form, f.fiscal_year, f.fiscal_period,
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
    """``(ticker, form, period_end)`` triples that already have a FUNDAMENTAL score.

    Keyed on the period end, never the ``fiscal_period`` label (T-140): a label is derived, and
    one the gateway's quarter tag produced could repeat -- Waters' quarters ending 2023-07-01 and
    2023-09-30 both arrived as ``(Q3)``, so the second was skipped as already done. A date names
    one period on any fiscal calendar."""
    rows = conn.execute(
        """
        SELECT a.ticker AS ticker, f.form AS form, f.period_end AS period_end
        FROM score_snapshot s
        JOIN assets a ON a.id = s.asset_id
        JOIN sec_filings f ON f.id = s.filing_id
        WHERE s.score_type = 'FUNDAMENTAL' AND f.period_end IS NOT NULL
        """
    )
    return {(r["ticker"], r["form"], r["period_end"]) for r in rows}


def latest_fiscal_year_end(conn: Database, asset_id: int, before: str) -> str | None:
    """Period end of the asset's latest stored 10-K ending before *before* -- the fiscal year
    end a 10-Q's quarter is counted from (T-140, :mod:`fundamental_agent.fiscal`)."""
    row = conn.execute(
        "SELECT MAX(period_end) AS period_end FROM sec_filings "
        "WHERE asset_id = ? AND form = '10-K' AND period_end < ?",
        (asset_id, before),
    ).fetchone()
    return str(row["period_end"]) if row and row["period_end"] else None


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


def metrics_only_accessions(conn: Database) -> set[str]:
    """Accessions whose filing a **metrics-only** run completed (T-143 part A, P1).

    ``completed_accessions`` is keyed on the FUNDAMENTAL ``score_snapshot`` a scored run writes,
    so it never names a metrics-only filing. A filing counts as done here only when its own
    writing run recorded ``metrics_only`` **and** its ``value_inputs`` rule input exists: that row
    is written after ``record_metrics`` and ``utility_coverage.write`` (``pipeline._analyze_one``),
    so a filing killed before the sequence finished (facts and metrics stored, its ``value_inputs``
    row not) is not done and a later metrics-only run resumes it (reviewer B, 2026-10-18). A normal
    run ignores this set -- its skip sets stay FUNDAMENTAL-keyed -- so it still scores every such
    filing."""
    rows = conn.execute(
        """
        SELECT DISTINCT f.accession_number AS accession, r.params_json AS params
        FROM sec_filings f
        JOIN analysis_run r ON r.id = f.run_id
        JOIN fundamental_metrics m
            ON m.filing_id = f.id AND m.metric_group = ? AND m.metric_name = ?
        WHERE f.accession_number IS NOT NULL
        """,
        (RULE_INPUT_GROUP, VALUE_INPUTS_METRIC),
    ).fetchall()
    out: set[str] = set()
    for row in rows:
        try:
            params = json.loads(row["params"] or "{}")
        except ValueError:
            continue
        if isinstance(params, dict) and params.get("metrics_only") is True:
            out.add(str(row["accession"]))
    return out


def insert_snapshot(conn: Database, row: SnapshotRow, *, run_id: int | None = None) -> None:
    """Append one immutable FUNDAMENTAL ``score_snapshot`` row. Resume-safe:
    a repeat ``(asset_id, 'FUNDAMENTAL', event_time)`` is ignored.

    ``inputs_json`` carries the flat metrics (``group.metric`` -> value) and, from T-073, the
    pillar block beside them (D4) -- no schema change."""
    inputs: dict[str, Any] = dict(row.metrics)
    inputs.update(row.pillars)
    conn.execute(
        """
        INSERT INTO score_snapshot
            (asset_id, score_type, raw_value, normalized_score, event_time, computed_at,
             model, inputs_json, filing_id, rating, narrative, strengths_json, risks_json,
             run_kind, run_id, available_at, prompt_hash)
        VALUES (?, 'FUNDAMENTAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'analysis', ?, ?, ?)
        ON CONFLICT (asset_id, score_type, event_time) DO NOTHING
        """,
        (
            row.asset_id,
            row.score,
            row.score,
            row.event_time,
            _now(),
            row.model,
            json.dumps(inputs),
            row.filing_id,
            row.rating,
            row.narrative,
            json.dumps(list(row.strengths)),
            json.dumps(list(row.risks)),
            run_id,
            filing_available_at(conn, row.filing_id),
            row.prompt_hash,
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


def is_equity_reit(conn: Database, asset_id: int) -> bool:
    """M4 predicate (D6), read from the company profile (T-071 part 1, APP-00).

    The profile's ``reit`` overlay, keyed by the asset's CIK (APP-10a) -- the one source every
    package reads for the type. An asset whose CIK has no profile is ``False``, never a guessed type.
    The GICS predicate this replaces (sector ``Real Estate`` and a sub-industry ending in ``REITs``)
    is how the profile's own ``reit`` column was derived, and the two agree on every pilot asset
    (measured in T-071 part 1)."""
    row = conn.execute("SELECT cik FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row or not row["cik"]:
        return False
    profile = profile_as_of(str(row["cik"]))
    return bool(profile and profile.reit)


# -- the prior-year capital and the company profile (MET-01/02, MD-02/35/38; T-151 part 2) ---------

_FACTS_SELECT_HAS_SIGN = (
    "SELECT statement, concept, label, standard_concept, period_key, value, preferred_sign "
    "FROM financial_facts WHERE filing_id = ? ORDER BY id"
)
_FACTS_SELECT = (
    "SELECT statement, concept, label, standard_concept, period_key, value, NULL AS preferred_sign "
    "FROM financial_facts WHERE filing_id = ? ORDER BY id"
)


def statements_from_facts(conn: Database, filing_id: int) -> Statements:
    """Rebuild a :class:`Statements` from the stored ``financial_facts`` of one filing (the
    pipeline's own reader; ``scripts/sec_xcheck/common.py`` has an equivalent for the audit
    scripts). ``financial_facts`` stores no abstract or dimensional rows, so the rebuilt payload
    carries one row per ``(statement, concept)`` -- enough to resolve every balance-sheet item
    :func:`prior_capital` reads."""
    has_sign = "preferred_sign" in conn.table_columns("financial_facts")
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(_FACTS_SELECT_HAS_SIGN if has_sign else _FACTS_SELECT, (filing_id,)):
        row = rows.setdefault(
            (r["statement"], r["concept"]),
            {
                "concept": r["concept"],
                "label": r["label"],
                "standard_concept": r["standard_concept"],
                "abstract": False,
                "dimension": False,
            },
        )
        if r["preferred_sign"] is not None:
            row.setdefault("preferred_sign", r["preferred_sign"])
        row.setdefault(r["period_key"], r["value"])
    payload: dict[str, list[Any]] = {"income_statement": [], "balance_sheet": [], "cash_flow": []}
    for (statement, _concept), row in rows.items():
        payload.setdefault(statement, []).append(row)
    return Statements.from_payload(payload)


def company_profile(conn: Database, asset_id: int) -> tuple[str | None, str | None]:
    """The company's GICS sector and sub-industry (today's snapshot, L-03) -- the fallback for a CIK
    the company profile does not hold (T-071 part 1 reads the tracked profile first, in
    :func:`attach_context`)."""
    row = conn.execute(
        "SELECT s.name AS sector, a.sub_industry FROM assets a "
        "LEFT JOIN sectors s ON a.sector_id = s.id WHERE a.id = ?",
        (asset_id,),
    ).fetchone()
    if not row:
        return None, None
    return (row["sector"] or None, row["sub_industry"] or None)


def _filing_with_period_end(
    conn: Database, asset_id: int, form: str, target: date
) -> tuple[int, str] | None:
    """The stored *form* filing of *asset_id* whose period end rounds to the same month end as
    *target* (PER-08), and that period end."""
    rows = conn.execute(
        "SELECT id, period_end FROM sec_filings "
        "WHERE asset_id = ? AND form = ? AND period_end IS NOT NULL ORDER BY period_end",
        (asset_id, form),
    ).fetchall()
    for row in rows:
        period_end = str(row["period_end"])
        try:
            parsed = date.fromisoformat(period_end[:10])
        except ValueError:
            continue
        if fiscal.same_period_end(parsed, target):
            return int(row["id"]), period_end
    return None


def _capital_from_stored(
    conn: Database, asset_id: int, form: str, target: date | None
) -> Capital | None:
    """Capital at the balance-sheet column of the stored *form* filing ending on *target* (a
    different filing's own facts), or ``None`` when there is no such filing."""
    if target is None:
        return None
    found = _filing_with_period_end(conn, asset_id, form, target)
    if found is None:
        return None
    filing_id, period_end = found
    prior = statements_from_facts(conn, filing_id)
    return replace(
        prior.capital_at(period_end), source="prior_10k" if form == "10-K" else "prior_10q"
    )


def prior_capital(
    conn: Database, asset_id: int, form: str, period_key: str, stmts: Statements
) -> None:
    """Resolve and attach the prior-year capital a filing's ratios divide by (T-151 part 2):

    * ``stmts.prior_capital`` -- the end of the prior fiscal year (MET-01/MET-02, MD-02/35): the
      filing's own comparative column when it carries one (D1 step 1), else the stored prior 10-K's
      balance sheet (step 2), else empty (flagged ``prior_year_bs_missing``, step 3);
    * ``stmts.prior_quarter_capital`` -- the prior year's same quarter (MD-38's Δ, a 10-Q; D8),
      from the stored prior-year 10-Q. ``None`` for a 10-K.

    Only the pipeline calls this: it reads another filing's stored facts, which a hermetic
    ``Statements`` does not have."""
    try:
        target = date.fromisoformat(period_key[:10])
    except ValueError:
        return
    own_key = stmts.prior_fiscal_year_end_key(period_key)
    if own_key is not None:
        stmts.prior_capital = replace(stmts.capital_at(own_key), source="filing")
    else:
        fye = latest_fiscal_year_end(conn, asset_id, period_key[:10])
        target_date = date.fromisoformat(fye[:10]) if fye else None
        stmts.prior_capital = _capital_from_stored(conn, asset_id, "10-K", target_date) or (
            stmts.capital_at(None)
        )
    if form == "10-Q":
        stmts.prior_quarter_capital = _capital_from_stored(
            conn, asset_id, "10-Q", fiscal.one_year_before(target)
        )


def attach_context(  # noqa: PLR0913 - the filing's identity plus the statements it enriches
    conn: Database,
    asset_id: int,
    form: str,
    period_key: str,
    stmts: Statements,
    *,
    accession: str | None = None,
) -> None:
    """Attach the company profile, the prior-year capital and the filing's accession to *stmts*
    before its metrics are computed (T-151 parts 2 and 3; T-071 part 1's profile). One call so every
    entry point (the pipeline and the recompute script) enriches the same way.

    The sector and sub-industry come from the **company profile** (T-071 part 1, APP-00/APP-04c/PER-14:
    an APP-00 type and a GICS snapshot), falling back to the assets table only for a CIK the profile
    does not hold -- never a guessed type. The two agree on every pilot asset."""
    row = conn.execute("SELECT cik FROM assets WHERE id = ?", (asset_id,)).fetchone()
    cik = str(row["cik"]) if row and row["cik"] else None
    profile = profile_as_of(cik, period_key[:10]) if cik else None
    if profile is not None:
        stmts.sector = profile.sector
        stmts.sub_industry = profile.sub_industry
    else:
        stmts.sector, stmts.sub_industry = company_profile(conn, asset_id)
    stmts.accession = accession
    prior_capital(conn, asset_id, form, period_key, stmts)
