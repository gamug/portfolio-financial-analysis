"""Read helpers over the shared tables -- plain SQL, no imports of the agent packages."""

from __future__ import annotations

import json
from pathlib import Path

from portfolio_common.db import Database, Row, in_clause

from kg_schema.env import universe_database_path
from kg_schema.queries import connect_ro, resolve_asset_ids, symbols_asof


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


def latest_metrics(conn: Database, cycle_date: str) -> dict[int, dict[str, float | None]]:
    """asset_id -> {"group.name": value} from the newest filing with period_end <= cycle_date."""
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT f.asset_id, MAX(f.period_end) AS pe
            FROM sec_filings f
            WHERE f.period_end IS NOT NULL AND f.period_end <= ?
            GROUP BY f.asset_id
        )
        SELECT f.asset_id, m.metric_group, m.metric_name, m.value
        FROM fundamental_metrics m
        JOIN sec_filings f ON f.id = m.filing_id
        JOIN latest l ON l.asset_id = f.asset_id AND l.pe = f.period_end
        """,
        (cycle_date,),
    ).fetchall()
    out: dict[int, dict[str, float | None]] = {}
    for r in rows:
        out.setdefault(int(r["asset_id"]), {})[f"{r['metric_group']}.{r['metric_name']}"] = r[
            "value"
        ]
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


def last_fundamental_dates(conn: Database, cycle_date: str) -> dict[int, str | None]:
    rows = conn.execute(
        """
        SELECT asset_id, MAX(event_time) AS et FROM score_snapshot
        WHERE score_type = 'FUNDAMENTAL' AND event_time <= ?
        GROUP BY asset_id
        """,
        (cycle_date,),
    ).fetchall()
    return {int(r["asset_id"]): r["et"] for r in rows}


def latest_fundamental_score(conn: Database, cycle_date: str) -> dict[int, float | None]:
    rows = conn.execute(
        """
        SELECT s.asset_id, s.raw_value
        FROM score_snapshot s
        JOIN (
            SELECT asset_id, MAX(event_time) AS et FROM score_snapshot
            WHERE score_type = 'FUNDAMENTAL' AND event_time <= ? GROUP BY asset_id
        ) last ON last.asset_id = s.asset_id AND last.et = s.event_time
        WHERE s.score_type = 'FUNDAMENTAL'
        """,
        (cycle_date,),
    ).fetchall()
    return {int(r["asset_id"]): r["raw_value"] for r in rows}


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
    conn: Database, metrics: dict[int, dict[str, float | None]]
) -> dict[int, float | None]:
    """Read market cap straight off the stored valuation metric inputs, when present."""
    rows = conn.execute(
        """
        SELECT f.asset_id, m.inputs_json
        FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id
        WHERE m.metric_group = 'valuation' AND m.metric_name = 'market_capitalization'
        """
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
