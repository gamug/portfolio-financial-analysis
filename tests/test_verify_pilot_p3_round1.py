"""verify_pilot's P3 round 1 checks: T-116 allows the current gate version, and T-140 counts the
missing periods by their **fiscal label** (reviewer A, P3 round 1, 2026-10-20)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from verify_pilot import (
    gate_version_status,
    missing_annual_filings,
    missing_quarter_labels,
    stale_metric_filings,
)

from kg_schema.versions import DATA_QUALITY_GATE_VERSION


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "financial.db")
    conn.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT)")
    conn.execute(
        "CREATE TABLE sec_filings (id INTEGER PRIMARY KEY, asset_id INTEGER, form TEXT, "
        "fiscal_period TEXT, period_end TEXT, accession_number TEXT)"
    )
    conn.execute(
        "CREATE TABLE fundamental_metrics (id INTEGER PRIMARY KEY, filing_id INTEGER, "
        "metric_name TEXT, value REAL, run_id INTEGER)"
    )
    conn.commit()
    return conn


def _filing(
    conn: sqlite3.Connection, ticker: str, form: str, fiscal_period: str, period_end: str
) -> int:
    row = conn.execute("SELECT id FROM assets WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        cursor = conn.execute("INSERT INTO assets (ticker) VALUES (?)", (ticker,))
        asset_id = int(cursor.lastrowid or 0)
    else:
        asset_id = int(row[0])
    cursor = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_period, period_end, accession_number) "
        "VALUES (?, ?, ?, ?, ?)",
        (asset_id, form, fiscal_period, period_end, f"acc-{fiscal_period}"),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _metric(conn: sqlite3.Connection, ticker: str, fiscal_period: str, *, run_id: int) -> None:
    """A metric written by *run_id* for the filing of *fiscal_period*."""
    filing_id = conn.execute(
        "SELECT id FROM sec_filings WHERE fiscal_period = ?", (fiscal_period,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO fundamental_metrics (filing_id, metric_name, value, run_id) "
        "VALUES (?, 'revenue', 1.0, ?)",
        (filing_id, run_id),
    )
    conn.commit()


def test_t116_allows_the_current_gate_version() -> None:
    """The gate is append-only under the current version, so a database written by this code
    carries it: that is not a finding. An older version is."""
    assert gate_version_status([DATA_QUALITY_GATE_VERSION]) == "PASS"
    assert gate_version_status([]) == "PASS"
    assert gate_version_status(["dq-v2"]) == "FAIL"


def test_t140_counts_a_missing_quarter_by_its_fiscal_label(tmp_path: Path) -> None:
    """A fiscal year is the 10-K that closes it: the 10-Qs between two 10-Ks are that year's
    quarters, and a label's quarter number is the fiscal quarter. FY2023 below is missing its Q2."""
    conn = _db(tmp_path)
    _filing(conn, "AAA", "10-K", "FY2022", "2022-12-31")
    _filing(conn, "AAA", "10-Q", "2022Q1", "2022-03-31")
    _filing(conn, "AAA", "10-Q", "2022Q2", "2022-06-30")
    _filing(conn, "AAA", "10-Q", "2022Q3", "2022-09-30")
    _filing(conn, "AAA", "10-K", "FY2023", "2023-12-31")
    _filing(conn, "AAA", "10-Q", "2023Q1", "2023-03-31")
    _filing(conn, "AAA", "10-Q", "2023Q3", "2023-09-30")
    assert missing_quarter_labels(conn) == [("AAA", "FY2023", "Q2", 2022, 2023)]


def test_t140_calls_no_period_missing_when_every_label_is_there(tmp_path: Path) -> None:
    """A 16/17-week fourth quarter (AZO, COST, DPZ, PEP) is not a gap: its fourth quarter *is*
    the FY 10-K, and the label counts it. The 119 days between the Q3 and the FY end below would
    have failed the old 110-day threshold."""
    conn = _db(tmp_path)
    _filing(conn, "ZZZ", "10-K", "FY2021", "2021-12-31")
    _filing(conn, "ZZZ", "10-Q", "2021Q1", "2021-04-03")
    _filing(conn, "ZZZ", "10-Q", "2021Q2", "2021-07-03")
    _filing(conn, "ZZZ", "10-Q", "2021Q3", "2021-09-03")
    assert missing_quarter_labels(conn) == []


def test_t140_exempts_the_first_fiscal_year(tmp_path: Path) -> None:
    """A company that joined the index mid-year has no earlier quarters: its first fiscal year is
    not a gap. The 10-Qs after the last 10-K belong to a fiscal year that has not closed yet."""
    conn = _db(tmp_path)
    _filing(conn, "NEW", "10-K", "FY2023", "2023-12-31")
    _filing(conn, "NEW", "10-Q", "2023Q3", "2023-09-30")
    _filing(conn, "NEW", "10-Q", "2024Q1", "2024-03-31")
    assert missing_quarter_labels(conn) == []


def test_t140_names_the_fiscal_year_without_its_10_k(tmp_path: Path) -> None:
    """Two consecutive 10-Ks more than 371 days apart (PER-08's year upper bound: 52/53 weeks)
    mean a fiscal year whose 10-K is missing -- invisible to the label count, because its
    quarters merge into the next year."""
    conn = _db(tmp_path)
    _filing(conn, "AAA", "10-K", "FY2021", "2021-12-31")
    _filing(conn, "AAA", "10-K", "FY2024", "2024-12-31")
    assert missing_annual_filings(conn) == [("AAA", "2021-12-31", "2024-12-31", 1096)]


def test_t140_does_not_name_a_declared_transition(tmp_path: Path) -> None:
    """PER-09: a 10-KT is declared, not fetched as a period, so a company that changed its fiscal
    year has more than a year between its 10-Ks -- and that is not a missing filing."""
    conn = _db(tmp_path)
    _filing(conn, "FERG", "10-K", "FY2024", "2024-07-31")
    _filing(conn, "FERG", "10-K", "FY2026", "2026-12-31")
    assert missing_annual_filings(conn) == []


def test_t017_names_the_filings_whose_metrics_predate_an_earlier_filings(tmp_path: Path) -> None:
    """A filing recovered in a later run leaves the filings after it computed without it: their
    metrics carry an earlier run's id. The pilot database has exactly four such filings, all
    ETR's (0000065984-25-000132, -26-000174, -26-000222, -26-000283)."""
    conn = _db(tmp_path)
    _filing(conn, "ETR", "10-Q", "2023Q3", "2023-09-30")
    _filing(conn, "ETR", "10-Q", "2025Q3", "2025-09-30")
    _metric(conn, "ETR", "2023Q3", run_id=4)
    _metric(conn, "ETR", "2025Q3", run_id=3)
    assert stale_metric_filings(conn) == [("ETR", "acc-2025Q3", "2025Q3", 3, 4)]


def test_t017_is_quiet_when_every_filing_carries_the_same_run(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _filing(conn, "AAA", "10-Q", "2023Q1", "2023-03-31")
    _filing(conn, "AAA", "10-Q", "2023Q2", "2023-06-30")
    _metric(conn, "AAA", "2023Q1", run_id=1)
    _metric(conn, "AAA", "2023Q2", run_id=1)
    assert stale_metric_filings(conn) == []
