"""The T-153d verify_pilot check: every line's profile and sector (T-153 part 3b).

Two directions: **every line ``members_over_range`` returns** -- a current member, a leaver, and a
company that left and rejoined -- has a profile covering each stored filing's period end, and no
asset the universe file classifies carries no sector (the path from the delivered file to the SECTOR
score) -- with no empty-named ``sectors`` row.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "verify_pilot", Path(__file__).resolve().parent.parent / "scripts" / "verify_pilot.py"
)
assert _SPEC and _SPEC.loader
verify = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_pilot"] = verify
_SPEC.loader.exec_module(verify)

#: CMA (Comerica, CIK 0000028412) left the index on 2024-06-23, so its profile in the tracked file
#: covers [2020-01-01, 2024-06-24). ILMN (CIK 0001110803) left on 2024-06-23 and rejoined on
#: 2026-09-21: it is a member again, so its profile is open -- and its 2022 filings must be covered
#: by it, which is what the widened check is for. ZZZ is a CIK no profile holds.
_UNIVERSE_DDL = """
CREATE TABLE universe_membership (
    symbol TEXT NOT NULL, security TEXT NOT NULL,
    gics_sector TEXT, gics_sub_industry TEXT, hq_location TEXT,
    date_added TEXT, cik TEXT, founded TEXT,
    valid_from TEXT NOT NULL, valid_to TEXT, source TEXT NOT NULL
);
"""
_DDL = """
CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT UNIQUE, cik TEXT, sector_id INTEGER);
CREATE TABLE sectors (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER, period_end TEXT);
"""


def _db(
    tmp_path: Path, *, members: list[tuple[str, str, str, str | None, str | None]]
) -> tuple[Path, Path]:
    """(fin_db, universe_db): *members* are ``(symbol, cik, valid_to, sector, sub_industry)``."""
    udb = tmp_path / "u.db"
    conn = sqlite3.connect(udb)
    conn.executescript(_UNIVERSE_DDL)
    conn.executemany(
        "INSERT INTO universe_membership (symbol, security, gics_sector, gics_sub_industry, cik,"
        " valid_from, valid_to, source) VALUES (?, ?, ?, ?, ?, '1976-07-01', ?, 'test')",
        [(s, f"{s} Inc.", sec, sub, cik, vt) for s, cik, vt, sec, sub in members],
    )
    conn.commit()
    conn.close()
    fin = tmp_path / "f.db"
    conn = sqlite3.connect(fin)
    conn.executescript(_DDL)
    conn.execute("INSERT INTO sectors (id, name) VALUES (1, 'Financials')")
    for i, (symbol, _cik, _vt, sector, _sub) in enumerate(members, 1):
        conn.execute(
            "INSERT INTO assets (id, ticker, cik, sector_id) VALUES (?, ?, ?, ?)",
            (i, symbol, _cik, 1 if sector else None),
        )
    conn.commit()
    conn.close()
    return fin, udb


def _add_stint(udb: Path, row: tuple[str, str, str, str | None, str, str]) -> None:
    """A second stint: ``(symbol, cik, valid_from, valid_to, sector, sub_industry)``."""
    symbol, cik, valid_from, valid_to, sector, sub = row
    conn = sqlite3.connect(udb)
    conn.execute(
        "INSERT INTO universe_membership (symbol, security, gics_sector, gics_sub_industry, cik,"
        " valid_from, valid_to, source) VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        (symbol, f"{symbol} Inc.", sector, sub, cik, valid_from, valid_to),
    )
    conn.commit()
    conn.close()


def _add_filing(fin: Path, asset_id: int, period_end: str) -> None:
    conn = sqlite3.connect(fin)
    conn.execute(
        "INSERT INTO sec_filings (asset_id, period_end) VALUES (?, ?)", (asset_id, period_end)
    )
    conn.commit()
    conn.close()


def test_the_check_passes_when_every_filing_is_covered(tmp_path: Path) -> None:
    """CMA's period ends run from before 2022 to its exit; both are inside its own interval."""
    fin, udb = _db(
        tmp_path,
        members=[
            ("CMA", "0000028412", "2024-06-23", "Financials", "Diversified Banks"),
            ("ZZZ", "9999999999", "2024-06-23", "Financials", "Diversified Banks"),
        ],
    )
    _add_filing(fin, 1, "2021-12-31")
    _add_filing(fin, 1, "2024-03-31")
    assert verify.leaver_profile_problems(str(fin), str(udb)) == (2, 2, [])


