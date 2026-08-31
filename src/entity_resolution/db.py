"""Write ``shared_executive_edge`` into ``KG_FINANTIAL_DB``."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import kg_schema
from entity_resolution.cooccurrence import Edge

METHOD = "news-per-cooccurrence-v1"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    kg_schema.ensure(conn)


def _asset_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {str(r["ticker"]): int(r["id"]) for r in conn.execute("SELECT id, ticker FROM assets")}


def replace_edges(
    conn: sqlite3.Connection,
    edges: Iterable[Edge],
    *,
    method: str = METHOD,
    run_id: int | None = None,
) -> int:
    """Full method-versioned recompute: drop rows of *method*, then insert *edges*."""
    ids = _asset_ids(conn)
    now = _now()
    conn.execute("DELETE FROM shared_executive_edge WHERE method = ?", (method,))
    rows = []
    for e in edges:
        aid_a, aid_b = ids.get(e.ticker_a), ids.get(e.ticker_b)
        if aid_a is None or aid_b is None:
            continue
        lo, hi = sorted((aid_a, aid_b))
        ca, cb = (
            (e.article_count_a, e.article_count_b)
            if aid_a < aid_b
            else (
                e.article_count_b,
                e.article_count_a,
            )
        )
        rows.append(
            (
                lo,
                hi,
                e.person_name,
                ca,
                cb,
                e.weight,
                None,
                None,
                method,
                json.dumps({"mean_ner_score": _mean(e.scores)}),
                now,
                run_id,
            )
        )
    conn.executemany(
        """
        INSERT OR IGNORE INTO shared_executive_edge
            (asset_id_a, asset_id_b, person_name, article_count_a, article_count_b, weight,
             first_seen, last_seen, method, evidence_json, computed_at, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
