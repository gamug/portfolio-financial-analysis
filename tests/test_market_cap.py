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

from kg_schema import corporate_actions, queries
from kg_schema.availability import AvailabilityMissing
from kg_schema.market_cap import (
    NO_COVER_SHARES,
    NO_RECENT_PRICE,
    STALE_SHARES,
    UNCLASSIFIED_ACTION,
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


#: The synthetic events the split tests register: (CIK, ex_date) -> (ratio, class). The
#: reader is CIK-keyed (a CIK's two share classes share one event), so a fixture asset needs
#: its CIK and every synthetic SPLIT row its event, exactly as production's list does.
SYNTHETIC_EVENTS: dict[tuple[str, str], tuple[float, str]] = {
    ("0000000001", "2026-08-15"): (2.0, corporate_actions.SPLIT),
    ("0000000001", "2026-07-01"): (2.0, corporate_actions.SPLIT),
    ("0000000001", "2026-10-05"): (2.0, corporate_actions.SPLIT),
    ("0000000001", "2026-08-10"): (2.0, corporate_actions.SPLIT),
    ("0000000001", "2026-09-10"): (3.0, corporate_actions.SPLIT),
    # T-153 part 3d (D10): the real events, at their own CIKs and ratios.
    ("0000313616", "2023-10-02"): (1.128, corporate_actions.SPINOFF),  # DHR / Veralto
    ("0000055067", "2023-10-02"): (1.065, corporate_actions.SPINOFF),  # K / WK Kellogg
    ("0001666700", "2025-11-03"): (2.39, corporate_actions.SPINOFF),  # DD / Qnity
    ("0001045810", "2024-06-10"): (10.0, corporate_actions.SPLIT),  # NVDA, a true split
    ("0001666700", "2026-06-24"): (1 / 3, corporate_actions.REVERSE_SPLIT),  # DD, 1-for-3
}

#: The assets the D10 tests use, with the CIK each event list row is keyed by.
D10_ASSETS = {
    101: ("DHR", "0000313616"),
    102: ("K", "0000055067"),
    103: ("DD", "0001666700"),
    104: ("NVDA", "0001045810"),
}

DHR, K_TICKER, DD, NVDA = 101, 102, 103, 104

PG_CIK = "0000000001"


@pytest.fixture(autouse=True)
def _synthetic_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the fixtures' synthetic events, so the reader explains them (T-153 part 3d)."""
    events = tuple(
        corporate_actions.CorporateActionEvent(
            cik=cik,
            ticker="PG",
            ex_date=ex_date,
            value=value,
            event_class=event_class,
            evidence_form="10-Q",
            evidence_url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
            evidence_date="2026-08-01",
            evidence_quote="the Company effected a stock split",
            rules="MKT-02;C11",
        )
        for (cik, ex_date), (value, event_class) in SYNTHETIC_EVENTS.items()
    )
    monkeypatch.setattr(corporate_actions, "read_events", lambda path=None: events)


@pytest.fixture
def conn(memory_quant_db: Database) -> Database:
    memory_quant_db.execute(
        "INSERT INTO assets (id, ticker, cik) VALUES (1, 'PG', ?), (2, 'GOOGL', ?)",
        (PG_CIK, "0001652044"),
    )
    for asset_id, (ticker, cik) in D10_ASSETS.items():
        memory_quant_db.execute(
            "INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)", (asset_id, ticker, cik)
        )
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


BRK_A, BRK_B = "us-gaap:CommonClassAMember", "us-gaap:CommonClassBMember"


def test_berkshire_class_a_is_converted_to_class_b_before_summing(conn: Database) -> None:
    """PR #110 review: A 511,820 + B 1,389,605,139 summed 1:1 is 1.39B B-equivalents; 1 A is
    1,500 B, so 2.157B -- the cap was 35.6% low."""
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (3, 'BRK.B', '0001067983')")
    fid = seed_cover_shares(conn, 3, 511_820, as_of_date="2026-01-31", class_member=BRK_A)
    seed_cover_shares(
        conn, 3, 1_389_605_139, as_of_date="2026-01-31", class_member=BRK_B, filing_id=fid
    )
    _price(conn, 3, "2026-02-27", 500.0)
    cap = market_caps_as_of(conn, [3], as_of="2026-02-27").caps[3]
    assert cap.share_count == 1_389_605_139 + 1500 * 511_820 == 2_157_335_139
    assert cap.value == pytest.approx(2_157_335_139 * 500.0)
    assert cap.cik == "0001067983" and cap.n_classes == 2


def test_the_conversion_is_keyed_by_issuer_and_padding_does_not_matter() -> None:
    entries = [(BRK_A, 2.0, "2026-01-31"), (BRK_B, 3.0, "2026-01-31")]
    assert cover_total(entries, cik="0001067983") == ShareCount(3003.0, "2026-01-31", 2)
    assert cover_total(entries, cik="1067983") == ShareCount(3003.0, "2026-01-31", 2)
    assert cover_total(entries, cik="0000000001") == ShareCount(5.0, "2026-01-31", 2)
    assert cover_total(entries) == ShareCount(5.0, "2026-01-31", 2)
    # a filed total is never converted
    assert cover_total([("", 7.0, "2026-01-31"), *entries], cik="0001067983") == ShareCount(
        7.0, "2026-01-31", 0
    )


def test_cover_total_is_the_one_aggregation() -> None:
    assert cover_total([]) is None
    assert cover_total([("", 5.0, "2026-01-02")]) == ShareCount(5.0, "2026-01-02", 0)
    # newest of a class wins; the sum is dated by the oldest class
    got = cover_total(
        [("A", 1.0, "2026-01-01"), ("A", 2.0, "2026-02-01"), ("B", 3.0, "2026-01-15")]
    )
    assert got == ShareCount(5.0, "2026-01-15", 2)


def test_fox_is_valued_class_by_class(conn: Database) -> None:
    """Fox Corporation (MKT-03): Class A trades as FOXA, Class B as FOX.
    Class A count x FOXA close + Class B count x FOX close."""
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (10, 'FOX', '0001754301')")
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (11, 'FOXA', '0001754301')")
    fid = seed_cover_shares(
        conn, 10, 200.0, as_of_date="2026-07-31", class_member="us-gaap:CommonClassBMember"
    )
    seed_cover_shares(
        conn,
        10,
        100.0,
        as_of_date="2026-07-31",
        class_member="us-gaap:CommonClassAMember",
        filing_id=fid,
    )
    _price(conn, 10, AS_OF, 25.0)  # FOX = 25.0
    _price(conn, 11, AS_OF, 30.0)  # FOXA = 30.0

    res = market_caps_as_of(conn, [10], as_of=AS_OF)
    cap = res.caps[10]
    # 100 * 30.0 + 200 * 25.0 = 3,000 + 5,000 = 8,000.0
    assert cap.value == pytest.approx(8_000.0)
    assert cap.incomplete_classes is False


def test_multi_class_with_unconverted_class_is_flagged_incomplete(conn: Database) -> None:
    """A multi-class CIK with a class neither listed nor converted gets incomplete_classes=True."""
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (20, 'MULTI', '0001999999')")
    fid = seed_cover_shares(
        conn, 20, 100.0, as_of_date="2026-07-31", class_member="us-gaap:CommonClassAMember"
    )
    seed_cover_shares(
        conn,
        20,
        50.0,
        as_of_date="2026-07-31",
        class_member="us-gaap:CommonClassBMember",
        filing_id=fid,
    )
    _price(conn, 20, AS_OF, 10.0)
    res = market_caps_as_of(conn, [20], as_of=AS_OF)
    cap = res.caps[20]
    assert cap.value == pytest.approx(150.0 * 10.0)
    assert cap.incomplete_classes is True


def test_cover_total_excludes_non_common_members_blk_and_ares() -> None:
    """MKT-04: Non-common share members (Preferred, FullyDiluted, UnitsMember) are excluded."""
    # BLK: exclude blk:FullyDilutedSharesMember and blk:ExchangeableUnitsMember
    blk_entries = [
        ("blk:FullyDilutedSharesMember", 162_476_186.0, "2026-07-31"),
        ("blk:ExchangeableUnitsMember", 7_606_927.0, "2026-07-31"),
        ("us-gaap:CommonStockMember", 154_869_259.0, "2026-07-31"),
    ]
    blk_count = cover_total(blk_entries, cik="0002012383")
    assert blk_count is not None
    assert blk_count.value == 154_869_259.0
    assert blk_count.n_classes == 1
    assert blk_count.n_excluded == 2
    assert blk_count.cover_member_not_common_excluded is True

    # ARES: exclude ares:SeriesBMandatoryConvertiblePreferredStockMember
    ares_entries = [
        ("ares:SeriesBMandatoryConvertiblePreferredStockMember", 30_000_000.0, "2026-08-03"),
        ("us-gaap:CommonClassAMember", 223_957_742.0, "2026-08-03"),
        ("us-gaap:CommonClassBMember", 1000.0, "2026-08-03"),
        ("us-gaap:CommonClassCMember", 102_828_576.0, "2026-08-03"),
        ("us-gaap:NonvotingCommonStockMember", 3_489_911.0, "2026-08-03"),
    ]
    ares_count = cover_total(ares_entries, cik="0001176948")
    assert ares_count is not None
    assert ares_count.value == 223_957_742.0 + 1000.0 + 102_828_576.0 + 3_489_911.0
    assert ares_count.n_classes == 4
    assert ares_count.n_excluded == 1
    assert ares_count.cover_member_not_common_excluded is True


def test_market_caps_as_of_excludes_non_common_members_blk_and_ares(conn: Database) -> None:
    """MKT-04: market_caps_as_of reflects excluded members and sets audit flags."""
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (30, 'BLK', '0002012383')")
    fid = seed_cover_shares(
        conn,
        30,
        162_476_186.0,
        as_of_date="2026-07-31",
        class_member="blk:FullyDilutedSharesMember",
    )
    seed_cover_shares(
        conn,
        30,
        7_606_927.0,
        as_of_date="2026-07-31",
        class_member="blk:ExchangeableUnitsMember",
        filing_id=fid,
    )
    seed_cover_shares(
        conn,
        30,
        154_869_259.0,
        as_of_date="2026-07-31",
        class_member="us-gaap:CommonStockMember",
        filing_id=fid,
    )
    _price(conn, 30, AS_OF, 100.0)

    res = market_caps_as_of(conn, [30], as_of=AS_OF)
    cap = res.caps[30]
    assert cap.share_count == 154_869_259.0
    assert cap.value == pytest.approx(154_869_259.0 * 100.0)
    assert cap.n_excluded == 2
    assert cap.cover_member_not_common_excluded is True


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


# -- T-153 part 3d (D10): the spin-off factor's date, and the guard ---------------------------


def test_a_spinoff_factor_applies_only_while_the_close_is_before_the_ex_date(
    conn: Database,
) -> None:
    """DHR / Veralto, 1.128 on 2023-10-02. Both vendors adjust the price *series* for a spin-off
    (it stays continuous) while the parent's share count does not change, so the recorded factor
    belongs on the count only while the close it is paired with is still pre-event.

    Master multiplies the count by 1.128 whichever close it pairs with: 12.8% too high on the
    after-date case below.
    """
    _split(conn, DHR, "2023-10-02", 1.128)
    # A count filed before the event, and a close *after* it: the close is raw, so no factor.
    seed_cover_shares(conn, DHR, 100.0, as_of_date="2023-08-01", filing_date="2023-08-03")
    _price(conn, DHR, "2023-10-04", 1_000.0)
    cap = market_caps_as_of(conn, [DHR], as_of="2023-10-04").caps[DHR]
    assert (cap.split_factor, cap.spinoff_factor) == (1.0, 1.0)
    assert cap.value == pytest.approx(100.0 * 1_000.0)  # the filed count x the raw close
    # A close *before* it: the stored close is the vendor's adjusted one (raw / 1.128), and the
    # factor puts the count back on the raw basis -- the same cap as the filed count x raw.
    _price(conn, DHR, "2023-09-29", 1_000.0 / 1.128)
    _res = market_caps_as_of(conn, [DHR], as_of="2023-09-29")
    before = _res.caps[DHR]
    assert before.spinoff_factor == pytest.approx(1.128)
    assert before.value == pytest.approx(100.0 * 1_000.0)


def test_a_spinoff_after_a_newer_count_is_applied_the_same_way(conn: Database) -> None:
    """DD / Qnity, 2.39 on 2025-11-03: the after-date case is the one the ruling measured
    (+139% until a newer filing's count arrives)."""
    _split(conn, DD, "2025-11-03", 2.39)
    seed_cover_shares(conn, DD, 50.0, as_of_date="2025-08-01", filing_date="2025-08-05")
    _price(conn, DD, "2025-11-05", 200.0)
    cap = market_caps_as_of(conn, [DD], as_of="2025-11-05").caps[DD]
    assert cap.spinoff_factor == 1.0
    assert cap.value == pytest.approx(50.0 * 200.0)
    _price(conn, DD, "2025-10-31", 200.0 / 2.39)
    _res = market_caps_as_of(conn, [DD], as_of="2025-10-31")
    assert _res.missing == {}, _res.missing
    before = _res.caps[DD]
    assert before.spinoff_factor == pytest.approx(2.39)
    assert before.value == pytest.approx(50.0 * 200.0)


def test_a_true_split_still_applies_on_both_sides_and_a_reverse_split_divides(
    conn: Database,
) -> None:
    """NVDA 10-for-1 (2024-06-10) and DD's 1-for-3 (2026-06-24): neither is a distribution, so
    the count is put on the price's basis whichever side of the event the close falls."""
    _split(conn, NVDA, "2024-06-10", 10.0)
    seed_cover_shares(conn, NVDA, 10.0, as_of_date="2024-05-31", filing_date="2024-05-31")
    _price(conn, NVDA, "2024-06-11", 12.0)
    forward = market_caps_as_of(conn, [NVDA], as_of="2024-06-11").caps[NVDA]
    assert (forward.split_factor, forward.spinoff_factor) == (10.0, 1.0)
    assert forward.value == pytest.approx(10.0 * 10.0 * 12.0)

    _split(conn, K_TICKER, "2023-10-02", 1.065)  # K / WK Kellogg, a spin-off recorded as a split
    seed_cover_shares(conn, K_TICKER, 300.0, as_of_date="2023-08-01", filing_date="2023-08-03")
    _price(conn, K_TICKER, "2023-10-03", 40.0)
    after = market_caps_as_of(conn, [K_TICKER], as_of="2023-10-03").caps[K_TICKER]
    assert (after.split_factor, after.spinoff_factor) == (1.0, 1.0)
    assert after.value == pytest.approx(300.0 * 40.0)

    _split(conn, DD, "2026-06-24", 1 / 3)
    seed_cover_shares(conn, DD, 300.0, as_of_date="2026-06-01", filing_date="2026-06-01")
    _price(conn, DD, "2026-06-25", 40.0)
    reverse = market_caps_as_of(conn, [DD], as_of="2026-06-25").caps[DD]
    assert reverse.split_factor == pytest.approx(1 / 3)
    assert reverse.value == pytest.approx(100.0 * 40.0)


def test_a_split_row_the_event_list_does_not_explain_refuses_the_cap(conn: Database) -> None:
    """D10's guard: a factor whose class nobody stated is never applied on a guess (MKT-02)."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-08-20", 1.5)  # not in SYNTHETIC_EVENTS
    res = market_caps_as_of(conn, [1], as_of=AS_OF)
    assert res.missing == {1: UNCLASSIFIED_ACTION}
    assert res.caps == {}


def test_a_recorded_factor_that_drifted_from_the_list_is_unexplained(conn: Database) -> None:
    """The list carries the vendor's own ratio: a row whose ratio no longer matches is not
    explained by it, and refuses the cap the same way."""
    seed_cover_shares(conn, 1, 100.0, as_of_date="2026-07-31")
    _price(conn, 1, AS_OF, 50.0)
    _split(conn, 1, "2026-08-15", 2.5)  # the list says 2.0
    assert market_caps_as_of(conn, [1], as_of=AS_OF).missing == {1: UNCLASSIFIED_ACTION}
