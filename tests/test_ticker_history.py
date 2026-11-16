"""T-153 part 2: a renamed company stays one asset (checklist L-11 / APP-10a).

The tracked ``data/ticker_history.csv`` is the source of truth; the CIK is the identity, so a
symbol is resolved by its CIK (a reused ticker is never merged), and ``sync_universe`` stores a
member under its line's canonical symbol -- inserting, adopting a recorded rename, or refusing
an undeclared split before anything is written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database

import kg_schema
from fundamental_agent import db as fundamental_db
from kg_schema import ticker_history
from kg_schema.queries import UniverseMember
from pricing_agent import db as pricing_db


def _member(symbol: str, cik: str | None) -> UniverseMember:
    return UniverseMember(
        symbol, f"{symbol} Inc.", cik, "Tech", "Software", None, None, None, "2020-01-01", None
    )


def _bare_db() -> Database:
    """An in-memory DB with ``assets`` and the shared schema ensured (so ``ticker_history`` is
    seeded) but no rows."""
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT)")
    conn.commit()
    kg_schema.ensure(conn)
    return conn


def _asset(conn: Database, asset_id: int, ticker: str, cik: str) -> None:
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)", (asset_id, ticker, cik))
    conn.commit()


# -- the file (the source of truth) -------------------------------------------------------


def test_the_file_cites_every_row_and_names_the_canonical_line() -> None:
    rows = {r.symbol: r for r in ticker_history.read_history()}
    assert len(rows) == 21
    assert rows["FB"].canonical_symbol == "META"
    assert (rows["FB"].valid_to, rows["META"].valid_from) == ("2022-06-08", "2022-06-09")
    assert rows["DISCA"].canonical_symbol == "WBD"  # SA-1: Series A continues
    assert rows["DISCK"].canonical_symbol == "DISCK"  # Series C ends like a leaver
    assert rows["PSKY"].canonical_symbol == "SKYD"
    assert rows["PSKY"].valid_from == "2025-08-07"  # SA-2: PSKY's first day, never earlier
    # D4 (part 3a): the post-exit renames -- GPS continues as GAP, FBHS as FBIN.
    assert rows["GPS"].canonical_symbol == "GAP"
    assert (rows["GPS"].valid_to, rows["GAP"].valid_from) == ("2024-08-21", "2024-08-22")
    assert rows["FBHS"].canonical_symbol == "FBIN"
    assert (rows["FBHS"].valid_to, rows["FBIN"].valid_from) == ("2022-12-14", "2022-12-15")
    for row in rows.values():
        assert row.evidence_url.startswith("http")
        assert row.evidence_date


def test_seed_makes_the_table_equal_the_file(tmp_path: Path) -> None:
    """The file is the source of truth (F5): a changed row is updated, a removed row is deleted."""
    conn = _bare_db()
    assert ticker_history.seed(conn) == 21
    rows = ticker_history.read_history()
    lines = ["cik,symbol,valid_from,valid_to,canonical_symbol,evidence_url,evidence_date,rules"]
    for r in rows:
        if r.symbol == "WBD":  # a row removed from the file
            continue
        canonical = "META2" if r.symbol == "FB" else r.canonical_symbol  # the next rename lands
        valid_to = "2022-06-07" if r.symbol == "FB" else (r.valid_to or "")
        lines.append(
            f"{r.cik},{r.symbol},{r.valid_from},{valid_to},{canonical},"
            f"{r.evidence_url},{r.evidence_date},{r.rules}"
        )
    path = tmp_path / "ticker_history.csv"
    path.write_text("\n".join(lines) + "\n")

    assert ticker_history.seed(conn, path) == 20
    stored = {
        str(r["symbol"]): (str(r["canonical_symbol"]), r["valid_to"])
        for r in conn.execute("SELECT symbol, canonical_symbol, valid_to FROM ticker_history")
    }
    assert len(stored) == 20
    assert stored["FB"] == ("META2", "2022-06-07")  # the changed row took the file's value
    assert "WBD" not in stored  # the deleted row is gone
    assert ticker_history.seed(conn, path) == 20  # a re-run on the same file changes nothing


def test_the_line_is_resolved_by_cik_and_dated_by_the_symbol() -> None:
    conn = _bare_db()
    assert ticker_history.canonical_symbol(conn, "0001326801", "FB") == "META"
    assert ticker_history.canonical_symbol(conn, "0001174610", "FB") is None  # ProShares today
    assert ticker_history.symbol_line(conn, "FB", "2022-05-02") == ("0001326801", "META")
    assert ticker_history.symbol_line(conn, "FB", "2026-01-02") is None  # never the old line
    assert ticker_history.line_symbols(conn, "0001437107", "WBD") == {"DISCA", "WBD"}


# -- planning: insert / share class / adopt / refuse (C3, C4) ------------------------------


def test_plan_inserts_the_line_canonical_then_refreshes_it() -> None:
    conn = _bare_db()
    (plan,) = ticker_history.plan_members(conn, [("FB", "0001326801")])
    assert plan == ticker_history.MemberAsset("FB", "0001326801", "META", "insert", None)
    _asset(conn, 7, "META", "0001326801")
    (plan,) = ticker_history.plan_members(conn, [("FB", "0001326801")])
    assert plan == ticker_history.MemberAsset("FB", "0001326801", "META", "refresh", 7)


def test_plan_makes_share_classes_of_one_cik_two_assets() -> None:
    conn = _bare_db()
    plans = ticker_history.plan_members(conn, [("DISCA", "0001437107"), ("DISCK", "0001437107")])
    assert plans == [
        ticker_history.MemberAsset("DISCA", "0001437107", "WBD", "insert", None),
        ticker_history.MemberAsset("DISCK", "0001437107", "DISCK", "insert", None),
    ]


def test_plan_adopts_a_recorded_rename() -> None:
    conn = _bare_db()
    _asset(conn, 11, "FB", "0001326801")
    (plan,) = ticker_history.plan_members(conn, [("META", "0001326801")])
    assert plan == ticker_history.MemberAsset("META", "0001326801", "META", "adopt", 11, "FB")


def test_plan_refuses_an_undeclared_split() -> None:
    conn = _bare_db()
    _asset(conn, 12, "PSKY", "0002041610")
    with pytest.raises(ticker_history.TickerConflict) as info:
        ticker_history.plan_members(conn, [("SKYQ", "0002041610")])
    message = str(info.value)
    assert "SKYQ" in message and "0002041610" in message and "PSKY" in message


def test_plan_refuses_when_the_canonical_and_an_old_symbol_both_exist() -> None:
    """F4: a database with both FB and META is refused, naming both, not silently refreshed."""
    conn = _bare_db()
    _asset(conn, 1, "FB", "0001326801")
    _asset(conn, 2, "META", "0001326801")
    with pytest.raises(ticker_history.TickerConflict) as info:
        ticker_history.plan_members(conn, [("META", "0001326801")])
    message = str(info.value)
    assert "asset 2 (META)" in message and "1 (FB)" in message


def test_plan_refuses_a_third_symbol_of_a_multi_class_cik() -> None:
    """F6: GOOGL + GOOX with a stale GOOG is not a share class -- GOOX is refused, not inserted."""
    conn = _bare_db()
    _asset(conn, 1, "GOOGL", "0001652044")
    _asset(conn, 2, "GOOG", "0001652044")
    with pytest.raises(ticker_history.TickerConflict):
        ticker_history.plan_members(conn, [("GOOGL", "0001652044"), ("GOOX", "0001652044")])


def test_plan_still_inserts_a_genuine_share_class() -> None:
    """The narrowing keeps the real case: GOOG arrives beside its own line's GOOGL asset."""
    conn = _bare_db()
    _asset(conn, 1, "GOOGL", "0001652044")
    plans = ticker_history.plan_members(conn, [("GOOG", "0001652044"), ("GOOGL", "0001652044")])
    assert [p.action for p in plans] == ["insert", "refresh"]


