"""Read helpers over the shared tables -- plain SQL, no imports of the agent packages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portfolio_common.db import Database, Row, in_clause

from kg_schema.env import universe_database_path
from kg_schema.queries import connect_ro, resolve_asset_ids, symbols_asof
from kg_schema.versions import DATA_QUALITY_GATE_VERSION, MetricVersions


def active_universe(
    conn: Database,
    universe: str,
    cycle_date: str,
    universe_db_path: str | Path | None = None,
) -> list[Row]:
    """``(id, ticker, sector_id)`` for the *universe* members as of *cycle_date*,
    read point-in-time from ``universe.db`` and mapped by ticker.

    Raises ``RuntimeError`` when ``universe.db`` has no members as of *cycle_date*
    or none of them exist in ``assets`` yet (run the agents for this date first)."""
    upath = universe_database_path(str(universe_db_path) if universe_db_path else None)
    uconn = connect_ro(upath)
    try:
        syms = symbols_asof(uconn, cycle_date, universe=universe)
    finally:
        uconn.close()
    if not syms:
        raise RuntimeError(f"universe.db ({upath}) has no {universe} members as of {cycle_date}")
    mapping, _missing = resolve_asset_ids(conn, syms)
    if not mapping:
        raise RuntimeError(
            f"none of the {len(syms)} {universe} members as of {cycle_date} exist in assets yet "
            f"-- run fundamental_agent / pricing_agent for this date first"
        )
    ids = sorted(mapping.values())
    return list(
        conn.execute(
            f"SELECT id, ticker, sector_id FROM assets WHERE id IN {in_clause(ids)} "  # noqa: S608
            "ORDER BY ticker",
            ids,
        )
    )


def latest_metrics(
    conn: Database, cycle_date: str, versions: MetricVersions
) -> dict[int, dict[str, float | None]]:
    """asset_id -> {"group.name": value} from each asset's newest filing *usable* on
    *cycle_date* (``available_at <= cycle_date``, the first trading day after its filing date,
    T-106/T-107 -- never one whose period merely ended by then, nor one filed that very day),
    read from the metrics engine *versions* the run resolved (T-090) -- never every version. A
    filing with no ``filing_date`` has no ``available_at`` and is skipped."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, m.metric_group, m.metric_name, m.value
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
        """,
        (cycle_date, versions.json_param()),
    ).fetchall()
    out: dict[int, dict[str, float | None]] = {}
    for r in rows:
        out.setdefault(int(r["asset_id"]), {})[f"{r['metric_group']}.{r['metric_name']}"] = r[
            "value"
        ]
    return out


@dataclass
class DataQuality:
    """What the Ring-1 gates (T-065) decided about each asset's latest filing, for the
    metric versions this run reads."""

    # asset_id -> {"group.name"} read as NULL
    quarantined: dict[int, set[str]] = field(default_factory=dict)
    # asset_id -> the HARD issues (the DATA_QUALITY veto's evidence)
    hard: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    # assets whose latest filing reports book equity <= 0 (DQ_NEG_EQUITY)
    negative_equity: set[int] = field(default_factory=set)

    def apply(
        self, metrics: dict[int, dict[str, float | None]]
    ) -> dict[int, dict[str, float | None]]:
        """*metrics* with every quarantined value replaced by None (the input is untouched)."""
        out: dict[int, dict[str, float | None]] = {}
        for aid, values in metrics.items():
            q = self.quarantined.get(aid, set())
            out[aid] = {k: (None if k in q else v) for k, v in values.items()}
        return out


def data_quality(conn: Database, cycle_date: str, versions: MetricVersions) -> DataQuality:
    """The quarantines and HARD issues recorded against each asset's latest *usable* filing
    (the same filing :func:`latest_metrics` reads, T-106/T-107), for the metric *versions* the run resolved and the
    current gate version only. A SOFT, unquarantined issue is a review item and is skipped."""
    rows = conn.execute(
        """
        WITH known AS (
            SELECT f.id, ROW_NUMBER() OVER (
                PARTITION BY f.asset_id ORDER BY f.period_end DESC, f.available_at DESC, f.id DESC
            ) AS rn
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.available_at IS NOT NULL AND f.available_at <= ?
        )
        SELECT f.asset_id, d.metric_group, d.metric_name, d.rule_id, d.severity,
               d.quarantined, d.value
        FROM data_quality_issue d
        JOIN sec_filings f ON f.id = d.filing_id
        JOIN known k ON k.id = f.id AND k.rn = 1
        WHERE (d.metric_group || '/' || d.metric_engine_version) IN (SELECT value FROM json_each(?))
          AND d.gate_version = ?
          AND (d.quarantined = 1 OR d.severity = 'HARD')
        ORDER BY f.asset_id, d.rule_id, d.metric_group, d.metric_name
        """,
        (cycle_date, versions.json_param(), DATA_QUALITY_GATE_VERSION),
    ).fetchall()
    out = DataQuality()
    for r in rows:
        aid = int(r["asset_id"])
        key = f"{r['metric_group']}.{r['metric_name']}"
        if r["quarantined"]:
            out.quarantined.setdefault(aid, set()).add(key)
        if r["rule_id"] == "DQ_NEG_EQUITY":
            out.negative_equity.add(aid)
        if r["severity"] == "HARD":
            out.hard.setdefault(aid, []).append(
                {"rule_id": r["rule_id"], "metric": key, "value": r["value"]}
            )
    return out


def latest_price_observation(conn: Database, cycle_date: str) -> dict[int, dict[str, float | None]]:
    rows = conn.execute(
        """
        SELECT p.*
        FROM price_observation p
        JOIN (
            SELECT asset_id, MAX(obs_date) AS d FROM price_observation
            WHERE obs_date <= ? GROUP BY asset_id
        ) last ON last.asset_id = p.asset_id AND last.d = p.obs_date
        """,
        (cycle_date,),
    ).fetchall()
    return {int(r["asset_id"]): dict(r) for r in rows}


def latest_fundamental_rows(conn: Database, cycle_date: str) -> list[Row]:
    """Each asset's newest FUNDAMENTAL snapshot usable on *cycle_date*
    (``id, asset_id, event_time, raw_value, normalized_score``). A FUNDAMENTAL row's
    ``event_time`` is its period end -- what the score is *about* -- not when it became known;
    that is its ``available_at``, the first trading day after its filing's date (T-107)."""
    return conn.execute(
        """
        WITH known AS (
            SELECT s.id, s.asset_id, s.event_time, s.raw_value, s.normalized_score,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.asset_id ORDER BY s.event_time DESC, s.id DESC
                   ) AS rn
            FROM score_snapshot s
            WHERE s.score_type = 'FUNDAMENTAL'
              AND s.available_at IS NOT NULL AND s.available_at <= ?
        )
        SELECT id, asset_id, event_time, raw_value, normalized_score FROM known WHERE rn = 1
        ORDER BY asset_id
        """,
        (cycle_date,),
    ).fetchall()


