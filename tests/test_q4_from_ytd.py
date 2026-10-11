"""PER-05 (T-149, checklist D-08): a discrete Q4 flow is the fiscal year less the nine-month
year-to-date of the third-quarter 10-Q -- not less three recorded quarters.

A 10-Q now records its own year-to-date beside its single-quarter value (``ytd_<item>`` in the
anchor metric row's audit inputs); a later Q4 reads the third quarter's. The year-to-date in
the last test is real (Waters' Q3 2023 10-Q, nine months to 2023-09-30); the other figures are
recorded stand-ins.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, cast

from conftest import filed_after
from portfolio_common.db import Database

from fundamental_agent import db
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import RunParams, _Engine, _targets, _ttm_flows, _YearTask
from fundamental_agent.statements import Statements

FIXTURES = Path(__file__).parent / "fixtures"
FY_END = date(2023, 12, 31)


def _record(
    conn: Database, form: str, period: str, period_end: str, inputs: dict[str, float]
) -> int:
    conn.execute("INSERT OR IGNORE INTO assets (id, ticker) VALUES (1, 'WAT')")
    fid = db.upsert_filing(
        conn,
        FilingKey(1, form, int(period_end[:4]), period),
        FilingMeta(filing_date=filed_after(period_end), period_end=period_end),
    )
    db.record_metrics(
        conn, fid, [("profitability", MetricResult("return_on_assets", None, "r", dict(inputs)))]
    )
    return fid


def _q4(conn: Database, item: str = "net_income") -> float | None:
    return db._quarter_flow_ending(conn, 1, FY_END, item)


def test_q4_is_the_year_less_the_third_quarter_s_nine_months(memory_db: Database) -> None:
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(
        memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0, "ytd_net_income": 300.0}
    )
    assert _q4(memory_db) == 100.0


def test_it_needs_no_first_or_second_quarter_recorded(memory_db: Database) -> None:
    """The old derivation needed Q1, Q2 and Q3; with only the year and the Q3 10-Q recorded it
    gave nothing."""
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0})  # no year-to-date
    assert _q4(memory_db) is None
    memory_db.execute("DELETE FROM sec_filings WHERE fiscal_period = '2023Q3'")
    _record(
        memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0, "ytd_net_income": 300.0}
    )
    assert _q4(memory_db) == 100.0


def test_a_third_quarter_recorded_without_its_year_to_date_falls_back_to_three_quarters(
    memory_db: Database,
) -> None:
    """Rows written before the year-to-date was kept: the year less Q1, Q2 and Q3, as before --
    and nothing when one of the three is missing."""
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0})
    _record(memory_db, "10-Q", "2023Q2", "2023-06-30", {"net_income": 110.0})
    assert _q4(memory_db) is None  # no Q1
    _record(memory_db, "10-Q", "2023Q1", "2023-03-31", {"net_income": 100.0})
    assert _q4(memory_db) == 400.0 - 100.0 - 110.0 - 90.0


def test_the_year_to_date_wins_over_the_quarters_when_both_are_recorded(
    memory_db: Database,
) -> None:
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(
        memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0, "ytd_net_income": 310.0}
    )
    _record(memory_db, "10-Q", "2023Q2", "2023-06-30", {"net_income": 110.0})
    _record(memory_db, "10-Q", "2023Q1", "2023-03-31", {"net_income": 100.0})
    assert _q4(memory_db) == 90.0  # 400 - 310, not 400 - 300


def test_a_10q_that_is_not_a_quarter_before_the_year_end_is_not_the_third(
    memory_db: Database,
) -> None:
    """A changed fiscal year: the latest 10-Q closed nine months before the 10-K's year end, not
    one quarter (PER-08: [89, 98] days). Its year-to-date is not nine months of this year."""
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(memory_db, "10-Q", "2023Q1", "2023-03-31", {"net_income": 90.0, "ytd_net_income": 90.0})
    assert _q4(memory_db) is None


def test_a_52_53_week_year_end_matches_by_the_rounded_month_end(memory_db: Database) -> None:
    """Fiscal year ends 2023-12-30 (a Saturday); its third quarter closed 2023-09-30."""
    _record(memory_db, "10-K", "FY2023", "2023-12-30", {"net_income": 400.0})
    _record(
        memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0, "ytd_net_income": 300.0}
    )
    assert _q4(memory_db) == 100.0


def test_the_trailing_quarters_of_a_first_quarter_use_the_derived_q4(memory_db: Database) -> None:
    """Q1 2024's four-quarter sum is Q1 + Q4 + Q3 + Q2 of 2023: Q4 from the year and the Q3
    year-to-date, with Q1 of 2023 never recorded."""
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 400.0})
    _record(
        memory_db, "10-Q", "2023Q3", "2023-09-30", {"net_income": 90.0, "ytd_net_income": 300.0}
    )
    _record(memory_db, "10-Q", "2023Q2", "2023-06-30", {"net_income": 110.0})
    flows = db.ttm_detail(
        memory_db, 1, period_end="2024-03-31", current={"net_income": 120.0}, ytd=None
    )
    assert flows["net_income"].method == "quarters"
    assert flows["net_income"].value == 120.0 + 100.0 + 90.0 + 110.0


def _waters_q3() -> Statements:
    raw = json.loads(
        (FIXTURES / "financials_WAT_10-Q_2023_acc-0001193125-23-271909.json").read_text()
    )
    return Statements.from_payload(raw["data"])


def test_a_10q_records_its_year_to_date_from_its_own_columns(memory_db: Database) -> None:
    """Waters' Q3 2023: net income 134.552M in the quarter, 426.029M in the nine months."""
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'WAT', '0001000697')")
    stmts = _waters_q3()
    task = _YearTask(1, "WAT", "Waters", "10-Q", 2023, "0001000697")
    (target,) = _targets(stmts, task)
    assert (target.fiscal_period, target.period.date) == ("2023Q3", "2023-09-30")
    engine = _Engine(
        conn=memory_db,
        edgar=cast("Any", None),
        analyst=cast("Any", None),
        params=RunParams(),
        report=cast("Any", None),
        completed=set(),
    )
    flows = _ttm_flows(engine, task, stmts, target)
    assert flows["net_income"].ytd == 426_029_000.0
    results = [("profitability", MetricResult("return_on_assets", None, "r", {"net_income": 1.0}))]
    db.stamp_ytd(results, flows)
    inputs = results[0][1].inputs
    assert inputs["ytd_net_income"] == 426_029_000.0
    assert inputs["net_income"] == 1.0  # the single-quarter value is untouched
    # recorded and read back for the Q4 of its fiscal year (a stand-in 10-K)
    fid = db.upsert_filing(
        memory_db,
        FilingKey(1, "10-Q", 2023, "2023Q3"),
        FilingMeta(filing_date="2023-11-02", period_end="2023-09-30"),
    )
    db.record_metrics(memory_db, fid, results)
    _record(memory_db, "10-K", "FY2023", "2023-12-31", {"net_income": 505_000_000.0})
    assert _q4(memory_db) == 505_000_000.0 - 426_029_000.0
