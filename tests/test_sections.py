"""10-K / 10-Q Item splitter and the sec_filing_section writer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fundamental_agent import db
from fundamental_agent.sections import Section, split_sections

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


def test_heals_item_marker_split_across_inline_tags() -> None:
    # Some filers chop the heading mid-word across <span>s ("Ite" + "m 2.") to
    # defeat naive scrapers; block-aware flattening must glue it back.
    md = "Cloud revenue rose and operating margin expanded again this quarter. "
    risk = "There have been no material changes to our previously disclosed risks. "
    html = (
        "<html><body>"
        "<p><span>Ite</span><span>m 2. Management's Discussion and Analysis</span></p>"
        f"<p>{md * 15}</p>"
        "<p><span>Item</span><span> 1A. Risk Factors</span></p>"
        f"<p>{risk * 15}</p>"
        "<p>Item 6. Exhibits</p><p>none</p>"
        "</body></html>"
    )
    by_type = {s.section_type: s for s in split_sections(html, "10-Q")}
    assert set(by_type) == {"MD&A", "RISK_FACTORS"}
    assert "Cloud revenue rose" in by_type["MD&A"].text
    assert "material changes" in by_type["RISK_FACTORS"].text


def test_extracts_bank_style_title_only_headings() -> None:
    # Large financial filers carry no "Item N" line markers -- the Item numbers live
    # only in a front cross-reference table; the body sections are descriptive
    # ALL-CAPS titles.
    mdna = "Net revenues increased across all reportable segments during the year. "
    risk = "Our results are sensitive to interest-rate and credit-spread movements. "
    legal = "We are party to various routine legal matters incidental to our business. "
    html = (
        "<html><body>"
        "<p>Table of Contents</p>"
        "<p>Management's Discussion and Analysis of Financial Condition and Results of Operations 25</p>"
        "<p>Risk Factors 40</p>"
        "<div>MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS</div>"
        f"<p>{mdna * 20}</p>"
        "<div>RISK FACTORS</div>"
        f"<p>{risk * 20}</p>"
        "<div>LEGAL PROCEEDINGS</div>"
        f"<p>{legal * 12}</p>"
        "</body></html>"
    )
    by_type = {s.section_type: s for s in split_sections(html, "10-K")}
    assert {"MD&A", "RISK_FACTORS", "LEGAL_PROCEEDINGS"} <= set(by_type)
    assert by_type["MD&A"].item_number == "7"
    assert "reportable segments" in by_type["MD&A"].text
    assert "credit-spread" not in by_type["MD&A"].text  # stopped at RISK FACTORS
    assert "interest-rate" in by_type["RISK_FACTORS"].text


def test_running_header_repeats_do_not_truncate_body() -> None:
    head = "Management's Discussion and Analysis of Financial Condition and Results of Operations"
    para = "Operating cash flow improved on stronger collections and lower capex. "
    repeated = "".join(f"<p>{head} (continued)</p><p>{para * 8}</p>" for _ in range(6))
    html = (
        "<html><body>"
        f"<h2>Item 2. {head}</h2><p>{para * 8}</p>"
        f"{repeated}"
        "<h2>Item 1A. Risk Factors</h2>"
        f"<p>{'No material changes to previously disclosed risk factors. ' * 10}</p>"
        "<h2>Item 6. Exhibits</h2><p>none</p>"
        "</body></html>"
    )
    by_type = {s.section_type: s for s in split_sections(html, "10-Q")}
    # the "(continued)" page headers are same-section, so the body runs through all
    # six of them, not just to the first one.
    assert by_type["MD&A"].text.count("Operating cash flow improved") >= 6
