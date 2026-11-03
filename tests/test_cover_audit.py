"""The cover-count audit layer (L-43, F-A12, D-20): the three seed files become tables on every ``ensure``,
and a filing's cover rows are corrected as they are written, each correction logged with its EDGAR page."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from conftest import seed_filing
from portfolio_common.db import Database

from fundamental_agent import db as fa_db
from fundamental_agent.statements import CoverShares
from kg_schema import cover_audit

DATA = Path(cover_audit.__file__).resolve().parent / "data"
SNDK = (
    "0002023554-25-000004"  # 10-Q cover printed 114,863,251; the 10-Q/A corrects it to 144,863,251
)
SHELL = "0001193125-20-140498"  # a shell's placeholder cover of 100 shares (MKT-04)


@pytest.fixture
def conn(memory_quant_db: Database) -> Database:
    return memory_quant_db


def _count(conn: Database, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])  # noqa: S608


def _filing(conn: Database, accession: str, asset_id: int = 1) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO assets (id, ticker, cik) VALUES (?, ?, ?)",
        (asset_id, f"T{asset_id}", f"000100000{asset_id}"),
    )
    fid = seed_filing(
        conn,
        asset_id,
        period_end="2025-03-31",
        filing_date="2025-04-30",
        form="10-Q",
        fiscal_period="2025Q1",
    )
    conn.execute("UPDATE sec_filings SET accession_number = ? WHERE id = ?", (accession, fid))
    return fid


def test_the_seed_files_load_into_their_tables_and_reseeding_adds_nothing(conn: Database) -> None:
    with (DATA / "cover_class_conversion.csv").open(newline="") as f:
        expected = len(list(csv.DictReader(f)))
    assert _count(conn, "cover_class_conversion") == expected == 861
    cover_audit.ensure_seeded(conn)
    assert _count(conn, "cover_class_conversion") == expected


def test_a_prior_cover_is_seeded_for_a_company_the_database_holds(conn: Database) -> None:
    with (DATA / "audit_prior_cover_shares.csv").open(newline="") as f:
        first = next(csv.DictReader(f))
    conn.execute("INSERT INTO assets (id, ticker, cik) VALUES (77, 'PRIOR', ?)", (first["cik"],))
    cover_audit.ensure_seeded(conn)
    cover_audit.ensure_seeded(conn)
    row = conn.execute(
        "SELECT value, available_at FROM audit_prior_cover_shares WHERE asset_id = 77 AND sec_accession = ?",
        (first["sec_accession"],),
    ).fetchone()
    assert row["value"] == float(first["value"])
    assert _count(conn, "audit_prior_cover_shares") >= 1


def test_a_cover_the_filing_prints_differently_is_corrected_and_logged(conn: Database) -> None:
    fid = _filing(conn, SNDK)
    cover = CoverShares(class_member="", value=114_863_251.0, as_of_date="2025-02-27")
    fa_db.insert_cover_shares(conn, fid, [cover], event_time="2025-03-31")
    row = conn.execute(
        "SELECT value FROM filing_cover_shares WHERE filing_id = ?", (fid,)
    ).fetchone()
    assert row["value"] == 144_863_251.0
    log = conn.execute(
        "SELECT * FROM audit_cover_corrections WHERE accession = ?", (SNDK,)
    ).fetchall()
    assert len(log) == 1
    assert log[0]["old_value"] == 114_863_251.0
    assert log[0]["source_accession"] == "0002023554-25-000016"
    assert log[0]["edgar_page"].startswith("https://www.sec.gov/Archives/edgar/data/")
    fa_db.insert_cover_shares(
        conn, fid, [cover], event_time="2025-03-31"
    )  # a re-run changes nothing
    assert _count(conn, "audit_cover_corrections") == 1 + _count_seeded_prior(conn)


def _count_seeded_prior(conn: Database) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM audit_cover_corrections WHERE table_name = 'audit_prior_cover_shares'"
        ).fetchone()["n"]
    )


def test_a_shell_s_placeholder_cover_is_removed(conn: Database) -> None:
    fid = _filing(conn, SHELL, asset_id=2)
    fa_db.insert_cover_shares(
        conn,
        fid,
        [CoverShares(class_member="", value=100.0, as_of_date="2020-05-11")],
        event_time="2020-03-31",
    )
    assert _count(conn, "filing_cover_shares") == 0
    assert (
        conn.execute(
            "SELECT action FROM audit_cover_corrections WHERE accession = ?", (SHELL,)
        ).fetchone()["action"]
        == "delete"
    )


def test_a_count_that_is_not_the_recorded_one_is_left_alone(conn: Database) -> None:
    fid = _filing(conn, SNDK)
    fa_db.insert_cover_shares(
        conn,
        fid,
        [CoverShares(class_member="", value=144_863_251.0, as_of_date="2025-02-27")],
        event_time="2025-03-31",
    )
    assert (
        conn.execute(
            "SELECT value FROM filing_cover_shares WHERE filing_id = ?", (fid,)
        ).fetchone()["value"]
        == 144_863_251.0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM audit_cover_corrections WHERE accession = ?", (SNDK,)
        ).fetchone()["n"]
        == 0
    )
