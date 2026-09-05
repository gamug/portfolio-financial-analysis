"""Entity resolution: name canonicalization, denylist, co-occurrence, read-only accessor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

from entity_resolution import db as er_db
from entity_resolution.cooccurrence import build_edges
from entity_resolution.denylist import is_denied
from entity_resolution.news_db import connect_ro
from entity_resolution.normalize import canonical

# urls.db subset: only the tables/indexes the accessor is allowed to touch.
_NEWS_SCHEMA = """
CREATE TABLE discovered_urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, domain TEXT NOT NULL,
    company TEXT NOT NULL, ticker TEXT NOT NULL, source TEXT NOT NULL,
    discovered_at TEXT NOT NULL, pub_date TEXT, title TEXT, status TEXT NOT NULL DEFAULT 'done'
);
CREATE INDEX idx_ticker ON discovered_urls (ticker);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY, ticker TEXT, body_text TEXT
);
CREATE TABLE article_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL, text TEXT NOT NULL, start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL, score REAL, model_name TEXT NOT NULL, processed_at TEXT NOT NULL
);
CREATE INDEX idx_article_entities_article_id ON article_entities (article_id);
"""


def _build_news_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_NEWS_SCHEMA)
    aid = 0
    spans: list[tuple[int, str, str, float]] = []

    def add(ticker: str, people: list[str], score: float = 0.99) -> None:
        nonlocal aid
        aid += 1
        conn.execute(
            "INSERT INTO discovered_urls (id, url, domain, company, ticker, source, "
            "discovered_at) VALUES (?, ?, 'x.com', ?, ?, 'test', '2026-01-01T00:00:00')",
            (aid, f"http://x/{aid}", ticker, ticker),
        )
        conn.execute(
            "INSERT INTO articles (id, ticker, body_text) VALUES (?, ?, 'body')", (aid, ticker)
        )
        for p in people:
            spans.append((aid, "PER", p, score))

    # A genuine shared executive across AAA and BBB (4 & 3 articles -> weight 3)
    for _ in range(4):
        add("AAA", ["Jane Roe"])
    for _ in range(3):
        add("BBB", ["Jane Roe"])
    # A pundit across many tickers -> heuristic drop
    for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]:
        for _ in range(4):
            add(t, ["Jim Cramer"])
    # A first-name-only fragment -> normalize drop
    for _ in range(4):
        add("AAA", ["Bob"])
        add("BBB", ["Bob"])
    # A shared name but too few articles on one side -> below min_articles
    add("CCC", ["Rare Person"])
    for _ in range(4):
        add("DDD", ["Rare Person"])

    conn.executemany(
        "INSERT INTO article_entities (article_id, entity_type, text, start_char, end_char, "
        "score, model_name, processed_at) VALUES (?, ?, ?, 0, 5, ?, 'dslim/bert', '2026')",
        spans,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def news_db(tmp_path: Path) -> Database:
    path = tmp_path / "urls_sample.db"
    _build_news_db(path)
    return connect_ro(path)


# -- normalize -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane Roe", "Jane Roe"),
        ("  Dr. Jane   Roe ", "Jane Roe"),
        ("JANE ROE", "Jane Roe"),
        ("CEO Tim Cook", "Tim Cook"),
        ("Bob", None),
        ("J", None),
        ("Cra", None),
    ],
)
def test_canonical(raw: str, expected: str | None) -> None:
    assert canonical(raw) == expected


# -- denylist -----------------------------------------------------------


def test_is_denied() -> None:
    assert is_denied("Jim Cramer", distinct_ticker_count=2)
    assert is_denied("Jane Roe", distinct_ticker_count=40)  # too many tickers
    assert is_denied("Jane Roe", distinct_ticker_count=3, mean_score=0.5)  # weak NER
    assert not is_denied("Jane Roe", distinct_ticker_count=3, mean_score=0.99)


# -- co-occurrence ----------------------------------------------------


def test_build_edges_keeps_only_the_genuine_shared_executive(
    news_db: Database,
) -> None:
    universe = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
    edges = build_edges(news_db, universe, min_weight=3.0, max_tickers=5, min_articles=3)
    assert len(edges) == 1
    e = edges[0]
    assert (e.ticker_a, e.ticker_b, e.person_name) == ("AAA", "BBB", "Jane Roe")
    assert e.weight == 3.0
    assert {e.article_count_a, e.article_count_b} == {4, 3}


def test_accessor_never_touches_articles_or_body_text(news_db: Database) -> None:
    seen: list[str] = []
    news_db.raw.set_trace_callback(seen.append)
    build_edges(news_db, ["AAA", "BBB"], min_weight=1.0, max_tickers=99, min_articles=1)
    news_db.raw.set_trace_callback(None)
    joined = " ".join(seen).lower()
    assert "articles" not in joined
    assert "body_text" not in joined
    assert "discovered_urls" in joined and "article_entities" in joined


# -- writer ---------------------------------------------------------


def test_replace_edges_is_method_versioned(memory_db: Database, news_db: Database) -> None:
    for t in ("AAA", "BBB"):
        memory_db.execute("INSERT INTO assets (ticker) VALUES (?)", (t,))
    memory_db.commit()
    edges = build_edges(news_db, ["AAA", "BBB"], min_weight=3.0, max_tickers=5, min_articles=3)

    er_db.replace_edges(memory_db, edges)
    er_db.replace_edges(memory_db, edges)  # idempotent -- delete-by-method then reinsert
    rows = memory_db.execute(
        "SELECT ticker_a, ticker_b, person_count, total_weight FROM v_shared_executive_edge"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("AAA", "BBB", 1, 3.0)]
