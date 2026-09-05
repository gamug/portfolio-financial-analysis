"""SQLite persistence for ``quant``.

Writes into the shared ``KG_FINANCIAL_DB`` and owns the ``quant_*`` tables;
``corporate_action`` / ``quant_return_daily`` / ``risk_free_rate`` /
``benchmark_series`` are additive shared tables created by :func:`kg_schema.ensure`.
Reads of other packages' tables are plain ``SELECT``s -- no ``cycle`` import.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import numpy.typing as npt
from portfolio_common.db import Database, in_clause

import kg_schema
from kg_schema.env import universe_database_path
from kg_schema.queries import connect_ro, resolve_asset_ids, symbols_asof

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
    except sqlite3.OperationalError:
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


def load_actions(
    conn: Database, asset_id: int, action_type: str, *, start: str, end: str
) -> dict[str, float]:
    """``ex_date -> value`` from ``v_corporate_action`` (latest engine per ex-date)."""
    return {
        str(r["ex_date"]): float(r["value"])
        for r in conn.execute(
            "SELECT ex_date, value FROM v_corporate_action "
            "WHERE asset_id = ? AND action_type = ? AND ex_date >= ? AND ex_date <= ?",
            (asset_id, action_type, start, end),
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


def load_market_caps(conn: Database, asset_ids: list[int]) -> dict[int, float]:
    """``asset_id -> market cap`` parsed from ``fundamental_metrics.inputs_json``
    (latest per asset); mirrors ``cycle.data.market_cap_estimates``. Missing -> absent."""
    out: dict[int, float] = {}
    try:
        rows = conn.execute(
            "SELECT sf.asset_id, fm.value, fm.inputs_json "
            "FROM fundamental_metrics fm JOIN sec_filings sf ON sf.id = fm.filing_id "
            "WHERE fm.metric_group = 'valuation' AND fm.metric_name = 'market_capitalization' "
            "ORDER BY sf.period_end"
        ).fetchall()
    except sqlite3.OperationalError:
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


def insert_risk_model(conn: Database, meta: RiskModelMeta) -> int:
    conn.execute(
        """
        INSERT INTO quant_risk_model
            (quant_run_id, as_of, model_version, lookback_days, min_history_days, n_assets,
             cov_estimator, cov_shrinkage, ret_estimator, periods_per_year, panel_engine_version,
             panel_spec_json, rf_annual, computed_at, params_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of, model_version) DO UPDATE SET
            quant_run_id = excluded.quant_run_id, lookback_days = excluded.lookback_days,
            min_history_days = excluded.min_history_days, n_assets = excluded.n_assets,
            cov_estimator = excluded.cov_estimator, cov_shrinkage = excluded.cov_shrinkage,
            ret_estimator = excluded.ret_estimator, periods_per_year = excluded.periods_per_year,
            panel_engine_version = excluded.panel_engine_version,
            panel_spec_json = excluded.panel_spec_json, rf_annual = excluded.rf_annual,
            computed_at = excluded.computed_at, params_json = excluded.params_json
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


def load_risk_model(conn: Database, *, as_of: str, model_version: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
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


def insert_portfolio(conn: Database, row: PortfolioRow) -> int:
    conn.execute(
        """
        INSERT INTO quant_portfolio
            (quant_run_id, model_id, as_of, kind, frontier_k, objective, solver, status,
             expected_return, expected_vol, sharpe, rf_annual, n_positions, turnover,
             target_param, engine_version, computed_at, params_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (as_of, kind, frontier_k, engine_version) DO UPDATE SET
            quant_run_id = excluded.quant_run_id, model_id = excluded.model_id,
            objective = excluded.objective, solver = excluded.solver, status = excluded.status,
            expected_return = excluded.expected_return, expected_vol = excluded.expected_vol,
            sharpe = excluded.sharpe, rf_annual = excluded.rf_annual,
            n_positions = excluded.n_positions, turnover = excluded.turnover,
            target_param = excluded.target_param, computed_at = excluded.computed_at,
            params_json = excluded.params_json
        """,
        (
            row.quant_run_id,
            row.model_id,
            row.as_of,
            row.kind,
            row.frontier_k,
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
            row.engine_version,
            _now(),
            row.params_json,
        ),
    )
    conn.commit()
    fk = row.frontier_k
    got = conn.execute(
        "SELECT id FROM quant_portfolio WHERE as_of = ? AND kind = ? AND engine_version = ? "
        "AND frontier_k IS ?",
        (row.as_of, row.kind, row.engine_version, fk),
    ).fetchone()
    return int(got["id"])


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
    except sqlite3.OperationalError:
        return {}
    return {int(r["asset_id"]): float(r["weight"]) for r in rows}


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
