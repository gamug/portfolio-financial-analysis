"""The as-of market cap reader (T-132): a cover-page share count times the close on the date.

Every case below is a way the old stored-metric cap was wrong, or one the new reader must refuse
rather than guess: a count that includes treasury stock (PG: 4.009B issued vs 2.324B outstanding),
a count two years old (PG's 2024-03-31 value was used on 2026-09-29), a close from another day, a
split between the count and the close, a filing nobody could know yet.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import seed_cover_shares
from portfolio_common.db import Database

from kg_schema import queries
from kg_schema.availability import AvailabilityMissing
from kg_schema.market_cap import (
    NO_COVER_SHARES,
    NO_RECENT_PRICE,
    STALE_SHARES,
    ShareCount,
    cover_total,
    market_caps_as_of,
)

AS_OF = "2026-09-29"


def _price(conn: Database, asset_id: int, day: str, close: float) -> None:
    conn.execute(
        "INSERT INTO price_daily (asset_id, date, close) VALUES (?, ?, ?)", (asset_id, day, close)
    )


def _split(
    conn: Database, asset_id: int, ex_date: str, ratio: float, *, engine: str = "corpact-v1"
) -> None:
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (?, 'SPLIT', ?, ?, 'pricing-gateway', ?, 'now')",
        (asset_id, ex_date, ratio, engine),
    )


@pytest.fixture
def conn(memory_quant_db: Database) -> Database:
    memory_quant_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'PG'), (2, 'GOOGL')")
    return memory_quant_db


# -- the formula ------------------------------------------------------------------------------


def test_the_cap_is_the_cover_count_times_the_close_not_shares_issued(conn: Database) -> None:
    """PG: 2.324B outstanding on the cover; the 4.009B 'issued' count is never read."""
    seed_cover_shares(conn, 1, 2_324_433_060, as_of_date="2026-07-31", filing_date="2026-08-05")
    _price(conn, 1, AS_OF, 140.0)
    res = market_caps_as_of(conn, [1], as_of=AS_OF)
    cap = res.caps[1]
    assert cap.value == pytest.approx(2_324_433_060 * 140.0)
    assert (cap.share_count, cap.share_as_of, cap.close, cap.close_date) == (
        2_324_433_060,
        "2026-07-31",
        140.0,
        AS_OF,
    )
    assert cap.share_age_days == 60 and cap.n_classes == 0 and cap.split_factor == 1.0
    assert res.get(1) == cap.value and res.missing == {}


def test_the_close_is_the_last_one_on_or_before_the_date(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, "2026-09-25", 10.0)  # Friday
    _price(conn, 1, "2026-09-30", 99.0)  # after the as-of date: never read
    cap = market_caps_as_of(conn, [1], as_of="2026-09-27").caps[1]  # a Sunday
    assert (cap.close, cap.close_date) == (10.0, "2026-09-25")


def test_the_latest_filing_wins(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-04-30", filing_date="2026-05-05")
    seed_cover_shares(conn, 1, 90.0, as_of_date="2026-07-31", filing_date="2026-08-05")
    _price(conn, 1, AS_OF, 2.0)
    assert market_caps_as_of(conn, [1], as_of=AS_OF).get(1) == 180.0  # the buyback, not 200


# -- what it refuses ---------------------------------------------------------------------------


def test_a_name_with_no_cover_count_is_missing_not_zero(conn: Database) -> None:
    _price(conn, 1, AS_OF, 140.0)
    res = market_caps_as_of(conn, [1], as_of=AS_OF)
    assert res.caps == {} and res.missing == {1: NO_COVER_SHARES}
    assert res.get(1) is None


def test_a_count_older_than_the_limit_is_refused(conn: Database) -> None:
    """PG's 2024-03-31 value was used on 2026-09-29 by the stored-metric reader."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2024-03-31")
    _price(conn, 1, AS_OF, 140.0)
    assert market_caps_as_of(conn, [1], as_of=AS_OF).missing == {1: STALE_SHARES}


