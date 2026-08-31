"""10-K / 10-Q Item splitter and the sec_filing_section writer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fundamental_agent import db
from fundamental_agent.sections import Section, canonical_item_label, split_sections

FIXTURES = Path(__file__).parent / "fixtures"


def _html() -> str:
    return (FIXTURES / "edgar_10k_sample.html").read_text()


def test_split_picks_real_sections_over_toc_lines() -> None:
    sections = split_sections(_html(), "10-K")
    by_type = {s.section_type: s for s in sections}
    assert set(by_type) == {"BUSINESS", "RISK_FACTORS", "LEGAL_PROCEEDINGS", "MD&A"}

    risk = by_type["RISK_FACTORS"]
    assert risk.item_number == "1A"
    assert "currently known or unknown" in risk.text  # body, not the TOC dotted line
    assert "....." not in risk.text
    assert risk.word_count > 80

    mdna = by_type["MD&A"]
    assert "should be read in conjunction" in mdna.text


def test_ordinals_are_sequential_and_hashes_stable() -> None:
    a = split_sections(_html(), "10-K")
    b = split_sections(_html(), "10-K")
    assert [s.ordinal for s in a] == list(range(len(a)))
    assert [s.sha256 for s in a] == [s.sha256 for s in b]


def test_unknown_form_yields_nothing() -> None:
    assert split_sections(_html(), "8-K") == []


def test_canonical_item_label_vocabulary() -> None:
    assert canonical_item_label("1A", "RISK_FACTORS") == "ITEM_1A_RISK_FACTORS"
    assert canonical_item_label("3", "LEGAL_PROCEEDINGS") == "ITEM_3_LEGAL_PROCEEDINGS"
    assert canonical_item_label("7", "MD&A") == "ITEM_7_MDA"
    assert canonical_item_label("1", "BUSINESS") == "ITEM_1_BUSINESS"
    assert canonical_item_label(" 2 ", "MD&A") == "ITEM_2_MDA"  # trimmed + upper
    assert canonical_item_label(None, "MD&A") == "MDA"  # no item number
    assert canonical_item_label("", "RISK_FACTORS") == "RISK_FACTORS"
    assert canonical_item_label("9", "New Kind") == "ITEM_9_NEW_KIND"  # generic fallback


def test_10q_uses_a_different_item_map() -> None:
    html = (
        "<html><body>"
        "<h2>Item 2. Management's Discussion and Analysis</h2>"
        "<p>" + ("Quarterly results improved on stronger services revenue. " * 12) + "</p>"
        "<h2>Item 1A. Risk Factors</h2>"
        "<p>" + ("There have been no material changes to our risk factors. " * 12) + "</p>"
        "<h2>Item 6. Exhibits</h2><p>none</p>"
        "</body></html>"
    )
    types = {s.section_type for s in split_sections(html, "10-Q")}
    assert types == {"MD&A", "RISK_FACTORS"}


def test_insert_filing_sections_is_immutable(memory_db: sqlite3.Connection) -> None:
    memory_db.execute("INSERT INTO assets (ticker) VALUES ('AAPL')")
    memory_db.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, retrieved_at) "
        "VALUES (1, '10-K', 2023, 'FY2023', '2024-01-01T00:00:00Z')"
    )
    memory_db.commit()
    sections = split_sections(_html(), "10-K")

    n = db.insert_filing_sections(memory_db, 1, sections, event_time="2023-11-03")
    assert n == len(sections)
    db.insert_filing_sections(memory_db, 1, sections, event_time="2023-11-03")  # no-op
    assert memory_db.execute("SELECT COUNT(*) FROM sec_filing_section").fetchone()[0] == n
    assert db.filings_with_sections(memory_db) == {1}

    view_rows = memory_db.execute(
        "SELECT ticker, form, section_type, word_count FROM v_sec_filing_section ORDER BY ordinal"
    ).fetchall()
    assert {r["section_type"] for r in view_rows} == {s.section_type for s in sections}
    assert all(r["ticker"] == "AAPL" and r["form"] == "10-K" for r in view_rows)


def test_section_helpers() -> None:
    s = Section("MD&A", "7", "Item 7.", 0, 10, 400, "one two three four five")
    assert s.word_count == 5
    assert len(s.sha256) == 64
