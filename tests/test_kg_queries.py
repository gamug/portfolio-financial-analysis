"""Point-in-time reads over universe.db + symbol->asset_id resolution."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database

import kg_schema
from kg_schema.queries import (
    WINDOW_START,
    connect_ro,
    members_asof,
    members_over_range,
    resolve_asset_ids,
    symbols_asof,
)

_UDB = Callable[..., Path]


def test_members_asof_is_point_in_time(universe_db: _UDB) -> None:
    path = universe_db(
        [
            ("ALWAYS", "2015-01-01", None),
            ("JOINED", "2022-06-01", None),
            ("LEFT", "2015-01-01", "2021-03-01"),
            ("SAMEDAY", "2019-05-05", "2019-05-05"),  # add + remove same day
        ]
    )
    c = connect_ro(path)
    assert symbols_asof(c, "2020-01-01") == ["ALWAYS", "LEFT"]
    assert symbols_asof(c, "2023-01-01") == ["ALWAYS", "JOINED"]
    assert symbols_asof(c, "2019-05-05") == [
        "ALWAYS",
        "LEFT",
        "SAMEDAY",
    ]  # a one-day stint: its own day
    assert symbols_asof(c, "2019-05-06") == ["ALWAYS", "LEFT"]  # and not the next


def test_members_asof_dedupes_readded_symbol(universe_db: _UDB) -> None:
    path = universe_db([("RE", "2010-01-01", "2015-01-01"), ("RE", "2020-01-01", None)])
    c = connect_ro(path)
    out = members_asof(c, "2023-01-01")
    assert [m.symbol for m in out] == ["RE"]
    assert out[0].valid_from == "2020-01-01"


def test_the_last_day_of_a_stint_is_a_member_day(universe_db: _UDB) -> None:
    """``valid_to`` is the last day of membership -- the removal takes effect the next day."""
    path = universe_db([("LEFT", "2015-01-01", "2021-03-01")])
    c = connect_ro(path)
    assert [m.symbol for m in members_asof(c, "2021-03-01")] == ["LEFT"]
    assert members_asof(c, "2021-03-02") == []


def test_a_one_day_stint_is_a_member_on_that_day_only(universe_db: _UDB) -> None:
    """PHIN was a member for one day only (2023-07-05): in that day, out on either side."""
    path = universe_db([("PHIN", "2023-07-05", "2023-07-05")])
    c = connect_ro(path)
    assert [m.symbol for m in members_asof(c, "2023-07-05")] == ["PHIN"]
    assert members_asof(c, "2023-07-04") == []
    assert members_asof(c, "2023-07-06") == []


def test_a_rename_has_exactly_one_symbol_on_each_day(universe_db: _UDB) -> None:
    """Old symbol to D, new symbol from D+1, one CIK (FB -> META: 2022-06-08 / 2022-06-09)."""
    path = universe_db([("FB", "2013-12-23", "2022-06-08"), ("META", "2022-06-09", None)])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE universe_membership SET cik = '0001326801'")
    conn.commit()
    conn.close()
    c = connect_ro(path)
    assert [(m.symbol, m.cik) for m in members_asof(c, "2022-06-08")] == [("FB", "0001326801")]
    assert [(m.symbol, m.cik) for m in members_asof(c, "2022-06-09")] == [("META", "0001326801")]


def test_the_latest_valid_from_wins_for_overlapping_stints(universe_db: _UDB) -> None:
    """The file should not overlap; if it ever does, the latest ``valid_from`` is the stint in force."""
    path = universe_db([("OVER", "2020-01-01", "2023-12-31"), ("OVER", "2022-01-01", None)])
    c = connect_ro(path)
    (covered,) = members_asof(c, "2023-06-30")
    assert covered.valid_from == "2022-01-01"
    assert members_asof(c, "2021-06-30")[0].valid_from == "2020-01-01"


def test_members_asof_rejects_non_sp500(universe_db: _UDB) -> None:
    path = universe_db([("A", "2020-01-01", None)])
    c = connect_ro(path)
    with pytest.raises(ValueError, match="single-universe"):
        members_asof(c, "2021-01-01", universe="RUSSELL")


# -- members_over_range: the window's ingestion set (T-153 part 3a) -------------------------


def _window_db(
    tmp_path: Path, rows: list[tuple[str, str, str | None]], ciks: dict[str, str]
) -> Path:
    """A universe file whose named symbols carry the CIKs the tracked ticker history knows."""
    path = write_universe_db(tmp_path / "u.db", rows)
    raw = sqlite3.connect(path)
    for symbol, cik in ciks.items():
        raw.execute("UPDATE universe_membership SET cik = ? WHERE symbol = ?", (cik, symbol))
    raw.commit()
    raw.close()
    return path


def test_members_over_range_collapses_a_line_and_keeps_share_classes(tmp_path: Path) -> None:
    """One entry per CIK line: DISCA and WBD are one line, DISCK another (T-153 part 3a)."""
    path = _window_db(
        tmp_path,
        [
            ("DISCA", "1976-07-01", "2022-04-10"),
            ("DISCK", "2014-08-06", "2022-04-10"),
            ("WBD", "2022-04-11", "2026-10-05"),
        ],
        {"DISCA": "0001437107", "DISCK": "0001437107", "WBD": "0001437107"},
    )
    conn = connect_ro(path)
    try:
        out = members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14")
    finally:
        conn.close()
    by_symbol = {w.member.symbol: w for w in out}
    assert set(by_symbol) == {"WBD", "DISCK"}
    assert (by_symbol["WBD"].window_from, by_symbol["WBD"].window_to, by_symbol["WBD"].stints) == (
        "2022-01-03",
        "2026-10-05",
        2,
    )
    assert (by_symbol["DISCK"].window_to, by_symbol["DISCK"].stints) == ("2022-04-10", 1)


def test_members_over_range_clamps_and_keeps_an_open_stint(tmp_path: Path) -> None:
    """A rename's line: FB and META collapse to META, clamped to the window and still open."""
    path = _window_db(
        tmp_path,
        [("FB", "2013-12-23", "2022-06-08"), ("META", "2022-06-09", None)],
        {"FB": "0001326801", "META": "0001326801"},
    )
    conn = connect_ro(path)
    try:
        (w,) = members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14")
    finally:
        conn.close()
    assert (w.member.symbol, w.window_from, w.window_to, w.stints) == (
        "META",
        "2022-01-03",
        None,
        2,
    )


