"""Point-in-time reads over universe.db + symbol->asset_id resolution."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema.queries import (
    connect_ro,
    members_asof,
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
    assert symbols_asof(c, "2019-05-05") == ["ALWAYS", "LEFT"]  # SAMEDAY excluded


def test_members_asof_dedupes_readded_symbol(universe_db: _UDB) -> None:
    path = universe_db([("RE", "2010-01-01", "2015-01-01"), ("RE", "2020-01-01", None)])
    c = connect_ro(path)
    out = members_asof(c, "2023-01-01")
    assert [m.symbol for m in out] == ["RE"]
    assert out[0].valid_from == "2020-01-01"


def test_members_asof_rejects_non_sp500(universe_db: _UDB) -> None:
    path = universe_db([("A", "2020-01-01", None)])
    c = connect_ro(path)
    with pytest.raises(ValueError, match="single-universe"):
        members_asof(c, "2021-01-01", universe="RUSSELL")


def test_resolve_asset_ids_hits_and_misses() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE)")
    conn.executemany("INSERT INTO assets (id, ticker) VALUES (?, ?)", [(1, "AAPL"), (2, "BRK.B")])
    db = Database(conn)
    mapping, missing = resolve_asset_ids(db, ["aapl", "BRK.B", "NEWCO"])
    assert mapping == {"AAPL": 1, "BRK.B": 2}
    assert missing == ["NEWCO"]
