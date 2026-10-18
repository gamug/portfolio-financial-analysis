"""``scripts/combined_event_window_test.py`` (T-153 part 3d, D10): the window test over the
combined classes.

The script selects the events of every combined class (``COMBINED_CLASSES``), so an MTCH-style
``split_spinoff`` event is listed beside DD's ``reverse_split_spinoff`` — T-143 part A item 9.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import combined_event_window_test as script

from kg_schema import corporate_actions


def _db(path: Path) -> Path:
    """Two combined-class issuers: MTCH (CIK 0000891103) and DD (CIK 0001666700)."""
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, cik TEXT);"
        "CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER, period_end TEXT,"
        " filing_date TEXT);"
        "CREATE TABLE price_daily (id INTEGER PRIMARY KEY, asset_id INTEGER, date TEXT, close REAL);"
        "INSERT INTO assets (id, ticker, cik) VALUES (1, 'MTCH', '0000891103'),"
        " (2, 'DD', '0001666700');"
        "INSERT INTO sec_filings (asset_id, period_end, filing_date)"
        " VALUES (1, '2020-06-30', '2020-08-10'), (2, '2019-03-31', '2019-05-03');"
        "INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2020-07-31', 110.0),"
        " (2, '2019-06-28', 70.0);"
    )
    raw.commit()
    raw.close()
    return path


def _mtch() -> corporate_actions.CorporateActionEvent:
    return corporate_actions.CorporateActionEvent(
        cik="0000891103",
        ticker="MTCH",
        ex_date="2020-07-01",
        value=3.502,
        event_class=corporate_actions.SPLIT_SPINOFF,
        evidence_form="8-K",
        evidence_url="https://www.sec.gov/Archives/edgar/data/891103/x.htm",
        evidence_date="2020-07-02",
        evidence_quote="Holders of Old IAC common stock",
        rules="MKT-02;C11",
    )


def test_a_split_spinoff_event_lists_its_assets_filings(tmp_path: Path) -> None:
    """T-143 part A item 9: a ``split_spinoff`` event (MTCH 2020-07-01) is a combined-class event, so
    its asset's stored filings are listed by the window test, exactly as DD's are."""
    rows = script.window_test(_db(tmp_path / "window.db"), events=(_mtch(),))
    assert [(r["ticker"], r["period_end"], r["ex_date"]) for r in rows] == [
        ("MTCH", "2020-06-30", "2020-07-01")
    ]
    assert rows[0]["refused"] is True  # the count (period end) precedes the event, inside the bound


def test_the_packaged_list_selects_both_combined_classes(tmp_path: Path) -> None:
    """The default (``read_events()``) selects the packaged list's combined classes too: DD's
    ``reverse_split_spinoff`` (2019-06-03) and MTCH's ``split_spinoff`` (2020-07-01)."""
    rows = script.window_test(_db(tmp_path / "window.db"))
    assert {r["ex_date"] for r in rows} == {"2019-06-03", "2020-07-01"}
