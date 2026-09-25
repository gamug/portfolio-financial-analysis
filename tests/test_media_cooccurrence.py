"""``media_cooccurrence`` (T-043): ``shared_executive_edge``'s exact shape and grain under
another name, so press / analyst co-occurrences (T-082) never mix with executive edges."""

from __future__ import annotations

import sqlite3

import pytest
from portfolio_common.db import Database

import kg_schema


def _columns(conn: Database, table: str) -> list[tuple[str, str, int]]:
    return [
        (r["name"], r["type"], r["notnull"])
        for r in conn.execute('SELECT name, type, "notnull" FROM pragma_table_info(?)', (table,))
    ]


def _indexes(conn: Database, table: str) -> set[str]:
    return {
        str(r["name"])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
            "AND name NOT LIKE 'sqlite_autoindex%'",
            (table,),
        )
    }


def test_ensure_creates_it_with_its_indexes_and_a_second_run_is_a_no_op(
    memory_db: Database,
) -> None:
    kg_schema.ensure(memory_db)  # memory_db already ran it once: this is the second run
    assert _indexes(memory_db, "media_cooccurrence") == {"ix_media_cooc_a", "ix_media_cooc_b"}


def test_it_has_shared_executive_edge_s_exact_columns(memory_db: Database) -> None:
    assert _columns(memory_db, "media_cooccurrence") == _columns(memory_db, "shared_executive_edge")


def test_its_key_is_pair_person_method(memory_db: Database) -> None:
    conn = memory_db
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AA'), (2, 'BB')")
    insert = (
        "INSERT INTO media_cooccurrence (asset_id_a, asset_id_b, person_name, article_count_a, "
        "article_count_b, weight, method, computed_at) VALUES (1, 2, ?, 3, 4, 0.5, ?, 'now')"
    )
    conn.execute(insert, ("Jane Analyst", "news-per-cooccurrence-v1"))
    conn.execute(insert, ("Jane Analyst", "news-per-cooccurrence-v2"))  # another method
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("Jane Analyst", "news-per-cooccurrence-v1"))
    # and it stays out of the executive edges
    assert conn.execute("SELECT COUNT(*) FROM shared_executive_edge").fetchone()[0] == 0