def last_fundamental_dates(conn: Database, cycle_date: str) -> dict[int, str | None]:
    """asset_id -> the period end of its newest FUNDAMENTAL score usable on *cycle_date*."""
    return {int(r["asset_id"]): r["event_time"] for r in latest_fundamental_rows(conn, cycle_date)}


def latest_fundamental_score(conn: Database, cycle_date: str) -> dict[int, float | None]:
    return {int(r["asset_id"]): r["raw_value"] for r in latest_fundamental_rows(conn, cycle_date)}


def latest_semantic_score(conn: Database, cycle_date: str) -> dict[int, float | None]:
    """Pre-existing SEMANTIC scores (written by the integration repo), if any."""
    rows = conn.execute(
        """
        SELECT s.asset_id, s.raw_value
        FROM score_snapshot s
        JOIN (
            SELECT asset_id, MAX(event_time) AS et FROM score_snapshot
            WHERE score_type = 'SEMANTIC' AND event_time <= ? GROUP BY asset_id
        ) last ON last.asset_id = s.asset_id AND last.et = s.event_time
        WHERE s.score_type = 'SEMANTIC'
        """,
        (cycle_date,),
    ).fetchall()
    return {int(r["asset_id"]): r["raw_value"] for r in rows}


def market_cap_estimates(
    conn: Database,
    cycle_date: str,
    metrics: dict[int, dict[str, float | None]],
    versions: MetricVersions,
) -> dict[int, float | None]:
    """Read market cap straight off the stored valuation metric inputs of the resolved
    *versions* (T-090), when present; the most recent filing per asset *usable on
    cycle_date* wins (T-106/T-107 -- a 2023 cycle must not read a 2026 market cap)."""
    rows = conn.execute(
        """
        SELECT f.asset_id, m.inputs_json
        FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id
        WHERE m.metric_group = 'valuation' AND m.metric_name = 'market_capitalization'
          AND (m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))
          AND m.available_at IS NOT NULL AND m.available_at <= ?
        ORDER BY f.period_end, m.available_at, f.id
        """,
        (versions.json_param(), cycle_date),
    ).fetchall()
    out: dict[int, float | None] = {}
    for r in rows:
        aid = int(r["asset_id"])
        try:
            payload = json.loads(r["inputs_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        out[aid] = payload.get("market_capitalization") or payload.get("value")
    for aid in metrics:
        out.setdefault(aid, None)
    return out
