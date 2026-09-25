"""SQLite persistence for ``quant``.

Writes into the shared ``KG_FINANCIAL_DB`` and owns the ``quant_*`` tables;
``corporate_action`` / ``quant_return_daily`` / ``risk_free_rate`` /
``benchmark_series`` are additive shared tables created by :func:`kg_schema.ensure`.
Reads of other packages' tables are plain ``SELECT``s -- no ``cycle`` import.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
from portfolio_common.db import Database, DatabaseError, Row, in_clause

import kg_schema
from kg_schema.env import universe_database_path
from kg_schema.queries import connect_ro, resolve_asset_ids, symbols_asof
from kg_schema.versions import MetricVersions

_ZERO_W = 1e-9  # weights this small are treated as "no position"
_WEIGHT_CHANGE = 1e-12  # a weight delta smaller than this is a no-op

# quant_run is quant-private (no read-contract view). The quant_* tables that DO
# get a v_quant_* projection live in kg_schema.ADDITIVE_DDL, so kg_schema.ensure
# creates them before it (re)builds the views.
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
    error          TEXT,
    code_version   TEXT                          -- code tag that produced the run
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def ensure_schema(conn: Database) -> None:
    conn.create_schema(SCHEMA)
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
    """One ``backfill-actions`` run. The gateway is the only source (T-085), so an
    asset either has its rows fetched or appears in ``errors`` with none written."""

    engine_version: str
    assets_seen: int = 0
    assets_fetched: int = 0
    dividends: int = 0
    splits: int = 0
    inserted: int = 0
    errors: list[str] = field(default_factory=list)


def upsert_corporate_actions(
    conn: Database, rows: Iterable[CorporateAction], *, engine_version: str
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


def load_universe_asset_ids(
    conn: Database,
    *,
    universe: str,
    as_of: str,
    universe_db_path: str | Path | None = None,
) -> list[int]:
    """``assets.id`` for the *universe* members as of *as_of*, read point-in-time
    from ``universe.db`` and mapped by ticker.

    Raises ``RuntimeError`` if ``universe.db`` has no members as of *as_of*, or if
    none of them have an ``assets`` row yet (run ``fundamental_agent`` /
    ``pricing_agent`` first). *universe_db_path* overrides the resolved path -- the
    test seam."""
    upath = universe_database_path(str(universe_db_path) if universe_db_path else None)
    uconn = connect_ro(upath)
    try:
        syms = symbols_asof(uconn, as_of, universe=universe)
    finally:
        uconn.close()
    if not syms:
        raise RuntimeError(f"universe.db ({upath}) has no {universe} members as of {as_of}")
    mapping, _missing = resolve_asset_ids(conn, syms)
    if not mapping:
        raise RuntimeError(
            f"none of the {len(syms)} {universe} members as of {as_of} exist in assets yet "
            f"-- run fundamental_agent / pricing_agent for this date first"
        )
    return sorted(mapping.values())


def hard_vetoed_as_of(conn: Database, cutoff_date: str) -> set[int]:
    """Assets with an uncleared HARD ``veto`` row dated on/before *cutoff_date*
    (copied from ``cycle.writers`` to avoid importing ``cycle``)."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT asset_id FROM veto "
            "WHERE severity = 'HARD' AND cleared_at IS NULL AND cycle_date <= ?",
            (cutoff_date,),
        ).fetchall()
    except DatabaseError:
        return set()  # no veto table in this DB
    return {int(r["asset_id"]) for r in rows}


def load_assets(
    conn: Database,
    *,
    universe: str,
    as_of: str,
    universe_db_path: str | Path | None = None,
) -> list[tuple[int, str]]:
    """``(asset_id, ticker)`` for the gated universe as of *as_of*."""
    ids = set(
        load_universe_asset_ids(
            conn, universe=universe, as_of=as_of, universe_db_path=universe_db_path
        )
    )
    return [
        (int(r["id"]), str(r["ticker"]))
        for r in conn.execute("SELECT id, ticker FROM assets ORDER BY ticker")
        if int(r["id"]) in ids
    ]


