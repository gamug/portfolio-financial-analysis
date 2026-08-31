"""Filter out people who co-occur across tickers but aren't shared executives.

The static list is seeded from the most frequent PER spans in the ``urls.db``
snapshot: US politicians and CNBC "Fast Money" pundits dominate business-news
person mentions. The heuristics catch the long tail (analysts appearing across
dozens of tickers, first-name-only fragments, weak NER hits).
"""

from __future__ import annotations

# Names (canonical form) that are never a shared-executive signal.
STATIC_DENY: frozenset[str] = frozenset(
    {
        "Donald Trump",
        "Trump",
        "Joe Biden",
        "Biden",
        "Barack Obama",
        "Obama",
        "Jim Cramer",
        "Cramer",
        "Pete Najarian",
        "Jon Najarian",
        "Guy Adami",
        "Karen Finerman",
        "Brian Kelly",
        "Michael Bloom",
        "Steve Grasso",
        "Dan Nathan",
        "Warren Buffett",  # widely quoted, rarely an actual shared director here
    }
)

# A person mentioned for more than this many distinct tickers is treated as an
# analyst / pundit / journalist, not a shared executive.
MAX_TICKERS_DEFAULT = 15
# Fewer than this many articles for a given ticker is too thin to trust the edge.
MIN_ARTICLES_DEFAULT = 3
# Minimum mean NER confidence for a person's spans.
MIN_NER_SCORE = 0.90


def is_denied(
    canonical_name: str,
    *,
    distinct_ticker_count: int,
    max_tickers: int = MAX_TICKERS_DEFAULT,
    mean_score: float | None = None,
) -> bool:
    if canonical_name in STATIC_DENY:
        return True
    if distinct_ticker_count > max_tickers:
        return True
    return mean_score is not None and mean_score < MIN_NER_SCORE
