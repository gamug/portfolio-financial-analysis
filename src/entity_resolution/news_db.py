"""Read-only accessor for the news repo's ``urls.db`` (4.5 GB, WAL).

Hard rule: only two query shapes are allowed, both hitting an index --
``discovered_urls.ticker`` and ``article_entities.article_id``. This module must
never name ``articles`` or ``body_text``; ``test_entity_news_db`` enforces it by
tracing executed SQL.

Open it with :func:`kg_schema.connect_ro` -- import that directly rather
than through this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from portfolio_common.db import Database

_ID_CHUNK = 900  # keep under SQLite's parameter limit


@dataclass(frozen=True)
class PerSpan:
    article_id: int
    text: str
    score: float | None


def article_ids_for_ticker(news: Database, ticker: str, *, until: str | None = None) -> list[int]:
    """Article ids discovered for *ticker* (uses ``idx_ticker``).

    When *until* (an ISO date) is given, only rows with a known ``pub_date`` on or
    before it are returned -- the no-lookahead filter. Rows with a NULL
    ``pub_date`` are excluded because they cannot be date-verified."""
    if until is None:
        rows = news.execute("SELECT id FROM discovered_urls WHERE ticker = ?", (ticker,)).fetchall()
    else:
        rows = news.execute(
            "SELECT id FROM discovered_urls "
            "WHERE ticker = ? AND pub_date IS NOT NULL AND pub_date <= ?",
            (ticker, until),
        ).fetchall()
    return [int(r[0]) for r in rows]


def per_entities_for_articles(news: Database, article_ids: list[int]) -> Iterator[PerSpan]:
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
