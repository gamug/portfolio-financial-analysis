"""The cover-page share count, from the gateway to ``financial`` storage and the valuation metric
(T-132(a)/(b); portfolio-data-mining T-043 adds ``data["cover"]["shares_outstanding"]``).

The count is the filing's own ``dei:EntityCommonStockSharesOutstanding``. ``us-gaap_CommonStock
SharesIssued`` is never a stand-in for it: it includes treasury stock (PG: 4.009B issued against
2.324B outstanding, a cap about 1.7x too high).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_filing
from portfolio_common.db import Database
from test_edgar_client import _serving
from test_pipeline_multi_filing import (
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _run,
    _stz_q2_payload,
)

from fundamental_agent import db
from fundamental_agent.edgar_client import EdgarError
from fundamental_agent.metrics import valuation
from fundamental_agent.pricing import ClosePrice
from fundamental_agent.statements import CoverShares, Statements

PG_COVER = {
    "shares_outstanding": [
        {"value": 2_324_433_060, "as_of_date": "2026-07-31", "class_member": None}
    ]
}


def _payload(**extra: Any) -> dict[str, Any]:
    return {"income_statement": [], "balance_sheet": [], "cash_flow": [], **extra}


# -- parsing -----------------------------------------------------------------------------------


def test_the_cover_count_is_parsed_from_the_payload() -> None:
    stmts = Statements.from_payload(_payload(cover=PG_COVER))
    assert stmts.cover_shares == [CoverShares(2_324_433_060.0, "2026-07-31", "")]
    assert stmts.cover_error is None


def test_every_class_is_kept_with_its_member() -> None:
    cover = {
        "shares_outstanding": [
            {"value": 5_822_000_000, "as_of_date": "2026-01-28", "class_member": "ClassA"},
            {"value": 837_000_000, "as_of_date": "2026-01-28", "class_member": "ClassB"},
        ]
    }
    stmts = Statements.from_payload(_payload(cover=cover))
    assert [(c.class_member, c.value) for c in stmts.cover_shares] == [
        ("ClassA", 5_822_000_000.0),
        ("ClassB", 837_000_000.0),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"value": 0, "as_of_date": "2026-07-31"},  # not a count
        {"value": -5, "as_of_date": "2026-07-31"},
        {"value": True, "as_of_date": "2026-07-31"},
        {"value": "2324433060", "as_of_date": "2026-07-31"},  # a string is not a number
        {"value": 100, "as_of_date": None},  # a count the filing did not date is never stored
        {"value": 100, "as_of_date": ""},
        {"value": 100},
        "not a dict",
    ],
)
def test_an_unusable_entry_is_dropped(entry: Any) -> None:
    assert (
        Statements.from_payload(_payload(cover={"shares_outstanding": [entry]})).cover_shares == []
    )


@pytest.mark.parametrize(
    "cover", [None, {}, {"shares_outstanding": []}, {"shares_outstanding": None}, "x"]
)
def test_a_payload_with_no_cover_has_none(cover: Any) -> None:
    """A gateway that predates T-043, a filing with no cover fact, and a malformed value."""
    assert Statements.from_payload(_payload(cover=cover)).cover_shares == []
    assert Statements.from_payload(_payload()).cover_shares == []


def test_a_failed_cover_read_is_carried_beside_the_still_validated_statements() -> None:
    payload = _payload(
        cover={"shares_outstanding": []},
        reconciliation_errors=[{"statement": "cover", "error": "KeyError: 'dei'"}],
    )
    stmts = Statements.from_payload(payload)
    assert stmts.cover_shares == [] and stmts.cover_error == "KeyError: 'dei'"


# -- the client --------------------------------------------------------------------------------


def test_a_cover_only_reconciliation_error_does_not_fail_the_filing() -> None:
    """The gateway's cover read fails in isolation (upstream PR #48): the three statements are
    still validated, so the filing is analysed and merely has no count."""
    payload = _payload(
        cover={"shares_outstanding": []},
        reconciliation_errors=[{"statement": "cover", "error": "boom"}],
    )
    with _serving({"success": True, "data": payload}) as client:
        assert client.financials("PG", "10-K", 2026) == payload


def test_a_statement_error_still_fails_even_beside_a_cover_error() -> None:
    payload = _payload(
        reconciliation_errors=[
            {"statement": "cover", "error": "boom"},
            {"statement": "balance_sheet", "error": "KeyError"},
        ]
    )
    with (
        _serving({"success": True, "data": payload}) as client,
        pytest.raises(EdgarError, match=r"balance_sheet.*KeyError") as info,
    ):
        client.financials("PG", "10-K", 2026)
    assert "cover" not in str(info.value)


# -- the registry never reads shares issued ----------------------------------------------------


def test_shares_issued_is_not_shares_outstanding() -> None:
    key = "2026-06-30"
    issued_only = _payload(
        balance_sheet=[
            {"concept": "us-gaap_CommonStockSharesIssued", "label": "Shares issued", key: 4_009e6}
        ]
    )
    assert Statements.from_payload(issued_only).get("shares_outstanding", key) is None
    both = _payload(
        balance_sheet=[
            {"concept": "us-gaap_CommonStockSharesIssued", key: 4_009e6},
            {"concept": "us-gaap_CommonStockSharesOutstanding", key: 2_324e6},
        ]
    )
    assert Statements.from_payload(both).get("shares_outstanding", key) == 2_324e6


# -- the valuation metric ----------------------------------------------------------------------


def _market_cap(stmts: Statements, key: str, close: float) -> tuple[float | None, dict[str, float]]:
    results = valuation.compute(stmts, key, ClosePrice("2023-09-29", close))
    cap = next(r for r in results if r.name == "market_capitalization")
    return cap.value, cap.inputs


def test_the_cover_count_is_preferred_to_the_balance_sheet(aapl_10k: Statements) -> None:
    key = "2023-09-30 (FY)"
    on_balance_sheet, inputs = _market_cap(aapl_10k, key, 100.0)
    assert on_balance_sheet == pytest.approx(15_550_061_000.0 * 100.0)
    assert inputs["shares_from_cover_page"] == 0.0

    aapl_10k.cover_shares = [CoverShares(15_000_000_000.0, "2023-10-20")]
    cap, inputs = _market_cap(aapl_10k, key, 100.0)
    assert cap == pytest.approx(15_000_000_000.0 * 100.0)
    assert inputs["shares_from_cover_page"] == 1.0 and inputs["shares_are_diluted_average"] == 0.0


def test_a_dual_class_cover_is_summed_in_the_metric_as_in_the_reader(aapl_10k: Statements) -> None:
    aapl_10k.cover_shares = [
        CoverShares(10.0, "2023-10-20", "ClassA"),
        CoverShares(5.0, "2023-10-20", "ClassB"),
    ]
    assert _market_cap(aapl_10k, "2023-09-30 (FY)", 2.0)[0] == pytest.approx(30.0)


def test_with_no_cover_the_balance_sheet_count_still_serves(aapl_10k: Statements) -> None:
    assert aapl_10k.cover_shares == []
    assert _market_cap(aapl_10k, "2023-09-30 (FY)", 1.0)[0] == pytest.approx(15_550_061_000.0)


# -- storage -----------------------------------------------------------------------------------


@pytest.fixture
def filing(memory_db: Database) -> int:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'PG')")
    return seed_filing(memory_db, 1, period_end="2026-06-30", filing_date="2026-08-05")


def test_cover_counts_are_stored_once(memory_db: Database, filing: int) -> None:
    counts = [CoverShares(2_324_433_060.0, "2026-07-31")]
    db.insert_cover_shares(memory_db, filing, counts, event_time="2026-06-30", run_id=7)
    db.insert_cover_shares(memory_db, filing, counts, event_time="2026-06-30", run_id=8)  # a re-run
    rows = memory_db.execute(
        "SELECT class_member, value, as_of_date, event_time, run_id FROM filing_cover_shares"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("", 2_324_433_060.0, "2026-07-31", "2026-06-30", 7)]


def test_a_zero_count_is_never_stored(memory_db: Database, filing: int) -> None:
    """The column's CHECK (value > 0) backs the parser's own filter; ``OR IGNORE`` drops it."""
    db.insert_cover_shares(
        memory_db, filing, [CoverShares(0.0, "2026-07-31")], event_time="2026-06-30"
    )
    assert memory_db.execute("SELECT COUNT(*) FROM filing_cover_shares").fetchone()[0] == 0


def test_deleting_a_filing_deletes_its_counts(memory_db: Database, filing: int) -> None:
    memory_db.execute("PRAGMA foreign_keys = ON")
    db.insert_cover_shares(
        memory_db, filing, [CoverShares(1.0, "2026-07-31")], event_time="2026-06-30"
    )
    memory_db.execute("DELETE FROM sec_filings WHERE id = ?", (filing,))
    assert memory_db.execute("SELECT COUNT(*) FROM filing_cover_shares").fetchone()[0] == 0


# -- the pipeline ------------------------------------------------------------------------------


def _with_cover(payload: dict[str, Any], count: float, as_of: str) -> dict[str, Any]:
    return {**payload, "cover": {"shares_outstanding": [{"value": count, "as_of_date": as_of}]}}


def _covers(tmp_path: Path) -> list[tuple[str, float, str]]:
    conn = sqlite3.connect(tmp_path / "kg.db")
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT f.fiscal_period, c.value, c.as_of_date FROM filing_cover_shares c "
            "JOIN sec_filings f ON f.id = c.filing_id ORDER BY f.period_end"
        )
    ]


def test_a_run_stores_each_filing_s_cover_count_under_its_own_filing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, Q1_JUN]},
        {
            Q1_JUN.accession_number: _with_cover(_q1_payload(), 190e6, "2023-06-20"),
            Q2_OCT.accession_number: _with_cover(_stz_q2_payload(), 185e6, "2023-09-29"),
        },
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert report.failed == 0
    assert _covers(tmp_path) == [("2023Q1", 190e6, "2023-06-20"), ("2023Q2", 185e6, "2023-09-29")]


def test_a_filing_without_a_count_is_stored_without_one_and_not_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar({("10-Q", 2023): [Q1_JUN]}, {Q1_JUN.accession_number: _q1_payload()})
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed) == (1, 0)
    assert _covers(tmp_path) == []


def test_a_failed_cover_read_is_recorded_for_triage_and_does_not_fail_the_filing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        **_q1_payload(),
        "reconciliation_errors": [{"statement": "cover", "error": "dei facts unreadable"}],
    }
    report = _run(
        monkeypatch,
        tmp_path,
        _Edgar({("10-Q", 2023): [Q1_JUN]}, {Q1_JUN.accession_number: payload}),
    )
    assert (report.completed, report.failed) == (1, 0)
    conn = sqlite3.connect(tmp_path / "kg.db")
    errors = conn.execute("SELECT stage, message FROM analysis_run_error").fetchall()
    assert errors == [("cover", "dei facts unreadable")]
