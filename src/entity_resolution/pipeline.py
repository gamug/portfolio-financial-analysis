"""Drive the entity-resolution step end to end."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from portfolio_common.db import Database

from entity_resolution import db as er_db
from entity_resolution.config import Settings
from entity_resolution.cooccurrence import MIN_WEIGHT_DEFAULT, build_edges
from entity_resolution.denylist import MAX_TICKERS_DEFAULT, MIN_ARTICLES_DEFAULT
from kg_schema import connect, connect_ro, rundate
from kg_schema.provenance import DirtyTree, code_version, dirty_tree_reason
from kg_schema.queries import symbols_asof


@dataclass
class RunParams:
    min_weight: float = MIN_WEIGHT_DEFAULT
    max_tickers: int = MAX_TICKERS_DEFAULT
    min_articles: int = MIN_ARTICLES_DEFAULT
    analysis_date: str = field(default_factory=rundate.today)


@dataclass
class RunReport:
    cycle_run_id: int
    tickers: int
    edges: int
    dirty_tree_bypassed: str | None = None  # T-114: why, if --allow-dirty overrode it


def _open_cycle(conn: Database, params: RunParams, *, cv: str, dirty_reason: str | None) -> int:
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    cycle_date = params.analysis_date
    params_json = json.dumps({**vars(params), "dirty_tree_bypassed": dirty_reason})
    cur = conn.execute(
        """
        INSERT INTO cycle_run
            (cycle_type, cycle_date, started_at, status, params_json, code_version)
        VALUES ('ENTITY_RESOLUTION', ?, ?, 'running', ?, ?)
        ON CONFLICT (cycle_type, cycle_date) DO UPDATE SET started_at = excluded.started_at,
            status = 'running', params_json = excluded.params_json,
            code_version = excluded.code_version
        """,
        (cycle_date, now, params_json, cv),
    )
    conn.commit()
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM cycle_run WHERE cycle_type = 'ENTITY_RESOLUTION' AND cycle_date = ?",
        (cycle_date,),
    ).fetchone()
    return int(row["id"])


def run(settings: Settings, params: RunParams) -> RunReport:
    conn = connect(settings.db_path)
    try:
        er_db.ensure_schema(conn)
        cv = code_version()
        dirty_reason = dirty_tree_reason(cv)
        if dirty_reason is not None and not settings.allow_dirty:
            raise DirtyTree(
                f"{dirty_reason}; pass --allow-dirty for a deliberate run from an uncommitted tree"
            )
        uconn = connect_ro(settings.universe_db_path)
        try:
            tickers = symbols_asof(uconn, params.analysis_date)
        finally:
            uconn.close()
        run_id = _open_cycle(conn, params, cv=cv, dirty_reason=dirty_reason)
        news = connect_ro(settings.news_db_path)
        try:
            edges = build_edges(
                news,
                tickers,
                min_weight=params.min_weight,
                max_tickers=params.max_tickers,
                min_articles=params.min_articles,
                until=params.analysis_date,
            )
        finally:
            news.close()
        written = er_db.replace_edges(conn, edges, run_id=run_id)
        conn.execute(
            "UPDATE cycle_run SET status = 'completed', finished_at = ? WHERE id = ?",
            (datetime.now(tz=UTC).isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
        return RunReport(
            cycle_run_id=run_id,
            tickers=len(tickers),
            edges=written,
            dirty_tree_bypassed=dirty_reason,
        )
    finally:
        conn.close()
