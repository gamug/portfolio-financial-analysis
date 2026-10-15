"""The company-profile generator's staged merges (T-153 part 3b, D1 option (b)).

The table's rows keep their one fixed interval; a **leaver** row is built from the staged evidence
(its last-day label, its Article 9 statement, its cover-page classes) with its own interval; a
**later joiner** (a member the table predates) is built under the current-member rule (its open
stint's label, its live tickers, an open interval); a bank label with no Article 9 statement gets no
row at all.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_company_profile",
    Path(__file__).resolve().parent.parent / "scripts" / "build_company_profile.py",
)
assert _SPEC and _SPEC.loader
build_profile = importlib.util.module_from_spec(_SPEC)
sys.modules["build_company_profile"] = build_profile
_SPEC.loader.exec_module(build_profile)

_SOURCE = (
    "cik,tickers,company_name,gics_sector,gics_sub_industry,gics_code,type,financial_firm,basis,"
    "evidence_accession,evidence_quote,reit,negative_equity_latest,multi_class_listed,needs_override\n"
    "0000000001,AAA,Alpha,Industrials,Machinery,,operating,False,GICS sector Industrials,,,"
    "False,False,False,False\n"
    "0000000002,BBB,Beta,Real Estate,Industrial REITs,,operating,False,GICS sector Real Estate,,,"
    "True,False,False,False\n"
)
_HEADER = (
    "cik,symbols,company_name,exit_date,gics_sector,gics_sub_industry,gics_revision_id,"
    "gics_revision_timestamp,last_10k_accession,last_10k_filed,article_9,evidence_quote,"
    "cover_symbols,note\n"
)


def _leaver(row: dict[str, str]) -> str:
    """One staged leaver row; *row* carries cik, symbols, exit_date, sector, sub and any override."""
    fields = {"article_9": "false", "quote": "", "cover": "X", **row}
    return (
        f"{fields['cik']},{fields['symbols']},{fields['symbols']} Inc.,{fields['exit_date']},"
        f"{fields['sector']},{fields['sub']},123,2026-01-01T00:00:00Z,0000-00-000000,2024-02-01,"
        f'{fields["article_9"]},"{fields["quote"]}",{fields["cover"]},\n'
    )


def _rows(text: str) -> list[dict[str, str]]:
    return list(
        csv.DictReader(
            io.StringIO("\n".join(line for line in text.splitlines() if not line.startswith("#")))
        )
    )


def test_the_current_rows_keep_the_tables_fixed_interval() -> None:
    out = build_profile.build(_SOURCE, _HEADER)
    rows = _rows(out)
    assert len(rows) == 2
    assert all(r["valid_from"] == "2020-01-01" and r["valid_to"] == "" for r in rows)
    assert rows[0]["regulated_utility"] == "False"


def test_a_leaver_row_carries_its_own_interval_and_type() -> None:
    leavers = _HEADER + _leaver(
        {
            "cik": "0000000003",
            "symbols": "CCC",
            "exit_date": "2024-06-23",
            "sector": "Financials",
            "sub": "Diversified Banks",
            "article_9": "true",
            "quote": "CCC is a bank holding company",
        }
    )
    rows = _rows(build_profile.build(_SOURCE, leavers))
    ccc = next(r for r in rows if r["cik"] == "0000000003")
    assert ccc["type"] == "article_9" and ccc["financial_firm"] == "True"
    assert ccc["gics_code"] == "40101010"
    assert ccc["valid_from"] == "2020-01-01" and ccc["valid_to"] == "2024-06-24"
    assert ccc["evidence_accession"] == "0000-00-000000"
    assert ccc["evidence_quote"] == "CCC is a bank holding company"


def test_a_bank_label_with_no_article_9_statement_gets_no_row() -> None:
    """APP-00 assigns no type: the Article 9 row needs the 10-K's statement, and the other-financial
    list does not include the bank codes (40101010/40101015) -- SBNY and FRC, declared."""
    leavers = _HEADER + _leaver(
        {
            "cik": "0000000004",
            "symbols": "DDD",
            "exit_date": "2023-03-14",
            "sector": "Financials",
            "sub": "Regional Banks",
        }
    )
    out = build_profile.build(_SOURCE, leavers)
    assert "0000000004" not in {r["cik"] for r in _rows(out)}
    assert build_profile.replaced_ciks(_SOURCE, leavers) == []


def test_an_insurer_is_article_7_without_claiming_a_statement_form() -> None:
    leavers = _HEADER + _leaver(
        {
            "cik": "0000000005",
            "symbols": "EEE",
            "exit_date": "2023-09-17",
            "sector": "Financials",
            "sub": "Multi-line Insurance",
        }
    )
    eee = next(r for r in _rows(build_profile.build(_SOURCE, leavers)) if r["cik"] == "0000000005")
    assert eee["type"] == "article_7" and eee["gics_code"] == "40301030"
    assert "no facts for a leaver here" in eee["basis"]


def test_a_leaver_already_in_the_table_replaces_its_row() -> None:
    """A company that left after the table was generated is still in it: its own interval wins."""
    leavers = _HEADER + _leaver(
        {
            "cik": "0000000002",
            "symbols": "BBB",
            "exit_date": "2026-09-20",
            "sector": "Real Estate",
            "sub": "Office REITs",
        }
    )
    out = build_profile.build(_SOURCE, leavers)
    rows = [r for r in _rows(out) if r["cik"] == "0000000002"]
    assert len(rows) == 1
    assert rows[0]["valid_to"] == "2026-09-21"
    assert rows[0]["gics_sub_industry"] == "Office REITs"
    assert build_profile.replaced_ciks(_SOURCE, leavers) == ["0000000002"]


def test_the_multi_class_overlay_comes_from_the_cover_symbols() -> None:
    one = _HEADER + _leaver(
        {
            "cik": "0000000006",
            "symbols": "FFF",
            "exit_date": "2026-09-20",
            "sector": "Consumer Staples",
            "sub": "Brewers",
        }
    )
    many = _HEADER + _leaver(
        {
            "cik": "0000000007",
            "symbols": "GGG",
            "exit_date": "2026-09-20",
            "sector": "Consumer Staples",
            "sub": "Brewers",
            "cover": "GGG | GGG.A",
        }
    )
    assert _rows(build_profile.build(_SOURCE, one))[2]["multi_class_listed"] == "False"
    assert _rows(build_profile.build(_SOURCE, many))[2]["multi_class_listed"] == "True"


_MEMBERS = (
    "cik,symbols,company_name,joined_date,gics_sector,gics_sub_industry,last_10k_accession,"
    "last_10k_filed,article_9,evidence_quote,live_tickers,note\n"
)


def _member(row: dict[str, str]) -> str:
    """One staged later-joiner row; *row* carries cik, symbols, joined_date, sector, sub and overrides."""
    fields = {
        "last_10k_accession": "",
        "last_10k_filed": "",
        "article_9": "false",
        "evidence_quote": "",
        "live_tickers": row["symbols"],
        "note": "",
        **row,
    }
    return (
        f"{fields['cik']},{fields['symbols']},{fields['symbols']} Inc.,{fields['joined_date']},"
        f"{fields['sector']},{fields['sub']},{fields['last_10k_accession']},{fields['last_10k_filed']},"
        f'{fields["article_9"]},"{fields["evidence_quote"]}",{fields["live_tickers"]},{fields["note"]}\n'
    )


def test_a_later_joiner_gets_an_open_interval_and_the_live_multi_class() -> None:
    """The mirror of a leaver: the table predates it, so its row is today's snapshot (L-03) with an
    open interval, and the multi-class overlay is the **live** ticker list (the current members'
    rule), not a cover page."""
    members = _MEMBERS + _member(
        {
            "cik": "0000000008",
            "symbols": "HHH",
            "joined_date": "2026-09-21",
            "sector": "Health Care",
            "sub": "Life Sciences Tools & Services",
            "live_tickers": "HHH/HHH.A",
        }
    )
    rows = _rows(build_profile.build(_SOURCE, _HEADER, members))
    hhh = next(r for r in rows if r["cik"] == "0000000008")
    assert hhh["type"] == "operating" and hhh["financial_firm"] == "False"
    assert hhh["valid_from"] == "2020-01-01" and hhh["valid_to"] == ""
    assert hhh["multi_class_listed"] == "True"
    assert "the open stint from 2026-09-21" in hhh["basis"]


def test_a_later_joiner_with_a_bank_label_and_no_statement_gets_no_row() -> None:
    members = _MEMBERS + _member(
        {
            "cik": "0000000009",
            "symbols": "III",
            "joined_date": "2026-09-21",
            "sector": "Financials",
            "sub": "Regional Banks",
        }
    )
    out = build_profile.build(_SOURCE, _HEADER, members)
    assert "0000000009" not in {r["cik"] for r in _rows(out)}


def test_a_later_joiner_already_in_the_table_replaces_its_row() -> None:
    members = _MEMBERS + _member(
        {
            "cik": "0000000002",
            "symbols": "BBB",
            "joined_date": "2026-09-21",
            "sector": "Real Estate",
            "sub": "Office REITs",
        }
    )
    rows = [
        r for r in _rows(build_profile.build(_SOURCE, _HEADER, members)) if r["cik"] == "0000000002"
    ]
    assert len(rows) == 1 and rows[0]["gics_sub_industry"] == "Office REITs"
    assert build_profile.replaced_ciks(_SOURCE, "", members) == ["0000000002"]


def test_the_header_names_both_sources() -> None:
    header = [
        line for line in build_profile.build(_SOURCE, _HEADER).splitlines() if line.startswith("#")
    ]
    assert any("company_types.csv" in line for line in header)
    assert any("leaver_company_types.csv" in line for line in header)
