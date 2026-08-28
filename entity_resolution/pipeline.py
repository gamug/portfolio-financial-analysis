"""Drive the entity-resolution step end to end."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from entity_resolution import db as er_db
from entity_resolution.config import Settings
from entity_resolution.cooccurrence import MIN_WEIGHT_DEFAULT, build_edges
from entity_resolution.denylist import MAX_TICKERS_DEFAULT, MIN_ARTICLES_DEFAULT
from entity_resolution.news_db import connect_ro


@dataclass
class RunParams:
    min_weight: float = MIN_WEIGHT_DEFAULT
    max_tickers: int = MAX_TICKERS_DEFAULT
    min_articles: int = MIN_ARTICLES_DEFAULT


@dataclass
class RunReport:
    cycle_run_id: int
    tickers: int
    edges: int


def _open_cycle(conn: sqlite3.Connection, params: RunParams) -> int:
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status, params_json)
        VALUES ('ENTITY_RESOLUTION', ?, ?, 'running', ?)
        ON CONFLICT (cycle_type, cycle_date) DO UPDATE SET started_at = excluded.started_at,
            status = 'running', params_json = excluded.params_json
        """,
        (now[:10], now, json.dumps(vars(params))),
    )
    conn.commit()
    if cur.lastrowid:
        return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM cycle_run WHERE cycle_type = 'ENTITY_RESOLUTION' AND cycle_date = ?",
        (now[:10],),
    ).fetchone()
    return int(row["id"])


def run(settings: Settings, params: RunParams) -> RunReport:
    conn = er_db.connect(settings.db_path)
    try:
        er_db.ensure_schema(conn)
        tickers = [
            str(r["ticker"]) for r in conn.execute("SELECT ticker FROM assets ORDER BY ticker")
        ]
        run_id = _open_cycle(conn, params)
        news = connect_ro(settings.news_db_path)
        try:
            edges = build_edges(
                news,
                tickers,
                min_weight=params.min_weight,
                max_tickers=params.max_tickers,
                min_articles=params.min_articles,
            )
        finally:
            news.close()
        written = er_db.replace_edges(conn, edges, run_id=run_id)
        conn.execute(
            "UPDATE cycle_run SET status = 'completed', finished_at = ? WHERE id = ?",
            (datetime.now(tz=UTC).isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
        return RunReport(cycle_run_id=run_id, tickers=len(tickers), edges=written)
    finally:
        conn.close()