def test_members_over_range_collapses_cday_to_day(tmp_path: Path) -> None:
    """CDAY -> DAY is one line, so one entry with two stints (T-153 part 3a)."""
    path = _window_db(
        tmp_path,
        [("CDAY", "2021-09-20", "2024-01-31"), ("DAY", "2024-02-01", "2026-02-08")],
        {"CDAY": "0001725057", "DAY": "0001725057"},
    )
    conn = connect_ro(path)
    try:
        (w,) = members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14")
    finally:
        conn.close()
    assert (w.member.symbol, w.window_from, w.window_to, w.stints) == (
        "DAY",
        "2022-01-03",
        "2026-02-08",
        2,
    )


def test_members_over_range_excludes_a_pre_window_leaver(tmp_path: Path) -> None:
    """A stint that ended before the window's first session is not in the set."""
    path = _window_db(tmp_path, [("OLD", "2010-01-01", "2021-12-31")], {})
    conn = connect_ro(path)
    try:
        assert members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14") == []
    finally:
        conn.close()


def test_members_over_range_keeps_a_one_day_leaver(tmp_path: Path) -> None:
    path = _window_db(tmp_path, [("PHIN", "2023-07-05", "2023-07-05")], {})
    conn = connect_ro(path)
    try:
        (w,) = members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14")
    finally:
        conn.close()
    assert (w.member.symbol, w.window_from, w.window_to) == ("PHIN", "2023-07-05", "2023-07-05")


def test_members_over_range_keeps_a_cikless_member_its_own_line(tmp_path: Path) -> None:
    path = _window_db(tmp_path, [("AAA", "2020-01-01", None)], {})
    raw = sqlite3.connect(path)
    raw.execute("UPDATE universe_membership SET cik = NULL")
    raw.commit()
    raw.close()
    conn = connect_ro(path)
    try:
        (w,) = members_over_range(conn, _identity_db([]), WINDOW_START, "2026-10-14")
    finally:
        conn.close()
    assert (w.member.symbol, w.member.cik, w.window_to) == ("AAA", None, None)


def _identity_db(assets: list[tuple[int, str, str | None]]) -> Database:
    """An in-memory DB with ``assets`` (id, ticker, cik) and the shared schema ensured, so the
    seeded ``ticker_history`` is present (T-153 part 2)."""
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT)")
    conn.executemany("INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)", assets)
    conn.commit()
    kg_schema.ensure(conn)
    return conn


def test_resolve_asset_ids_hits_and_misses() -> None:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = Database(raw)
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT)")
    conn.executemany(
        "INSERT INTO assets (id, ticker, cik) VALUES (?, ?, ?)",
        [(1, "AAPL", None), (2, "BRK.B", None)],
    )
    mapping, missing = resolve_asset_ids(
        conn, [("aapl", None), ("BRK.B", None), ("NEWCO", "0000000001")], as_of="2024-01-02"
    )
    assert mapping == {"AAPL": 1, "BRK.B": 2}
    assert missing == ["NEWCO"]


def test_resolve_asset_ids_maps_a_renames_old_symbol_by_its_cik() -> None:
    """FB (CIK 0001326801) is META's line: on a 2022 date it is the one asset (T-153 part 2)."""
    db = _identity_db([(7, "META", "0001326801")])
    assert resolve_asset_ids(db, [("FB", "0001326801")], as_of="2022-05-02") == ({"FB": 7}, [])
    assert resolve_asset_ids(db, [("META", "0001326801")], as_of="2022-06-09") == ({"META": 7}, [])


def test_resolve_asset_ids_never_merges_a_reused_ticker() -> None:
    """The symbol ``FB`` is ProShares Trust (CIK 0001174610) after the rename: a different CIK
    on a different asset is MISSING, never merged; the Meta member still resolves."""
    db = _identity_db([(7, "META", "0001326801"), (8, "FB", "0001326801")])
    assert resolve_asset_ids(db, [("FB", "0001174610")], as_of="2026-01-02") == ({}, ["FB"])
    assert resolve_asset_ids(db, [("FB", "0001326801")], as_of="2022-05-02") == ({"FB": 7}, [])


def test_resolve_asset_ids_resolves_each_echo_by_its_cik() -> None:
    """Two CIKs that share the symbol ``ECHO`` never merge (T-153 part 2)."""
    db = _identity_db([(3, "ECHO", "0001415404")])
    assert resolve_asset_ids(db, [("ECHO", "0001415404")], as_of="2026-07-01") == ({"ECHO": 3}, [])
    assert resolve_asset_ids(db, [("ECHO", "0001234567")], as_of="2020-06-01") == ({}, ["ECHO"])


def test_resolve_asset_ids_without_a_cik_matches_the_ticker_alone() -> None:
    """A member with no CIK keeps today's behaviour: the bare ticker decides."""
    db = _identity_db([(7, "META", "0001326801")])
    assert resolve_asset_ids(db, [("META", None)], as_of="2026-01-02") == ({"META": 7}, [])
