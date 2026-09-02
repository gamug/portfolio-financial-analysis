"""Pair assets whose news articles mention the same person."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from entity_resolution import denylist
from entity_resolution.news_db import article_ids_for_ticker, per_entities_for_articles
from entity_resolution.normalize import canonical

MIN_WEIGHT_DEFAULT = 3.0


@dataclass(frozen=True)
class Edge:
    ticker_a: str
    ticker_b: str  # ticker_a < ticker_b lexicographically
    person_name: str
    article_count_a: int
    article_count_b: int
    weight: float
    scores: list[float]


@dataclass
class _PerTicker:
    articles: set[int]
    scores: list[float]


def _collect_person_map(
    news: sqlite3.Connection, tickers: list[str], *, until: str | None = None
) -> dict[str, dict[str, _PerTicker]]:
    """``{canonical_name: {ticker: _PerTicker}}`` from PER spans."""
    person_map: dict[str, dict[str, _PerTicker]] = defaultdict(dict)
    for ticker in tickers:
        ids = article_ids_for_ticker(news, ticker, until=until)
        if not ids:
            continue
        for span in per_entities_for_articles(news, ids):
            name = canonical(span.text)
            if name is None:
                continue
            bucket = person_map[name].setdefault(ticker, _PerTicker(set(), []))
            bucket.articles.add(span.article_id)
            if span.score is not None:
                bucket.scores.append(float(span.score))
    return person_map


def build_edges(  # noqa: PLR0913 - keyword-only filter knobs with defaults
    news: sqlite3.Connection,
    tickers: list[str],
    *,
    min_weight: float = MIN_WEIGHT_DEFAULT,
    max_tickers: int = denylist.MAX_TICKERS_DEFAULT,
    min_articles: int = denylist.MIN_ARTICLES_DEFAULT,
    until: str | None = None,
) -> list[Edge]:
    """Build filtered shared-person edges. *tickers* is the analysis universe;
    *until* caps the news to articles published on or before that date."""
    person_map = _collect_person_map(news, tickers, until=until)
    edges: list[Edge] = []
    for name, per_ticker in person_map.items():
        distinct = len(per_ticker)
        if distinct < 2:  # noqa: PLR2004 - needs at least two tickers to form an edge
            continue
        all_scores = [s for bucket in per_ticker.values() for s in bucket.scores]
        mean_score = sum(all_scores) / len(all_scores) if all_scores else None
        if denylist.is_denied(
            name,
            distinct_ticker_count=distinct,
            max_tickers=max_tickers,
            mean_score=mean_score,
        ):
            continue
        strong = {t: b for t, b in per_ticker.items() if len(b.articles) >= min_articles}
        names = sorted(strong)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                ta, tb = names[i], names[j]
                ca, cb = len(strong[ta].articles), len(strong[tb].articles)
                weight = float(min(ca, cb))
                if weight < min_weight:
                    continue
                edges.append(
                    Edge(
                        ticker_a=ta,
                        ticker_b=tb,
                        person_name=name,
                        article_count_a=ca,
                        article_count_b=cb,
                        weight=weight,
                        scores=strong[ta].scores + strong[tb].scores,
                    )
                )
    return edges
