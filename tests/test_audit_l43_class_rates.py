"""L-43 (MKT-03, reviewer A 2026-10-29): an unlisted class is valued through the conversion rate its own filing prints,
recorded with the citing accession in ``cover_class_conversion``; with a rate for every unlisted class the market cap is
no longer marked incomplete, and a database without the table reads as before."""

from __future__ import annotations

import pytest
from conftest import seed_cover_shares
from portfolio_common.db import Database

from kg_schema import queries
from kg_schema.market_cap import cover_total, market_caps_as_of

AS_OF = "2026-09-29"


@pytest.fixture
def conn(memory_quant_db: Database) -> Database:
    return memory_quant_db


TABLE = (
    "CREATE TABLE cover_class_conversion (accession TEXT NOT NULL, class_member TEXT NOT NULL, rate REAL NOT NULL, "
    "source_accession TEXT NOT NULL, source_text TEXT NOT NULL, rule TEXT, PRIMARY KEY (accession, class_member))"
)


def _visa_like(conn: Database) -> int:
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (30, 'VLIKE', '0001888888')")
    fid = seed_cover_shares(
        conn, 30, 1_000.0, as_of_date="2026-07-31", class_member="us-gaap:CommonClassAMember"
    )
    seed_cover_shares(
        conn,
        30,
        100.0,
        as_of_date="2026-07-31",
        class_member="us-gaap:CommonClassBMember",
        filing_id=fid,
    )
    seed_cover_shares(
        conn,
        30,
        10.0,
        as_of_date="2026-07-31",
        class_member="us-gaap:CommonClassCMember",
        filing_id=fid,
    )
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (30, ?, 10.0)", (AS_OF,))
    return fid


def _accession(conn: Database, fid: int) -> str:
    acc = f"0001888888-26-{fid:06d}"
    conn.execute("UPDATE sec_filings SET accession_number = ? WHERE id = ?", (acc, fid))
    return acc


def test_without_the_table_the_classes_count_one_for_one_and_are_flagged(conn: Database) -> None:
    _visa_like(conn)
    cap = market_caps_as_of(conn, [30], as_of=AS_OF).caps[30]
    assert cap.value == pytest.approx(1_110.0 * 10.0)
    assert cap.incomplete_classes is True


def test_the_filing_s_own_rates_convert_the_classes_and_clear_the_flag(conn: Database) -> None:
    fid = _visa_like(conn)
    conn.execute(TABLE)
    acc = _accession(conn, fid)
    for member, rate in (("us-gaap:CommonClassBMember", 1.5), ("us-gaap:CommonClassCMember", 4.0)):
        conn.execute(
            "INSERT INTO cover_class_conversion VALUES (?, ?, ?, ?, 'as-converted table', 'MKT-03')",
            (acc, member, rate, acc),
        )
    assert queries.cover_class_rates(conn, [fid]) == {
        fid: {"us-gaap:CommonClassBMember": 1.5, "us-gaap:CommonClassCMember": 4.0}
    }
    cap = market_caps_as_of(conn, [30], as_of=AS_OF).caps[30]
    # 1,000 + 100 x 1.5 + 10 x 4.0 = 1,190 class A equivalents at 10.0
    assert cap.share_count == pytest.approx(1_190.0)
    assert cap.value == pytest.approx(11_900.0)
    assert cap.incomplete_classes is False


def test_a_class_with_no_cited_rate_keeps_the_flag(conn: Database) -> None:
    fid = _visa_like(conn)
    conn.execute(TABLE)
    acc = _accession(conn, fid)
    conn.execute(
        "INSERT INTO cover_class_conversion VALUES (?, 'us-gaap:CommonClassBMember', 1.0, ?, 'share for share', 'MKT-03')",
        (acc, acc),
    )
    cap = market_caps_as_of(conn, [30], as_of=AS_OF).caps[30]
    assert cap.value == pytest.approx(1_110.0 * 10.0)
    assert cap.incomplete_classes is True  # class C has no rate: two unconverted classes remain


def test_cover_total_rates_take_precedence_over_the_static_table() -> None:
    entries = [
        ("us-gaap:CommonClassAMember", 10.0, "2026-07-31"),
        ("us-gaap:CommonClassBMember", 100.0, "2026-07-31"),
    ]
    # Berkshire's static 1,500 applies without filing rates and yields to them when given
    assert cover_total(entries, cik="0001067983").value == pytest.approx(15_100.0)  # type: ignore[union-attr]
    assert cover_total(
        entries, cik="0001067983", rates={"us-gaap:CommonClassAMember": 1_000.0}
    ).value == pytest.approx(  # type: ignore[union-attr]
        10_100.0
    )