# -- sync_universe ------------------------------------------------------------------------


@pytest.mark.parametrize("sync", [fundamental_db.sync_universe, pricing_db.sync_universe])
def test_sync_is_one_asset_across_a_rename(memory_db: Database, sync: object) -> None:
    sync(memory_db, [_member("FB", "0001326801")])  # type: ignore[operator]
    sync(memory_db, [_member("META", "0001326801")])  # type: ignore[operator]
    rows = [(r["ticker"], r["cik"]) for r in memory_db.execute("SELECT ticker, cik FROM assets")]
    assert rows == [("META", "0001326801")]


@pytest.mark.parametrize("sync", [fundamental_db.sync_universe, pricing_db.sync_universe])
def test_sync_adopts_and_reports_a_recorded_rename(memory_db: Database, sync: object) -> None:
    memory_db.execute(
        "INSERT INTO assets (ticker, company_name, cik) VALUES ('FB', 'Facebook', '0001326801')"
    )
    memory_db.commit()
    asset_id = int(memory_db.execute("SELECT id FROM assets").fetchone()["id"])
    result = sync(memory_db, [_member("META", "0001326801")])  # type: ignore[operator]
    row = memory_db.execute("SELECT id, ticker, company_name, cik FROM assets").fetchone()
    assert (row["id"], row["ticker"], row["cik"]) == (asset_id, "META", "0001326801")
    # F7: the ordinary upsert runs after the retitle, so the member's name/sector refresh too
    assert row["company_name"] == "META Inc."
    assert result.adoptions == [ticker_history.Adoption("FB", "META", asset_id, "0001326801")]


@pytest.mark.parametrize("sync", [fundamental_db.sync_universe, pricing_db.sync_universe])
def test_sync_refuses_an_undeclared_split_before_writing(memory_db: Database, sync: object) -> None:
    memory_db.execute("INSERT INTO assets (ticker, cik) VALUES ('PSKY', '0002041610')")
    memory_db.commit()
    with pytest.raises(ticker_history.TickerConflict):
        sync(memory_db, [_member("SKYQ", "0002041610")])  # type: ignore[operator]
    rows = {r["ticker"] for r in memory_db.execute("SELECT ticker FROM assets")}
    assert rows == {"PSKY"}
