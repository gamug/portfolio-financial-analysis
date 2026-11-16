"""The leavers' profile rows (T-153 part 3b, D1/D3/D4): type, overlays, interval.

Reads the tracked ``src/kg_schema/data/company_profile.csv``, its reader, and the staged evidence
``docs/leaver_company_types.csv`` (outside ``docs/checklist_sec/``) -- never the checklist's own
table (AGENTS.md). The counts are measured on the delivered ``universe_history_gics.db`` and the
SEC cover pages, so a change to them has to update this test deliberately.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from kg_schema.company_profile import profile_as_of, profiles

STAGED = Path(__file__).resolve().parent.parent / "docs" / "leaver_company_types.csv"
# APP-00 assigns a bank no type without the 10-K's own statement, and its other-financial codes do
# not include the bank codes (40101010/40101015): these two keep no row, declared in
# `docs/model_fixes.md` (T-153 part 3b, reviewer A 2026-10-14).
UNTYPED = {"0001132979": "FRC", "0001288784": "SBNY"}


def _staged() -> dict[str, dict[str, str]]:
    text = STAGED.read_text(encoding="utf-8")
    return {
        row["cik"]: row
        for row in csv.DictReader(line for line in text.splitlines() if not line.startswith("#"))
    }


def test_every_leaver_the_rules_establish_a_type_for_has_a_row() -> None:
    staged = _staged()
    assert len(staged) == 86
    rows = profiles()
    for cik in staged:
        if cik in UNTYPED:
            assert cik not in rows
        else:
            assert cik in rows


def test_a_leaver_keeps_no_row_when_no_cell_of_app00_applies() -> None:
    """SBNY and FRC have no 10-K or 10-Q on EDGAR, so no Article 9 statement exists, and APP-00's
    other-financial list does not include 40101015 -- the type stays unknown."""
    for cik in UNTYPED:
        assert profile_as_of(cik) is None


def test_a_leavers_interval_covers_its_last_membership_day_and_not_the_next() -> None:
    staged, rows = _staged(), profiles()
    for cik, row in staged.items():
        profile = rows.get(cik)
        if profile is None:
            continue
        assert profile.valid_from == "2020-01-01"
        assert (
            profile.valid_to
            == (date.fromisoformat(row["exit_date"]) + timedelta(days=1)).isoformat()
        )
        # The boundary: this file's ``valid_to`` is EXCLUSIVE (`covers` is
        # `valid_from <= day < valid_to`), while the universe file's ``valid_to`` is the last
        # membership day, INCLUSIVE. So the profile covers the last membership day itself and not
        # the day after it -- and the whole of a D-10 filing's period end (from before 2022 to the
        # exit) is covered, which is what the reader asks with.
        assert (
            profile.valid_to
            == (date.fromisoformat(row["exit_date"]) + timedelta(days=1)).isoformat()
        )
        assert profile.covers(row["exit_date"])  # the last membership day
        assert not profile.covers(profile.valid_to)  # the next day
        assert not profile.covers(
            (date.fromisoformat(row["exit_date"]) + timedelta(days=2)).isoformat()
        )
        assert profile.covers(profile.valid_from)


def test_a_current_members_interval_stays_open() -> None:
    for profile in profiles().values():
        if profile.valid_to:
            continue
        assert profile.valid_from == "2020-01-01"
        assert profile.covers("2026-10-14")


def test_the_types_the_reviewer_ruled_on() -> None:
    rows = profiles()
    # Article 9 by the 10-K's own statement, including a company GICS files under Consumer Finance
    assert rows["0000028412"].type == "article_9"  # Comerica
    assert rows["0001393612"].type == "article_9"  # Discover (its own sentence wins)
    assert rows["0000719739"].type == "article_9"  # SVB Financial
    assert rows["0000109380"].type == "article_9"  # Zions, by hand (a national bank)
    # PBCT: "Thrifts & Mortgage Finance" is not in C5's current structure (C5 p.11: the Banks group
    # has 40101010/40101015 only), so the type comes from its own 10-K and no code is guessed
    assert rows["0001378946"].type == "article_9"
    assert rows["0001378946"].sub_industry == "Thrifts & Mortgage Finance"
    assert rows["0001378946"].sector == "Financials"
    # Article 7 by the GICS code (the statement form is confirmed on the ingested facts)
    assert rows["0000059558"].type == "article_7"  # Lincoln National
    # MKTX is an operating company (APP-00 names 40203040)
    assert rows["0001278021"].type == "operating"
    # the REIT overlay, from the delivered label
    for cik in ("0000783280", "0000899689", "0000915912"):  # Duke Realty, Vornado, AvalonBay
        assert rows[cik].reit is True
    # no leaver is in the Utilities sector, so the D-13 overlay matches none
    assert not any(p.regulated_utility for cik, p in rows.items() if p.valid_to)


def test_a_leavers_sector_and_label_come_from_the_delivered_file() -> None:
    staged, rows = _staged(), profiles()
    for cik, row in staged.items():
        profile = rows.get(cik)
        if profile is None:
            continue
        assert profile.sector == (row["gics_sector"] or None)
        assert profile.sub_industry == (row["gics_sub_industry"] or None)


def test_the_multi_class_overlay_comes_from_the_cover_pages_classes() -> None:
    """The **common-equity** classes registered on the last 10-K's cover (L-03/L-11's classification
    in force on the last membership day; MKT-03, C6 p.9, C11), not the submissions file's live
    ticker list and not the cover's notes or preferred series."""
    staged, rows = _staged(), profiles()
    for cik, row in staged.items():
        profile = rows.get(cik)
        if profile is None:
            continue
        assert profile.multi_class == (len(row["cover_classes"].split(" | ")) > 1)
    assert [rows[c].multi_class for c in ("0000012208", "0000024545", "0001336917")] == [True] * 3
    for cik in ("0001755672", "0001437107"):  # CTVA's preferred series, WBD's note series
        assert rows[cik].multi_class is False