def test_the_check_flags_a_filing_after_the_exit(tmp_path: Path) -> None:
    """A filing whose period end is past the leaver's exit has no profile covering it."""
    fin, udb = _db(
        tmp_path, members=[("CMA", "0000028412", "2024-06-23", "Financials", "Diversified Banks")]
    )
    _add_filing(fin, 1, "2024-09-30")
    lines, filings, problems = verify.leaver_profile_problems(str(fin), str(udb))
    assert (lines, filings) == (1, 1)
    assert any("CMA" in p and "no profile covering 2024-09-30" in p for p in problems)


def test_the_check_flags_a_line_with_no_profile_at_all(tmp_path: Path) -> None:
    fin, udb = _db(
        tmp_path, members=[("ZZZ", "9999999999", "2024-06-23", "Financials", "Diversified Banks")]
    )
    _add_filing(fin, 1, "2022-06-30")
    _, _, problems = verify.leaver_profile_problems(str(fin), str(udb))
    assert any("ZZZ" in p and "no profile covering" in p for p in problems)


def test_the_check_flags_an_empty_named_sector(tmp_path: Path) -> None:
    """``_upsert_sector`` must never create one for a leaver the file leaves unclassified."""
    fin, udb = _db(
        tmp_path, members=[("CMA", "0000028412", "2024-06-23", "Financials", "Diversified Banks")]
    )
    conn = sqlite3.connect(fin)
    conn.execute("INSERT INTO sectors (id, name) VALUES (2, '')")
    conn.commit()
    conn.close()
    _, _, problems = verify.leaver_profile_problems(str(fin), str(udb))
    assert any("empty name" in p for p in problems)


def test_the_check_flags_a_classified_line_whose_asset_has_no_sector(tmp_path: Path) -> None:
    """The universe file gives the line a sector, so the asset must carry it (D5's path)."""
    fin, udb = _db(
        tmp_path, members=[("CMA", "0000028412", "2024-06-23", "Financials", "Diversified Banks")]
    )
    conn = sqlite3.connect(fin)
    conn.execute("UPDATE assets SET sector_id = NULL WHERE ticker = 'CMA'")
    conn.commit()
    conn.close()
    _add_filing(fin, 1, "2022-06-30")
    _, _, problems = verify.leaver_profile_problems(str(fin), str(udb))
    assert any("gives it a sector but the asset has none" in p for p in problems)


def test_a_rejoined_member_is_checked_over_its_whole_window(tmp_path: Path) -> None:
    """ILMN left on 2024-06-23 and rejoined on 2026-09-21: one line, an open profile, and its 2022
    filings must be covered by it -- the case the leaver-only check missed."""
    fin, udb = _db(
        tmp_path,
        members=[
            ("ILMN", "0001110803", "2024-06-23", "Health Care", "Life Sciences Tools & Services")
        ],
    )
    _add_stint(
        udb,
        ("ILMN", "0001110803", "2026-09-21", None, "Health Care", "Life Sciences Tools & Services"),
    )
    _add_filing(fin, 1, "2022-03-31")  # its first membership
    _add_filing(fin, 1, "2024-03-31")  # up to its exit
    _add_filing(fin, 1, "2026-06-30")  # after it rejoined
    assert verify.leaver_profile_problems(str(fin), str(udb)) == (1, 3, [])


def test_a_file_with_no_label_asks_nothing_of_the_sector(tmp_path: Path) -> None:
    """A scratch universe that carries no GICS for the line is not a failure: MBC, FTRE and PHIN
    have no label and no filing (the delivered file never listed them)."""
    fin, udb = _db(tmp_path, members=[("MBC", "0001941365", "2022-12-18", None, None)])
    _add_filing(fin, 1, "2022-09-30")  # a filing, but the file carries no label
    assert verify.leaver_profile_problems(str(fin), str(udb)) == (1, 1, [])
