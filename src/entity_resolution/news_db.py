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


# -- the change watermark (T-017) ---------------------------------------------------------------
#
# ``entity_resolution pending`` and the run itself describe "the news this build saw" with three
# numbers. Each query is one of two index shapes, written out as literal SQL (never an identifier
# built at runtime): ``discovered_urls.ticker`` for the first two, a rowid read for the third.

_ROWID_PRAGMA = {
    "discovered_urls": "PRAGMA table_info(discovered_urls)",
    "article_entities": "PRAGMA table_info(article_entities)",
}


def id_is_rowid(news: Database, table: str) -> bool:
    """``True`` when *table*'s ``id`` is its ``INTEGER PRIMARY KEY`` (a rowid alias), so
    ``MAX(id)`` is a single index step. Any other shape answers ``False``: the watermark then
    says it is unavailable rather than scan a 4.5 GB file."""
    columns = news.execute(_ROWID_PRAGMA[table]).fetchall()
    pk = [c for c in columns if int(c["pk"]) > 0]
    return (
        len(pk) == 1
        and str(pk[0]["name"]).lower() == "id"
        and str(pk[0]["type"]).strip().upper() == "INTEGER"
    )


def news_watermark(news: Database, tickers: list[str], *, until: str) -> dict[str, object]:
    """``{"n_urls", "max_url_id", "max_entity_id"}`` -- what a build over *tickers* up to *until*
    would read. ``n_urls`` / ``max_url_id`` aggregate the same rows as
    :func:`article_ids_for_ticker` (``pub_date`` known and on or before *until*); ``max_entity_id``
    is the newest NER row anywhere, so a span added later to an article already counted still
    moves it (it also moves for other tickers and later dates -- a conservative over-trigger).
    ``{"unavailable": <why>}`` when a table's ``id`` is not a rowid key."""
    for table in ("discovered_urls", "article_entities"):
        if not id_is_rowid(news, table):
            return {"unavailable": f"{table}.id is not a rowid key"}
    n_urls = 0
    max_url_id = 0
    for ticker in tickers:
        row = news.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS top FROM discovered_urls "
            "WHERE ticker = ? AND pub_date IS NOT NULL AND pub_date <= ?",
            (ticker, until),
        ).fetchone()
        n_urls += int(row["n"])
        max_url_id = max(max_url_id, int(row["top"] or 0))
    entity = news.execute("SELECT MAX(id) AS top FROM article_entities").fetchone()
    return {
        "n_urls": n_urls,
        "max_url_id": max_url_id,
        "max_entity_id": int(entity["top"] or 0),
    }