def test_the_age_limit_is_a_parameter_and_inclusive(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-03-13")  # exactly 200 days before AS_OF
    _price(conn, 1, AS_OF, 1.0)
    assert 1 in market_caps_as_of(conn, [1], as_of=AS_OF).caps
    assert market_caps_as_of(conn, [1], as_of=AS_OF, max_share_age_days=199).missing == {
        1: STALE_SHARES
    }


def test_a_close_older_than_the_limit_is_refused(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, "2026-09-18", 5.0)  # 11 days before
    assert market_caps_as_of(conn, [1], as_of=AS_OF).missing == {1: NO_RECENT_PRICE}
    _price(conn, 1, "2026-09-19", 5.0)  # 10 days: usable
    assert 1 in market_caps_as_of(conn, [1], as_of=AS_OF).caps


def test_no_price_at_all_is_missing(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    assert market_caps_as_of(conn, [1], as_of=AS_OF).missing == {1: NO_RECENT_PRICE}


# -- point in time -----------------------------------------------------------------------------


def test_a_filing_is_not_usable_before_its_next_session(conn: Database) -> None:
    """Filed Friday 2026-09-25: known from Monday, whatever the count's own date says."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-09-20", filing_date="2026-09-25")
    for day in ("2026-09-25", "2026-09-26", "2026-09-27"):
        _price(conn, 1, day, 1.0)
        assert market_caps_as_of(conn, [1], as_of=day).missing == {1: NO_COVER_SHARES}, day
    _price(conn, 1, "2026-09-28", 1.0)
    assert market_caps_as_of(conn, [1], as_of="2026-09-28").get(1) == 100.0


def test_a_later_filing_is_never_read_on_an_earlier_date(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-04-30", filing_date="2026-05-05")
    seed_cover_shares(conn, 1, 50.0, as_of_date="2026-07-31", filing_date="2026-08-05")
    _price(conn, 1, "2026-07-01", 1.0)
    assert market_caps_as_of(conn, [1], as_of="2026-07-01").get(1) == 100.0


def test_a_database_without_the_availability_backfill_is_refused(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    conn.execute("DROP TRIGGER IF EXISTS trg_sf_available_update")
    conn.execute("UPDATE sec_filings SET available_at = NULL")
    with pytest.raises(AvailabilityMissing):
        market_caps_as_of(conn, [1], as_of=AS_OF)


# -- splits ------------------------------------------------------------------------------------


def test_a_count_from_before_a_split_is_put_on_the_price_basis(conn: Database) -> None:
    """A 2-for-1 on 2026-08-15, a count of 100 from 2026-07-31 (pre-split shares), a close of 50
    from the gateway, which has adjusted history to the post-split basis: the cap is 200 x 50, not
    100 x 50 -- the unadjusted count against an adjusted price understates it by the ratio."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-08-15", 2.0)
    cap = market_caps_as_of(conn, [1], as_of=AS_OF).caps[1]
    assert (cap.shares, cap.split_factor, cap.value) == (200.0, 2.0, 10_000.0)


def test_a_split_before_the_count_leaves_it_alone(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-07-01", 2.0)  # the count already reflects it
    assert market_caps_as_of(conn, [1], as_of=AS_OF).caps[1].split_factor == 1.0


def test_a_split_after_the_last_stored_bar_is_not_applied(conn: Database) -> None:
    """The close has not been adjusted for a split recorded after the newest bar it was fetched
    with, so the count must not be either."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-10-05", 2.0)
    assert market_caps_as_of(conn, [1], as_of=AS_OF).caps[1].split_factor == 1.0


def test_a_historical_date_is_put_on_the_basis_of_the_newest_bar(conn: Database) -> None:
    """As of 2026-08-01 (before the split) the stored close is already post-split-adjusted
    because the series was fetched after it: the count is scaled too, or the cap halves."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-29", filing_date="2026-07-30")
    _price(conn, 1, "2026-08-01", 50.0)
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-08-15", 2.0)
    assert market_caps_as_of(conn, [1], as_of="2026-08-01").get(1) == 10_000.0


def test_two_splits_compound_and_only_the_gateway_engine_counts(conn: Database) -> None:
    seed_cover_shares(conn, 1, 10.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 1.0)
    _split(conn, 1, "2026-08-10", 2.0)
    _split(conn, 1, "2026-09-10", 3.0)
    _split(conn, 1, "2026-08-20", 7.0, engine="corpact-v0-approx")  # a legacy approximation
    assert market_caps_as_of(conn, [1], as_of=AS_OF).get(1) == 60.0


# -- classes -----------------------------------------------------------------------------------


def test_a_single_class_or_a_filed_total_is_one_count(conn: Database) -> None:
    fid = seed_cover_shares(conn, 2, 1000.0, as_of_date="2026-07-23", class_member="")
    seed_cover_shares(conn, 2, 600.0, as_of_date="2026-07-23", class_member="ClassA", filing_id=fid)
    seed_cover_shares(conn, 2, 400.0, as_of_date="2026-07-23", class_member="ClassB", filing_id=fid)
    _price(conn, 2, AS_OF, 1.0)
    cap = market_caps_as_of(conn, [2], as_of=AS_OF).caps[2]
    assert (cap.share_count, cap.n_classes) == (1000.0, 0)  # the filer's own total, never summed


def test_classes_with_no_filed_total_are_summed_and_flagged(conn: Database) -> None:
    fid = seed_cover_shares(conn, 2, 5_822.0, as_of_date="2026-07-28", class_member="ClassA")
    seed_cover_shares(conn, 2, 837.0, as_of_date="2026-07-28", class_member="ClassB", filing_id=fid)
    seed_cover_shares(
        conn, 2, 5_438.0, as_of_date="2026-07-28", class_member="ClassC", filing_id=fid
    )
    _price(conn, 2, AS_OF, 2.0)
    res = market_caps_as_of(conn, [2], as_of=AS_OF)
    assert (res.caps[2].share_count, res.caps[2].n_classes) == (12_097.0, 3)
    assert res.coverage()["n_multi_class"] == 1


def test_cover_total_is_the_one_aggregation() -> None:
    assert cover_total([]) is None
    assert cover_total([("", 5.0, "2026-01-02")]) == ShareCount(5.0, "2026-01-02", 0)
    # newest of a class wins; the sum is dated by the oldest class
    got = cover_total(
        [("A", 1.0, "2026-01-01"), ("A", 2.0, "2026-02-01"), ("B", 3.0, "2026-01-15")]
    )
    assert got == ShareCount(5.0, "2026-01-15", 2)


# -- the result --------------------------------------------------------------------------------


def test_coverage_reports_who_is_missing_and_how_old_the_counts_are(conn: Database) -> None:
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 1.0)
    cov = market_caps_as_of(conn, [1, 2], as_of=AS_OF).coverage()
    assert cov["n_assets"] == 2 and cov["n_with_cap"] == 1 and cov["n_missing"] == 1
    assert cov["missing"] == {"2": NO_COVER_SHARES}
    assert (cov["share_age_days_median"], cov["share_age_days_max"]) == (60.0, 60)
    assert cov["max_share_age_days"] == 200


def test_no_assets_is_an_empty_result(conn: Database) -> None:
    res = market_caps_as_of(conn, [], as_of=AS_OF)
    assert res.caps == {} and res.missing == {}


def test_a_database_without_the_tables_values_nothing() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    assert queries.cover_share_rows(Database(raw), [1], AS_OF) == []
    assert queries.last_closes(Database(raw), [1], as_of=AS_OF, floor="2026-01-01") == []
    assert queries.last_price_dates(Database(raw), [1]) == {}
    assert queries.split_rows(Database(raw), [1], engine_version="corpact-v1") == []
