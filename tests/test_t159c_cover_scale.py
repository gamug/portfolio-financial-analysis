"""T-159c §2b: a cover count x1,000 / x1/1,000 against the prior filing gets a stored flag."""

from __future__ import annotations

from conftest import seed_cover_shares
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.statements import CoverShares


def _current(value: float, as_of: str = "2022-09-30") -> list[CoverShares]:
    return [CoverShares(value, as_of)]


def _seed(memory_db: Database, value: float, as_of: str, filed: str) -> int:
    return seed_cover_shares(memory_db, 1, value, as_of_date=as_of, filing_date=filed)


def test_a_cover_count_1000x_the_prior_filing_is_flagged(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(memory_db, 100.0, "2022-06-30", "2022-07-28")
    current = _seed(memory_db, 100_000.0, "2022-09-30", "2022-10-28")
    assert db.cover_share_scale_suspect(
        memory_db,
        1,
        _current(100_000.0),
        filing_id=current,
        filing_date="2022-10-28",
    )


def test_a_cover_count_1000th_of_the_prior_filing_is_flagged(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(memory_db, 100_000.0, "2022-06-30", "2022-07-28")
    current = _seed(memory_db, 100.0, "2022-09-30", "2022-10-28")
    assert db.cover_share_scale_suspect(
        memory_db,
        1,
        _current(100.0),
        filing_id=current,
        filing_date="2022-10-28",
    )


def test_a_normal_change_is_not_flagged(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(memory_db, 100.0, "2022-06-30", "2022-07-28")
    current = _seed(memory_db, 105.0, "2022-09-30", "2022-10-28")
    assert not db.cover_share_scale_suspect(
        memory_db,
        1,
        _current(105.0),
        filing_id=current,
        filing_date="2022-10-28",
    )


def test_a_later_filing_is_not_history(memory_db: Database) -> None:
    """Point in time: a future filing's x1,000 count must not make this one look clean, and the
    future count itself is not an anchor."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(memory_db, 100.0, "2022-06-30", "2022-07-28")
    current = _seed(memory_db, 100.0, "2022-09-30", "2022-10-28")
    _seed(memory_db, 100_000.0, "2022-12-31", "2023-01-28")
    assert not db.cover_share_scale_suspect(
        memory_db,
        1,
        _current(100.0),
        filing_id=current,
        filing_date="2022-10-28",
    )
