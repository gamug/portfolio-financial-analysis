"""Read-only accessor for the news repo's ``urls.db`` (4.5 GB, WAL).

Hard rule: only two query shapes are allowed, both hitting an index --
``discovered_urls.ticker`` and ``article_entities.article_id``. This module must
never name ``articles`` or ``body_text``; ``test_entity_news_db`` enforces it by
tracing executed SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_ID_CHUNK = 900  # keep under SQLite's parameter limit


@dataclass(frozen=True)
class PerSpan:
    article_id: int
    text: str
    score: float | None


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open *path* strictly read-only so the news pipeline's WAL is untouched."""
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def article_ids_for_ticker(news: sqlite3.Connection, ticker: str) -> list[int]:
    """Every article id discovered for *ticker* (uses ``idx_ticker``)."""
    rows = news.execute("SELECT id FROM discovered_urls WHERE ticker = ?", (ticker,)).fetchall()
    return [int(r[0]) for r in rows]


def per_entities_for_articles(
    news: sqlite3.Connection, article_ids: list[int]
) -> Iterator[PerSpan]:
    """Stream PER spans for *article_ids* in id-index chunks (uses
    ``idx_article_entities_article_id``)."""
    for start in range(0, len(article_ids), _ID_CHUNK):
        chunk = article_ids[start : start + _ID_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        # placeholders is only "?,?,..." -- the ids are bound parameters, not interpolated.
        sql = (
            "SELECT article_id, text, score FROM article_entities "  # noqa: S608
            f"WHERE entity_type = 'PER' AND article_id IN ({placeholders})"
        )
        rows = news.execute(sql, chunk)
        for r in rows:
            yield PerSpan(int(r["article_id"]), str(r["text"]), r["score"])