def load_daily_closes(
    conn: Database, asset_id: int, *, start: str, end: str
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


# The gateway is the only corporate-actions source (T-085), so only its engines are
# read; if a newer gateway engine ever coexists with an older one for the same asset +
# action_type, the newer wins outright. Rows written before T-085 under the
# XBRL-derived engines ('corpact-v0-approx', 'corpact-v1-derived') stay in the
# append-only table as history but are deliberately NOT in this tuple -- they are
# never read. Deliberately NOT ``v_corporate_action``'s per-(asset, action_type,
# ex_date) "most recently ingested" resolution either: two engines' ex-dates rarely
# collide, so that view would return BOTH engines' rows side by side.
_ACTION_ENGINE_PRIORITY = ("corpact-v2", "corpact-v1")


def has_gateway_actions(conn: Database, engines: Sequence[str] | None = None) -> bool:
    """True when ``corporate_action`` holds at least one row from a gateway engine
    (*engines*, default ``_ACTION_ENGINE_PRIORITY``)."""
    engines = tuple(engines or _ACTION_ENGINE_PRIORITY)
    marks = ", ".join("?" for _ in engines)
    row = conn.execute(
        f"SELECT 1 FROM corporate_action WHERE engine_version IN ({marks}) LIMIT 1",  # noqa: S608
        engines,
    ).fetchone()
    return row is not None


def load_actions(  # noqa: PLR0913 - the lookup key plus the window and the engine choice
    conn: Database,
    asset_id: int,
    action_type: str,
    *,
    start: str,
    end: str,
    engines: Sequence[str] | None = None,
) -> dict[str, float]:
    """``ex_date -> value`` in ``[start, end]``, from whichever single
    engine_version is the best available for *asset_id* among *engines* (default
    ``_ACTION_ENGINE_PRIORITY``, newest first; a ``--corpact-version`` pins one, T-093)
    -- never a blend of two engines' ex-dates."""
    priority = tuple(engines or _ACTION_ENGINE_PRIORITY)
    available = {
        str(r["engine_version"])
        for r in conn.execute(
            "SELECT DISTINCT engine_version FROM corporate_action "
            "WHERE asset_id = ? AND action_type = ?",
            (asset_id, action_type),
        )
    }
    engine = next((e for e in priority if e in available), None)
    if engine is None:
        return {}
    return {
        str(r["ex_date"]): float(r["value"])
        for r in conn.execute(
            "SELECT ex_date, value FROM corporate_action "
            "WHERE asset_id = ? AND action_type = ? AND engine_version = ? "
            "AND ex_date >= ? AND ex_date <= ?",
            (asset_id, action_type, engine, start, end),
        )
    }


# -- total-return daily series ----------------------------------------------


@dataclass(frozen=True)
class ReturnRow:
    """One day of the total-return series. ``price_log_return`` / ``tr_log_return``
    are ``None`` on an asset's first row."""

    obs_date: str
    close_split_adj: float
    adj_close: float
    tr_index: float
    cash_dividend: float
    split_factor: float
    price_log_return: float | None
    tr_log_return: float | None


def upsert_return_daily(
    conn: Database, asset_id: int, rows: Iterable[ReturnRow], *, engine_version: str
) -> int:
    """``INSERT OR IGNORE`` on ``(asset_id, obs_date, engine_version)``."""
    now = _now()
    inserted = 0
    for r in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO quant_return_daily
                (asset_id, obs_date, close_split_adj, adj_close, tr_index, cash_dividend,
                 split_factor, price_log_return, tr_log_return, source, engine_version, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'quant-tr-v1', ?, ?)
            """,
            (
                asset_id,
                r.obs_date,
                r.close_split_adj,
                r.adj_close,
                r.tr_index,
                r.cash_dividend,
                r.split_factor,
                r.price_log_return,
                r.tr_log_return,
                engine_version,
                now,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def load_market_caps(
    conn: Database, asset_ids: list[int], versions: MetricVersions, *, as_of: str
) -> dict[int, float]:
    """``asset_id -> market cap`` parsed from ``fundamental_metrics.inputs_json``
    (latest per asset whose filing was public by *as_of*, T-106) of the *versions* the run
    resolved (T-090); mirrors ``cycle.data.market_cap_estimates``. Missing -> absent."""
    out: dict[int, float] = {}
    try:
        rows = conn.execute(
            """
            SELECT sf.asset_id, m.value, m.inputs_json
            FROM fundamental_metrics m JOIN sec_filings sf ON sf.id = m.filing_id
            WHERE m.metric_group = 'valuation' AND m.metric_name = 'market_capitalization'
              AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
              AND sf.filing_date IS NOT NULL AND sf.filing_date <= ?
            ORDER BY sf.period_end, sf.filing_date, sf.id
            """,
            (versions.json_param(), as_of),
        ).fetchall()
    except DatabaseError:
        return out
    wanted = set(asset_ids)
    for r in rows:
        aid = int(r["asset_id"])
        cap = r["value"]
        if cap is None and r["inputs_json"]:
            try:
                cap = json.loads(r["inputs_json"]).get("market_capitalization")
            except (ValueError, TypeError):
                cap = None
        if cap is not None and aid in wanted:
            out[aid] = float(cap)  # ORDER BY period_end => last wins = most recent
    return out


# -- risk model -------------------------------------------------------------


@dataclass
class RiskModelMeta:
    as_of: str
    model_version: str
    lookback_days: int
    min_history_days: int
    n_assets: int
    cov_estimator: str
    cov_shrinkage: float | None
    ret_estimator: str
    periods_per_year: int
    panel_engine_version: str
    panel_spec_json: str
    rf_annual: float | None
    params_json: str
    quant_run_id: int | None = None
    manifest_json: str | None = None


def insert_risk_model(conn: Database, meta: RiskModelMeta) -> int:
    conn.execute(
        """
        INSERT INTO quant_risk_model
            (quant_run_id, as_of, model_version, lookback_days, min_history_days, n_assets,
             cov_estimator, cov_shrinkage, ret_estimator, periods_per_year, panel_engine_version,
             panel_spec_json, rf_annual, computed_at, params_json, manifest_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of, model_version) DO UPDATE SET
            quant_run_id = excluded.quant_run_id, lookback_days = excluded.lookback_days,
            min_history_days = excluded.min_history_days, n_assets = excluded.n_assets,
            cov_estimator = excluded.cov_estimator, cov_shrinkage = excluded.cov_shrinkage,
            ret_estimator = excluded.ret_estimator, periods_per_year = excluded.periods_per_year,
            panel_engine_version = excluded.panel_engine_version,
            panel_spec_json = excluded.panel_spec_json, rf_annual = excluded.rf_annual,
            computed_at = excluded.computed_at, params_json = excluded.params_json,
            manifest_json = excluded.manifest_json
        """,
        (
            meta.quant_run_id,
            meta.as_of,
            meta.model_version,
            meta.lookback_days,
            meta.min_history_days,
            meta.n_assets,
            meta.cov_estimator,
            meta.cov_shrinkage,
            meta.ret_estimator,
            meta.periods_per_year,
            meta.panel_engine_version,
            meta.panel_spec_json,
            meta.rf_annual,
            _now(),
            meta.params_json,
            meta.manifest_json,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM quant_risk_model WHERE as_of = ? AND model_version = ?",
        (meta.as_of, meta.model_version),
    ).fetchone()
    return int(row["id"])


def insert_expected_returns(
    conn: Database, model_id: int, mu_by_model: dict[str, dict[int, float]]
) -> int:
    n = 0
    for mu_model, by_asset in mu_by_model.items():
        for asset_id, mu in by_asset.items():
            conn.execute(
                "INSERT INTO quant_expected_return (model_id, asset_id, mu_model, mu) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (model_id, asset_id, mu_model) "
                "DO UPDATE SET mu = excluded.mu",
                (model_id, asset_id, mu_model, mu),
            )
            n += 1
    conn.commit()
    return n


def insert_covariance(
    conn: Database,
    model_id: int,
    asset_ids: list[int],
    sigma: npt.NDArray[np.float64],
) -> int:
    """Store the lower triangle (``i <= j``) of the annualized covariance."""
    n = 0
    for i, ai in enumerate(asset_ids):
        for j in range(i, len(asset_ids)):
            conn.execute(
                "INSERT INTO quant_covariance (model_id, asset_id_i, asset_id_j, value) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (model_id, asset_id_i, asset_id_j) "
                "DO UPDATE SET value = excluded.value",
                (model_id, ai, asset_ids[j], float(sigma[i, j])),
            )
            n += 1
    conn.commit()
    return n


def load_covariance(conn: Database, model_id: int) -> tuple[list[int], npt.NDArray[np.float64]]:
    rows = conn.execute(
        "SELECT asset_id_i, asset_id_j, value FROM quant_covariance WHERE model_id = ?",
        (model_id,),
    ).fetchall()
    ids = sorted({int(r["asset_id_i"]) for r in rows} | {int(r["asset_id_j"]) for r in rows})
    ix = {a: k for k, a in enumerate(ids)}
    m = np.zeros((len(ids), len(ids)), dtype=np.float64)
    for r in rows:
        i, j = ix[int(r["asset_id_i"])], ix[int(r["asset_id_j"])]
        m[i, j] = m[j, i] = float(r["value"])
    return ids, m


def load_risk_model(conn: Database, *, as_of: str, model_version: str) -> Row | None:
    row: Row | None = conn.execute(
        "SELECT * FROM quant_risk_model WHERE as_of = ? AND model_version = ?",
        (as_of, model_version),
    ).fetchone()
    return row


def load_expected_returns(conn: Database, model_id: int, mu_model: str) -> dict[int, float]:
    return {
        int(r["asset_id"]): float(r["mu"])
        for r in conn.execute(
            "SELECT asset_id, mu FROM quant_expected_return WHERE model_id = ? AND mu_model = ?",
            (model_id, mu_model),
        )
    }


def load_sector_of(conn: Database, asset_ids: list[int]) -> dict[int, int | None]:
    q = f"SELECT id, sector_id FROM assets WHERE id IN {in_clause(asset_ids)}"  # noqa: S608
    return {
        int(r["id"]): (int(r["sector_id"]) if r["sector_id"] is not None else None)
        for r in conn.execute(q, asset_ids)
    }


# -- optimized books --------------------------------------------------------


@dataclass
class PortfolioRow:
    as_of: str
    kind: str
    objective: str
    solver: str
    status: str
    expected_return: float | None
    expected_vol: float | None
    sharpe: float | None
    rf_annual: float | None
    n_positions: int
    engine_version: str
    frontier_k: int | None = None
    turnover: float | None = None
    target_param: float | None = None
    model_id: int | None = None
    quant_run_id: int | None = None
    params_json: str | None = None
    manifest_json: str | None = None


def insert_portfolio(conn: Database, row: PortfolioRow) -> int:
    """Write *row* and return its id: update the book already stored under the same key,
    else insert a new one.

    The key is ``(as_of, kind, frontier_k, engine_version)``, but ``frontier_k`` is NULL for
    every non-frontier book, and SQL NULLs never compare equal -- so the table's own
    ``UNIQUE``/``ON CONFLICT`` never matched and every identical re-run inserted another copy of
    each book (T-101). The lookup therefore matches ``frontier_k IS ?``, which treats NULL as a
    value; migration m007 adds the equivalent NULL-safe unique index. Should a database still
    hold duplicates from before that migration, the oldest is the one updated -- it is the row
    the old read-back returned, so it holds the book's positions."""
    values = (
        row.quant_run_id,
        row.model_id,
        row.objective,
        row.solver,
        row.status,
        row.expected_return,
        row.expected_vol,
        row.sharpe,
        row.rf_annual,
        row.n_positions,
        row.turnover,
        row.target_param,
        _now(),
        row.params_json,
        row.manifest_json,
    )
    existing = conn.execute(
        "SELECT id FROM quant_portfolio WHERE as_of = ? AND kind = ? AND engine_version = ? "
        "AND frontier_k IS ? ORDER BY id LIMIT 1",
        (row.as_of, row.kind, row.engine_version, row.frontier_k),
    ).fetchone()
    if existing is not None:
        pid = int(existing["id"])
        conn.execute(
            """
            UPDATE quant_portfolio SET
                quant_run_id = ?, model_id = ?, objective = ?, solver = ?, status = ?,
                expected_return = ?, expected_vol = ?, sharpe = ?, rf_annual = ?,
                n_positions = ?, turnover = ?, target_param = ?, computed_at = ?,
                params_json = ?, manifest_json = ?
            WHERE id = ?
            """,
            (*values, pid),
        )
    else:
        pid = int(
            conn.execute(
                """
                INSERT INTO quant_portfolio
                    (quant_run_id, model_id, objective, solver, status, expected_return,
                     expected_vol, sharpe, rf_annual, n_positions, turnover, target_param,
                     computed_at, params_json, manifest_json,
                     as_of, kind, frontier_k, engine_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (*values, row.as_of, row.kind, row.frontier_k, row.engine_version),
            ).fetchone()["id"]
        )
    conn.commit()
    return pid


def sync_positions(
    conn: Database, portfolio_id: int, as_of: str, weights: dict[int, float]
) -> tuple[int, int]:
    """Open new stints, close vanished ones, update changed weights -- history is
    immutable, mirroring ``cycle.writers.sync_positions``."""
    open_rows = {
        int(r["asset_id"]): (r["id"], float(r["weight"]))
        for r in conn.execute(
            "SELECT id, asset_id, weight FROM quant_position "
            "WHERE portfolio_id = ? AND valid_to IS NULL",
            (portfolio_id,),
        )
    }
    target = {a: w for a, w in weights.items() if abs(w) > _ZERO_W}
    opened = closed = 0
    for aid in set(open_rows) - set(target):
        conn.execute(
            "UPDATE quant_position SET valid_to = ? WHERE id = ?", (as_of, open_rows[aid][0])
        )
        closed += 1
    for aid, w in target.items():
        if aid in open_rows:
            if abs(open_rows[aid][1] - w) > _WEIGHT_CHANGE:
                conn.execute(
                    "UPDATE quant_position SET weight = ? WHERE id = ?", (w, open_rows[aid][0])
                )
        else:
            conn.execute(
                "INSERT INTO quant_position (portfolio_id, asset_id, weight, valid_from) "
                "VALUES (?, ?, ?, ?)",
                (portfolio_id, aid, w, as_of),
            )
            opened += 1
    conn.commit()
    return opened, closed


def load_book_weights(conn: Database, portfolio_id: int) -> dict[int, float]:
    return {
        int(r["asset_id"]): float(r["weight"])
        for r in conn.execute(
            "SELECT asset_id, weight FROM quant_position "
            "WHERE portfolio_id = ? AND valid_to IS NULL",
            (portfolio_id,),
        )
    }


def load_live_book(conn: Database, as_of: str) -> dict[int, float]:
    """The open ``portfolio_position`` book as of *as_of* (the cycle's live book)."""
    try:
        rows = conn.execute(
            "SELECT asset_id, weight FROM portfolio_position "
            "WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND weight IS NOT NULL",
            (as_of, as_of),
        ).fetchall()
    except DatabaseError:
        return {}
    return {int(r["asset_id"]): float(r["weight"]) for r in rows}


def earliest_portfolio_as_of(conn: Database) -> str | None:
    """The earliest ``as_of`` across every persisted ``quant_portfolio`` book, or
    ``None`` if none exist yet -- ``evaluate``'s default ``--from`` when omitted
    (Q2, docs/model_fixes.md): starting from the earliest book maximizes the
    forward window actually available, rather than a caller guessing a date
    that happens to coincide with the last date ``price_daily`` has data for."""
    row = conn.execute("SELECT MIN(as_of) AS as_of FROM quant_portfolio").fetchone()
    return str(row["as_of"]) if row and row["as_of"] is not None else None


def load_forward_simple_returns(
    conn: Database, asset_ids: list[int], *, after: str, until: str, engine_version: str
) -> dict[str, dict[int, float]]:
    """``date -> {asset_id: simple return}`` for trading days in ``(after, until]``."""
    if not asset_ids:
        return {}
    base = (
        "SELECT asset_id, obs_date, tr_log_return FROM quant_return_daily "
        "WHERE engine_version = ? AND obs_date > ? AND obs_date <= ? "
        "AND tr_log_return IS NOT NULL AND asset_id IN "
    )
    q = base + in_clause(asset_ids)
    out: dict[str, dict[int, float]] = {}
    for r in conn.execute(q, [engine_version, after, until, *asset_ids]):
        out.setdefault(str(r["obs_date"]), {})[int(r["asset_id"])] = float(
            np.expm1(float(r["tr_log_return"]))
        )
    return out


def upsert_benchmark_series(
    conn: Database,
    benchmark: str,
    rows: list[tuple[str, float, float]],
    *,
    engine_version: str,
    source: str,
) -> int:
    """*rows*: (obs_date, log_return, total_return_level)."""
    now = _now()
    n = 0
    for obs_date, lr, trl in rows:
        conn.execute(
            "INSERT INTO benchmark_series "
            "(benchmark, obs_date, level, total_return_level, log_return, source, "
            " engine_version, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (benchmark, obs_date, engine_version) DO UPDATE SET "
            "total_return_level = excluded.total_return_level, log_return = excluded.log_return, "
            "ingested_at = excluded.ingested_at",
            (benchmark, obs_date, trl, trl, lr, source, engine_version, now),
        )
        n += 1
    conn.commit()
    return n


def load_benchmark_returns(
    conn: Database, benchmark: str, *, after: str, until: str
) -> dict[str, float]:
    return {
        str(r["obs_date"]): float(np.expm1(float(r["log_return"])))
        for r in conn.execute(
            "SELECT obs_date, log_return FROM v_benchmark_series "
            "WHERE benchmark = ? AND obs_date > ? AND obs_date <= ? AND log_return IS NOT NULL",
            (benchmark, after, until),
        )
    }


def upsert_benchmark_performance(
    conn: Database,
    portfolio_id: int,
    rows: list[tuple[str, float, float, str | None, float | None, float | None]],
    *,
    engine_version: str,
) -> int:
    """*rows*: (date, realized_return, cumulative_return, benchmark, benchmark_return,
    active_return)."""
    now = _now()
    n = 0
    for d, rr, cr, bench, br, ar in rows:
        conn.execute(
            "INSERT INTO quant_benchmark_performance "
            "(portfolio_id, date, realized_return, cumulative_return, benchmark, "
            " benchmark_return, active_return, engine_version, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (portfolio_id, date, engine_version) DO UPDATE SET "
            "realized_return = excluded.realized_return, "
            "cumulative_return = excluded.cumulative_return, "
            "benchmark_return = excluded.benchmark_return, active_return = excluded.active_return, "
            "computed_at = excluded.computed_at",
            (portfolio_id, d, rr, cr, bench, br, ar, engine_version, now),
        )
        n += 1
    conn.commit()
    return n


def insert_frontier_points(
    conn: Database,
    model_id: int,
    points: list[tuple[int, float, float, float, float | None, str, str]],
) -> int:
    """*points* rows: (k, target_return, expected_return, expected_vol, sharpe, status,
    weights_json). Rows with a higher ``k`` from a previous, larger sweep are dropped."""
    conn.execute(
        "DELETE FROM quant_frontier_point WHERE model_id = ? AND k >= ?",
        (model_id, len(points)),
    )
    n = 0
    for k, tgt, ret, vol, sharpe, status, wjson in points:
        conn.execute(
            "INSERT INTO quant_frontier_point "
            "(model_id, k, target_return, expected_return, expected_vol, sharpe, status, "
            " weights_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (model_id, k) DO UPDATE SET target_return = excluded.target_return, "
            "expected_return = excluded.expected_return, expected_vol = excluded.expected_vol, "
            "sharpe = excluded.sharpe, status = excluded.status, weights_json = excluded.weights_json",
            (model_id, k, tgt, ret, vol, sharpe, status, wjson),
        )
        n += 1
    conn.commit()
    return n


# -- stored input versions (T-093) ------------------------------------------------------------
#
# One fully literal statement per input, keyed by the input's name: a table name is an
# identifier no ``?`` can bind, and it is never interpolated (constitution Code & Git #10).
_VERSION_STATS_SQL: dict[str, str] = {
    "returns": (
        "SELECT engine_version AS version, COUNT(*) AS n_rows, MIN(computed_at) AS first_at, "
        "MAX(computed_at) AS last_at FROM quant_return_daily GROUP BY engine_version"
    ),
    "corpact": (
        "SELECT engine_version AS version, COUNT(*) AS n_rows, MIN(ingested_at) AS first_at, "
        "MAX(ingested_at) AS last_at FROM corporate_action GROUP BY engine_version"
    ),
    "risk_model": (
        "SELECT model_version AS version, COUNT(*) AS n_rows, MIN(computed_at) AS first_at, "
        "MAX(computed_at) AS last_at FROM quant_risk_model GROUP BY model_version"
    ),
}


@dataclass(frozen=True)
class VersionStat:
    """One stored version of an input: how many rows, and when they were first/last written."""

    version: str
    n_rows: int
    first_at: str | None
    last_at: str | None


def version_stats(conn: Database, input_name: str) -> list[VersionStat]:
    """What is stored for *input_name*: ``returns``, ``corpact``, or ``risk_model`` (full
    ``model_version`` strings, tag included). Empty when the table does not exist yet. The
    metrics listing is :func:`kg_schema.queries.metric_version_stats` -- the one place allowed
    to read ``fundamental_metrics`` across every version."""
    try:
        rows = conn.execute(_VERSION_STATS_SQL[input_name]).fetchall()
    except DatabaseError:
        return []
    return [
        VersionStat(
            str(r["version"]),
            int(r["n_rows"]),
            None if r["first_at"] is None else str(r["first_at"]),
            None if r["last_at"] is None else str(r["last_at"]),
        )
        for r in rows
    ]


def risk_model_versions_at(conn: Database, as_of: str) -> list[str]:
    """Every ``quant_risk_model.model_version`` stored for *as_of*."""
    try:
        return [
            str(r["model_version"])
            for r in conn.execute(
                "SELECT model_version FROM quant_risk_model WHERE as_of = ?", (as_of,)
            )
        ]
    except DatabaseError:
        return []
