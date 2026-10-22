"""T-159c §2b: a cover count x1,000 / x1/1,000 against the prior filing gets a stored flag."""

from __future__ import annotations

from conftest import seed_cover_shares
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.statements import CoverShares


def _current(value: float, as_of: str = "2022-09-30") -> list[CoverShares]:
    return [CoverShares(value, as_of)]


def _seed(
    memory_db: Database,
    value: float,
    as_of: str,
    filed: str,
    class_member: str = "",
) -> int:
    return seed_cover_shares(
        memory_db,
        1,
        value,
        as_of_date=as_of,
        filing_date=filed,
        class_member=class_member,
    )


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


def test_a_50x_class_event_is_not_flagged(memory_db: Database) -> None:
    """V's 2024 class-B exchange: 245.5M against 4.8M is ~50.8x, not the x1,000 defect."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(
        memory_db,
        4_835_384.0,
        "2023-12-31",
        "2024-02-01",
        class_member="v:CommonClassB1Member",
    )
    current = _seed(
        memory_db,
        245_513_385.0,
        "2024-03-31",
        "2024-04-25",
        class_member="v:CommonClassB1Member",
    )
    assert not db.cover_share_scale_suspect(
        memory_db,
        1,
        [CoverShares(245_513_385.0, "2024-03-31", "v:CommonClassB1Member")],
        filing_id=current,
        filing_date="2024-04-25",
    )


def test_a_recovery_filing_after_a_defect_is_not_flagged(memory_db: Database) -> None:
    """CMG 2022-06-30: the median of the up-to-4 prior filings is the normal ~27.7M, not the
    defective 2022-03-31 count."""
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")
    _seed(memory_db, 27_700_000.0, "2021-09-30", "2021-10-28")
    _seed(memory_db, 27_700_000.0, "2021-12-31", "2022-02-10")
    _seed(memory_db, 27_962.0, "2022-03-31", "2022-04-28")
    current = _seed(memory_db, 27_742_000.0, "2022-06-30", "2022-07-28")
    assert not db.cover_share_scale_suspect(
        memory_db,
        1,
        _current(27_742_000.0, "2022-06-30"),
        filing_id=current,
        filing_date="2022-07-28",
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
