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

import kg_schema

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
    error          TEXT
);
"""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    # No WAL: KG_FINANCIAL_DB may sit on a bind mount with unreliable -shm.
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
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
    conn: sqlite3.Connection, rows: Iterable[CorporateAction], *, engine_version: str
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


def load_universe_asset_ids(conn: sqlite3.Connection, *, universe: str, as_of: str) -> list[int]:
    """Open ``universe_membership`` as of *as_of*; fall back to every ``assets`` row
    when the membership table is empty (mirrors ``cycle.data.active_universe``)."""
    rows = conn.execute(
        """
        SELECT DISTINCT asset_id FROM universe_membership
        WHERE universe = ? AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY asset_id
        """,
        (universe, as_of, as_of),
    ).fetchall()
    if rows:
        return [int(r["asset_id"]) for r in rows]
    return [int(r["id"]) for r in conn.execute("SELECT id FROM assets ORDER BY id")]


def hard_vetoed_as_of(conn: sqlite3.Connection, cutoff_date: str) -> set[int]:
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


def load_assets(conn: sqlite3.Connection, *, universe: str, as_of: str) -> list[tuple[int, str]]:
    """``(asset_id, ticker)`` for the gated universe as of *as_of*."""
    ids = set(load_universe_asset_ids(conn, universe=universe, as_of=as_of))
    return [
        (int(r["id"]), str(r["ticker"]))
        for r in conn.execute("SELECT id, ticker FROM assets ORDER BY ticker")
        if int(r["id"]) in ids
    ]


def load_daily_closes(
    conn: sqlite3.Connection, asset_id: int, *, start: str, end: str
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
    conn: sqlite3.Connection, asset_id: int, action_type: str, *, start: str, end: str
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
    conn: sqlite3.Connection, asset_id: int, rows: Iterable[ReturnRow], *, engine_version: str
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


def load_market_caps(conn: sqlite3.Connection, asset_ids: list[int]) -> dict[int, float]:
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


def insert_risk_model(conn: sqlite3.Connection, meta: RiskModelMeta) -> int:
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
    conn: sqlite3.Connection, model_id: int, mu_by_model: dict[str, dict[int, float]]
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
    conn: sqlite3.Connection,
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


def load_covariance(
    conn: sqlite3.Connection, model_id: int
) -> tuple[list[int], npt.NDArray[np.float64]]:
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
