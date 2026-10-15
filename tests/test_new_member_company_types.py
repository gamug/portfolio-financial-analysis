"""The later joiners' staging (T-153 part 3b): the open stints the checklist's table does not hold.

Reads a small universe fixture and a table fixture -- never ``docs/checklist_sec/`` (AGENTS.md).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "new_member_company_types",
    Path(__file__).resolve().parent.parent / "scripts" / "new_member_company_types.py",
)
assert _SPEC and _SPEC.loader
staging = importlib.util.module_from_spec(_SPEC)
sys.modules["new_member_company_types"] = staging
_SPEC.loader.exec_module(staging)

_DDL = """
CREATE TABLE universe_membership (
    symbol TEXT NOT NULL, security TEXT NOT NULL,
    gics_sector TEXT, gics_sub_industry TEXT, hq_location TEXT,
    date_added TEXT, cik TEXT, founded TEXT,
    valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL
);
"""
_TABLE = (
    "cik,tickers,company_name,gics_sector,gics_sub_industry,gics_code,type,financial_firm,basis,"
    "evidence_accession,evidence_quote,reit,negative_equity_latest,multi_class_listed,needs_override\n"
    "0000000001,AAA,Alpha,Industrials,Machinery,,operating,False,x,,,False,False,False,False\n"
)


def _universe(tmp_path: Path) -> Path:
    path = tmp_path / "u.db"
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO universe_membership (symbol, security, gics_sector, gics_sub_industry, cik,"
        " valid_from, valid_to, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'live_snapshot')",
        [
            # the table already holds it
            ("AAA", "Alpha Inc.", "Industrials", "Machinery", "0000000001", "1976-07-01", None),
            # joined after the table was generated
            (
                "BBB",
                "Beta Inc.",
                "Health Care",
                "Life Sciences Tools & Services",
                "0000000002",
                "2026-09-21",
                None,
            ),
            # a leaver: closed, so not an open member
            (
                "CCC",
                "Gamma Inc.",
                "Energy",
                "Oil & Gas Exploration & Production",
                "0000000003",
                "1976-07-01",
                "2024-06-23",
            ),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_only_the_open_stints_the_table_does_not_hold(tmp_path: Path) -> None:
    """TWLO, VYLR, BE, P and ILMN measured: an open stint whose CIK the table predates."""
    table = tmp_path / "t.csv"
    table.write_text(_TABLE)
    udb = _universe(tmp_path)
    known = staging.table_ciks(str(table))
    assert known == {"0000000001"}
    conn = sqlite3.connect(udb)
    conn.row_factory = sqlite3.Row
    rows = staging.open_members(conn, known)
    conn.close()
    assert [r["symbols"] for r in rows] == ["BBB"]
    assert rows[0]["joined_date"] == "2026-09-21"
    assert rows[0]["gics_sub_industry"] == "Life Sciences Tools & Services"


def test_the_staged_fields() -> None:
    assert staging.FIELDS == [
        "cik",
        "symbols",
        "company_name",
        "joined_date",
        "gics_sector",
        "gics_sub_industry",
        "last_10k_accession",
        "last_10k_filed",
        "article_9",
        "evidence_quote",
        "live_tickers",
        "note",
    ]


def test_the_header_names_the_generator() -> None:
    header = staging.dump([]).splitlines()[0]
    assert "scripts/new_member_company_types.py" in header
