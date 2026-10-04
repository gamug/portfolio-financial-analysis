"""T-140 (b): no filing dropped for a repeated quarter label (``docs/model_fixes.md``).

The gateway tags a column with a quarter from the period end's calendar month, so a 52/53-week
filer's quarter that closes a few days after a month boundary is tagged one ahead: Waters' fiscal
Q2 ending 2023-07-01 arrived as ``(Q3)`` beside the real Q3 (2023-09-30), the resume key
``(ticker, form, fiscal_period)`` read the second filing as done, and it was skipped with no error.
The ``financials_WAT_*`` / ``financials_APO_*`` fixtures are real gateway captures, each statement
trimmed to its first rows (every period column and the balance sheet's fiscal year end kept).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database
from test_pipeline_multi_filing import _Edgar, _StubAnalyst

from fundamental_agent import db, pipeline
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingLabelCollision, FilingMeta
from fundamental_agent.edgar_client import FilingRef
from fundamental_agent.fiscal import (
    label_year,
    payload_fiscal_year_end,
    quarter_label,
    quarter_number,
)
from fundamental_agent.pipeline import RunParams, _resolve_target, _targets, _YearTask
from fundamental_agent.statements import Statements
from kg_schema import connect

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((FIXTURES / name).read_text())["data"])


WAT_Q1 = FilingRef("10-Q", "2023-05-09", "0001193125-23-138998")  # quarter ends 2023-04-01
WAT_Q2 = FilingRef("10-Q", "2023-08-02", "0001193125-23-201115")  # 2023-07-01, tagged (Q3)
WAT_Q3 = FilingRef("10-Q", "2023-11-07", "0001193125-23-271909")  # 2023-09-30, tagged (Q3)
WAT_2021_Q3 = FilingRef("10-Q", "2021-11-04", "0001193125-21-319595")  # 2021-10-02, tagged (Q4)
WAT_2022_Q3 = FilingRef("10-Q", "2022-11-03", "0001193125-22-276585")  # 2022-10-01, tagged (Q4)
APO_Q1 = FilingRef("10-Q", "2023-05-09", "0001858681-23-000017")
_WAT_FILES = {
    WAT_Q1.accession_number: "financials_WAT_10-Q_2023_acc-0001193125-23-138998.json",
    WAT_Q2.accession_number: "financials_WAT_10-Q_2023_acc-0001193125-23-201115.json",
    WAT_Q3.accession_number: "financials_WAT_10-Q_2023_acc-0001193125-23-271909.json",
    WAT_2021_Q3.accession_number: "financials_WAT_10-Q_2021_acc-0001193125-21-319595.json",
    WAT_2022_Q3.accession_number: "financials_WAT_10-Q_2022_acc-0001193125-22-276585.json",
}


def _wat(ref: FilingRef) -> dict[str, Any]:
    return _fixture(_WAT_FILES[ref.accession_number])


def _task(ticker: str = "WAT", year: int = 2023) -> _YearTask:
    return _YearTask(1, ticker, ticker, "10-Q", year)


def _label(payload: dict[str, Any], ticker: str = "WAT") -> str:
    (target,) = _targets(Statements.from_payload(payload), _task(ticker))
    return target.fiscal_period


# -- the fiscal-calendar arithmetic -------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end", "fiscal_year_end", "label"),
    [
        # a calendar-quarter filer: every label is the calendar quarter, as before
        ("2023-03-31", "2022-12-31", "2023Q1"),
        ("2023-06-30", "2022-12-31", "2023Q2"),
        ("2023-09-30", "2022-12-31", "2023Q3"),
        # Waters, a 52/53-week December filer: the dates the gateway tagged (Q2)/(Q3)/(Q4)
        ("2023-04-01", "2022-12-31", "2023Q1"),
        ("2023-07-01", "2022-12-31", "2023Q2"),
        ("2023-09-30", "2022-12-31", "2023Q3"),
        ("2021-10-02", "2020-12-31", "2021Q3"),
        ("2022-10-01", "2021-12-31", "2022Q3"),
        ("2026-04-04", "2025-12-31", "2026Q1"),
        # a fiscal year ending 2023-01-01 (Johnson & Johnson's calendar) against its quarters
        ("2023-04-02", "2023-01-01", "2023Q1"),
        ("2023-07-02", "2023-01-01", "2023Q2"),
        ("2023-10-01", "2023-01-01", "2023Q3"),
        # a September filer (Apple), a February one (Constellation), a January one (Target)
        ("2022-12-31", "2022-09-24", "2022Q1"),
        ("2023-07-01", "2022-09-24", "2023Q3"),
        ("2023-05-31", "2023-02-28", "2023Q1"),
        ("2023-11-30", "2023-02-28", "2023Q3"),
        ("2023-04-29", "2023-01-28", "2023Q1"),
        ("2023-10-28", "2023-01-28", "2023Q3"),
        # a fiscal year end from the *next* year anchors a quarter too (a first quarter before any
        # stored 10-K): 2021-04-03 counted back from 2021-12-31
        ("2021-04-03", "2021-12-31", "2021Q1"),
    ],
)
def test_the_quarter_is_counted_from_the_fiscal_year_end(
    period_end: str, fiscal_year_end: str, label: str
) -> None:
    assert (
        quarter_label(date.fromisoformat(period_end), date.fromisoformat(fiscal_year_end)) == label
    )


@pytest.mark.parametrize(
    ("period_end", "fiscal_year_end"),
    [
        ("2022-12-31", "2022-12-31"),  # the fiscal year end itself: a 10-K, never a 10-Q
        ("2023-12-30", "2022-12-31"),  # a year on: the same
        ("2023-02-15", "2022-12-31"),  # 46 days: no quarter boundary within a month
        ("2023-08-20", "2022-12-31"),  # 232 days: between the second and third
    ],
)
def test_a_date_that_is_no_10_q_quarter_end_gets_no_label(
    period_end: str, fiscal_year_end: str
) -> None:
    assert (
        quarter_number(date.fromisoformat(period_end), date.fromisoformat(fiscal_year_end)) is None
    )


def test_every_quarter_of_a_52_53_week_calendar_is_labelled_in_order_for_fifteen_years() -> None:
    """Fiscal years of 52 or 53 weeks ending on the Saturday nearest 31 December (Waters):
    quarters of 13 weeks (the 53rd week sits in the last). All three 10-Qs of every year are
    labelled Q1, Q2, Q3 -- and the calendar year of the period end is the label's year."""
    fye = date(2014, 1, 4)  # a Saturday
    for _ in range(15):
        next_fye = fye + timedelta(weeks=52)
        if abs((next_fye + timedelta(weeks=1) - date(next_fye.year, 12, 31)).days) < abs(
            (next_fye - date(next_fye.year, 12, 31)).days
        ):
            next_fye += timedelta(weeks=1)  # the 53-week year
        labels = [quarter_label(fye + timedelta(weeks=13 * n), fye) for n in (1, 2, 3)]
        years = [(fye + timedelta(weeks=13 * n)).year for n in (1, 2, 3)]
        assert labels == [f"{y}Q{n}" for y, n in zip(years, (1, 2, 3), strict=True)], fye
        fye = next_fye


def test_a_calendar_quarter_filer_keeps_every_label_it_had() -> None:
    """For a December filer the old label (the gateway's calendar-quarter tag) and the new are
    the same string, on every quarter end of twelve years."""
    for year in range(2014, 2026):
        fye = date(year - 1, 12, 31)
        for month, day, quarter in ((3, 31, 1), (6, 30, 2), (9, 30, 3)):
            assert quarter_label(date(year, month, day), fye) == f"{year}Q{quarter}"


def test_the_payload_fiscal_year_end_is_the_comparative_balance_sheet_column() -> None:
    end = date(2023, 9, 30)
    assert payload_fiscal_year_end(["2022-12-31", "2023-09-30"], end) == date(2022, 12, 31)
    assert payload_fiscal_year_end(["2023-09-30"], end) is None  # only its own column
    # a prior-year same-quarter instant (365 days back) is not the fiscal year end
    assert payload_fiscal_year_end(["2022-09-30", "2023-09-30"], end) is None
    # the earliest instant inside the window, never a stray later one
    assert payload_fiscal_year_end(["2023-03-31", "2022-12-31"], end) == date(2022, 12, 31)


# -- the real Waters payloads -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "period_end", "label"),
    [
        (WAT_Q1, "2023-04-01", "2023Q1"),
        (WAT_Q2, "2023-07-01", "2023Q2"),  # the gateway tagged this one (Q3)
        (WAT_Q3, "2023-09-30", "2023Q3"),
        (WAT_2021_Q3, "2021-10-02", "2021Q3"),  # tagged (Q4): a 10-Q cannot be Q4
        (WAT_2022_Q3, "2022-10-01", "2022Q3"),  # tagged (Q4)
    ],
)
def test_waters_quarters_are_labelled_from_the_fiscal_calendar(
    ref: FilingRef, period_end: str, label: str
) -> None:
    payload = _wat(ref)
    (target,) = _targets(Statements.from_payload(payload), _task())
    assert (target.period.date, target.fiscal_period) == (period_end, label)
    assert not target.fiscal_period.endswith("Q4")


def test_the_gateway_tags_two_waters_quarters_alike() -> None:
    """The defect itself, on the real payloads: both filings' own quarter column is ``(Q3)``."""
    q2 = Statements.from_payload(_wat(WAT_Q2))
    q3 = Statements.from_payload(_wat(WAT_Q3))
    assert max(q2.quarter_periods(), key=lambda p: p.date).key == "2023-07-01 (Q3)"
    assert max(q3.quarter_periods(), key=lambda p: p.date).key == "2023-09-30 (Q3)"


def test_a_stored_fiscal_year_end_counts_a_quarter_whose_payload_has_no_balance_sheet() -> None:
    payload = {**_wat(WAT_Q2), "balance_sheet": []}
    stmts = Statements.from_payload(payload)
    assert _resolve_target(stmts, _task()).startswith("no fiscal year end")  # type: ignore[union-attr]
    target = _resolve_target(stmts, _task(), lambda _before: date(2022, 12, 31))
    assert not isinstance(target, str) and target.fiscal_period == "2023Q2"


def test_the_stored_fiscal_year_end_outranks_the_payload() -> None:
    """The latest prior 10-K is the anchor; the payload's comparative column is the fallback."""
    stmts = Statements.from_payload(_wat(WAT_Q2))
    asked: list[str] = []

    def lookup(before: str) -> date:
        asked.append(before)
        return date(2022, 12, 31)

    target = _resolve_target(stmts, _task(), lookup)
    assert asked == ["2023-07-01"]
    assert not isinstance(target, str) and target.fiscal_period == "2023Q2"


def test_a_period_that_is_no_quarter_of_the_fiscal_year_is_refused_with_a_reason() -> None:
    """Never a guessed label: an anchor that puts the quarter end 46 days past a boundary."""
    stmts = Statements.from_payload(_wat(WAT_Q2))
    reason = _resolve_target(stmts, _task(), lambda _before: date(2022, 11, 14))
    assert (
        isinstance(reason, str)
        and "not a quarter end of the fiscal year ending 2022-11-14" in reason
    )


# -- a period ending in the first week of January belongs to the year before -------------------

JNJ_FY2022 = FilingRef("10-K", "2023-02-16", "0000200406-23-000016")  # year ended 2023-01-01
JNJ_FY2023 = FilingRef("10-K", "2024-02-16", "0000200406-24-000013")  # year ended 2023-12-31
JNJ_Q3 = FilingRef("10-Q", "2023-10-27", "0000200406-23-000102")  # quarter ended 2023-10-01
_JNJ_FILES = {
    JNJ_FY2022.accession_number: "financials_JNJ_10-K_2023_acc-0000200406-23-000016.json",
    JNJ_FY2023.accession_number: "financials_JNJ_10-K_2024_acc-0000200406-24-000013.json",
    JNJ_Q3.accession_number: "financials_JNJ_10-Q_2023_acc-0000200406-23-000102.json",
}


def _jnj(ref: FilingRef) -> dict[str, Any]:
    return _fixture(_JNJ_FILES[ref.accession_number])


@pytest.mark.parametrize(
    ("period_end", "year"),
    [
        ("2023-12-31", 2023),  # a calendar filer's year end: unchanged
        ("2023-03-31", 2023),
        ("2023-01-28", 2023),  # a January fiscal year end (Target): unchanged
        ("2023-01-07", 2022),  # the first week of January: the year that just ended
        ("2023-01-01", 2022),  # J&J's fiscal 2022
        ("2022-01-02", 2021),  # J&J's fiscal 2021
        ("2022-12-31", 2022),
    ],
)
def test_the_label_year_is_the_year_of_the_period_s_last_week(period_end: str, year: int) -> None:
    assert label_year(date.fromisoformat(period_end)) == year


def test_two_first_quarters_a_year_apart_get_two_labels() -> None:
    """A 4-4-5 filer with a September year end (Starbucks): its first quarter ended 2023-01-01 and
    the next year's ended 2023-12-31. Both were ``2023Q1``; the second was dropped."""
    first = quarter_label(date(2023, 1, 1), date(2022, 10, 2))
    second = quarter_label(date(2023, 12, 31), date(2023, 10, 1))
    assert (first, second) == ("2022Q1", "2023Q1")


def test_the_two_johnson_and_johnson_fiscal_years_are_labelled_apart() -> None:
    """The real 10-Ks: the year ended 2023-01-01 and the one ended 2023-12-31 were both ``FY2023``
    (the second silently dropped as already done). Now FY2022 and FY2023, with their own years."""
    for ref, period_end, label, year in (
        (JNJ_FY2022, "2023-01-01", "FY2022", 2022),
        (JNJ_FY2023, "2023-12-31", "FY2023", 2023),
    ):
        task = _YearTask(1, "JNJ", "JNJ", "10-K", 2023)
        (target,) = _targets(Statements.from_payload(_jnj(ref)), task)
        assert (target.period.date, target.fiscal_period, target.fiscal_year) == (
            period_end,
            label,
            year,
        )


def test_a_johnson_and_johnson_quarter_the_gateway_tagged_q4_is_its_third() -> None:
    """The production shape behind "a 10-Q labelled Q4": a 52/53-week December filer's third
    quarter (ended 2023-10-01) arrives as ``(Q4)``."""
    stmts = Statements.from_payload(_jnj(JNJ_Q3))
    assert max(stmts.quarter_periods(), key=lambda p: p.date).key == "2023-10-01 (Q4)"
    (target,) = _targets(stmts, _YearTask(1, "JNJ", "JNJ", "10-Q", 2023))
    assert (target.fiscal_period, target.fiscal_year) == ("2023Q3", 2023)


def test_both_johnson_and_johnson_10_ks_are_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar(
        {("10-K", 2023): [JNJ_FY2022], ("10-K", 2024): [JNJ_FY2023]},
        {ref.accession_number: _jnj(ref) for ref in (JNJ_FY2022, JNJ_FY2023)},
    )
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(
        forms=("10-K",), since_year=2023, until_year=2024, analysis_date="2026-09-21"
    )
    report = pipeline.run(_settings(tmp_path, "JNJ"), params)
    assert (report.completed, report.skipped, report.failed) == (2, 0, 0)
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute(
        "SELECT fiscal_year, fiscal_period, period_end FROM sec_filings ORDER BY period_end"
    ).fetchall() == [(2022, "FY2022", "2023-01-01"), (2023, "FY2023", "2023-12-31")]


# -- the prior-year pairing reads dates, not the gateway's tag ----------------------------------


def test_a_quarter_is_paired_with_the_quarter_that_ended_a_year_before() -> None:
    """Waters' real Q3: ``2023-09-30 (Q3)`` beside ``2022-10-01 (Q4)``. Pairing by tag found no
    prior year, so the very filing the resume key used to drop would have been stored without a
    year-over-year comparison; the date finds it."""
    (target,) = _targets(Statements.from_payload(_wat(WAT_Q3)), _task())
    assert target.period.key == "2023-09-30 (Q3)"
    assert target.prior is not None and target.prior.key == "2022-10-01 (Q4)"


def test_a_calendar_filer_s_prior_year_quarter_is_the_same_column_as_before() -> None:
    columns = ("2025-09-30 (Q3)", "2025-09-30 (YTD)", "2024-09-30 (Q3)", "2024-09-30 (YTD)")
    row = {"concept": "x", "label": "x", "abstract": False, "dimension": False}
    stmts = Statements.from_payload(
        {
            "income_statement": [{**row, **dict.fromkeys(columns, 1.0)}],
            "balance_sheet": [],
            "cash_flow": [],
        }
    )
    current = next(p for p in stmts.periods if p.key == "2025-09-30 (Q3)")
    prior = stmts.prior_of(current)
    assert prior is not None and prior.key == "2024-09-30 (Q3)"
    assert stmts.prior_of(next(p for p in stmts.periods if p.key == "2025-09-30 (YTD)")).key == (  # type: ignore[union-attr]
        "2024-09-30 (YTD)"
    )


def test_a_quarter_with_no_twin_a_year_back_has_no_prior() -> None:
    row = {"concept": "x", "label": "x", "abstract": False, "dimension": False}
    stmts = Statements.from_payload(
        {
            "income_statement": [{**row, "2025-09-30 (Q3)": 1.0, "2025-06-30 (Q2)": 1.0}],
            "balance_sheet": [],
            "cash_flow": [],
        }
    )
    assert stmts.prior_of(next(p for p in stmts.periods if p.key == "2025-09-30 (Q3)")) is None


# -- a full run: nothing is dropped, nothing is silent ------------------------------------------


def _settings(tmp_path: Path, ticker: str) -> Settings:
    udb = tmp_path / "universe.db"
    if not udb.exists():
        write_universe_db(udb, [(ticker, "2020-01-01", None)])
    return Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=udb,
        llm_api_key="k",
        llm_model="deepseek-chat",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    edgar: _Edgar,
    ticker: str,
    *,
    year: int = 2023,
) -> pipeline.RunReport:
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(
        forms=("10-Q",), since_year=year, until_year=year, analysis_date="2026-09-21"
    )
    return pipeline.run(_settings(tmp_path, ticker), params)


def _filings(tmp_path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(tmp_path / "kg.db")
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT fiscal_period, period_end, accession_number FROM sec_filings ORDER BY period_end"
        )
    ]


def _waters_2023() -> _Edgar:
    return _Edgar(
        {("10-Q", 2023): [WAT_Q3, WAT_Q2, WAT_Q1]},
        {ref.accession_number: _wat(ref) for ref in (WAT_Q1, WAT_Q2, WAT_Q3)},
    )


def test_waters_2023_stores_all_three_quarters_under_their_own_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pilot-1 defect: the 2023-09-30 10-Q was skipped as "already done" because the
    2023-07-01 one carried the same ``(Q3)`` tag. Both are stored now, as Q2 and Q3."""
    report = _run(monkeypatch, tmp_path, _waters_2023(), "WAT")
    assert (report.completed, report.skipped, report.failed) == (3, 0, 0)
    assert _filings(tmp_path) == [
        ("2023Q1", "2023-04-01", WAT_Q1.accession_number),
        ("2023Q2", "2023-07-01", WAT_Q2.accession_number),
        ("2023Q3", "2023-09-30", WAT_Q3.accession_number),
    ]


def test_a_resumed_run_keys_on_the_period_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run(monkeypatch, tmp_path, _waters_2023(), "WAT")
    again = _waters_2023()
    report = _run(monkeypatch, tmp_path, again, "WAT")
    assert (report.completed, report.skipped, report.failed) == (0, 3, 0)
    assert again.financials_calls == []
    with closing(connect(tmp_path / "kg.db")) as conn:
        assert db.completed_units(conn) == {
            ("WAT", "10-Q", d) for d in ("2023-04-01", "2023-07-01", "2023-09-30")
        }


def test_a_repeated_label_can_never_again_be_a_silent_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pilot-1 shape reproduced on purpose: a labeller that gives the 2023-07-01 and
    2023-09-30 quarters the same label, as the gateway's tag did. The second filing is no longer
    "already done" -- the resume key is its period end -- so it reaches the store, which refuses
    to overwrite the first under that label: a recorded failure, never a quiet skip."""
    monkeypatch.setattr(pipeline, "quarter_label", lambda end, _fye: f"{end.year}Q3")
    edgar = _Edgar(
        {("10-Q", 2023): [WAT_Q3, WAT_Q2]},
        {ref.accession_number: _wat(ref) for ref in (WAT_Q2, WAT_Q3)},
    )
    report = _run(monkeypatch, tmp_path, edgar, "WAT")
    assert (report.completed, report.skipped, report.failed) == (1, 0, 1)
    assert "already holds the filing for period end 2023-07-01" in report.errors[0]
    assert [r[:2] for r in _filings(tmp_path)] == [("2023Q3", "2023-07-01")]


def test_apo_q1_2023_records_why_it_has_no_quarter_of_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway returns only the prior fiscal year's column (``2022-12-31 (FY)``) for APO's
    Q1-2023 10-Q: before T-140 nothing was stored, counted or logged. Now it is a recorded skip."""
    edgar = _Edgar(
        {("10-Q", 2023): [APO_Q1]},
        {
            APO_Q1.accession_number: _fixture(
                "financials_APO_10-Q_2023_acc-0001858681-23-000017.json"
            )
        },
    )
    report = _run(monkeypatch, tmp_path, edgar, "APO")
    assert (report.completed, report.skipped, report.failed) == (0, 1, 0)
    assert _filings(tmp_path) == []
    assert len(report.errors) == 1 and "[skipped]" in report.errors[0]
    conn = sqlite3.connect(tmp_path / "kg.db")
    (ticker, form, stage, message) = conn.execute(
        "SELECT ticker, form, stage, message FROM analysis_run_error"
    ).fetchone()
    assert (ticker, form, stage) == ("APO", "10-Q", "period")
    assert APO_Q1.accession_number in message
    assert "no quarter column of its own" in message and "2022-12-31 (FY)" in message


def test_a_comparative_only_quarter_column_is_not_the_filings_own() -> None:
    """A payload whose latest quarter column ends months before its balance sheet carries only a
    comparative; storing it would stamp last year's quarter with this filing's accession."""
    payload = _wat(WAT_Q3)
    payload["income_statement"] = [
        {k: v for k, v in row.items() if k not in ("2023-09-30 (Q3)", "2023-09-30 (YTD)")}
        for row in payload["income_statement"]
    ]
    reason = _resolve_target(Statements.from_payload(payload), _task())
    assert isinstance(reason, str) and "a comparative, not the filing's own quarter" in reason
    assert _targets(Statements.from_payload(payload), _task()) == []


def test_a_database_labelled_before_t140_fails_loudly_instead_of_overwriting_a_filing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A row of the old scheme holds the label ``2023Q3`` for the 2023-07-01 quarter. The real Q3
    (2023-09-30) must not silently replace it in place: the run records a failure for it."""
    _run(monkeypatch, tmp_path, _waters_2023(), "WAT")
    conn = sqlite3.connect(tmp_path / "kg.db")
    conn.execute("DELETE FROM sec_filings WHERE period_end = '2023-09-30'")
    conn.execute("UPDATE sec_filings SET fiscal_period = '2023Q3' WHERE period_end = '2023-07-01'")
    conn.commit()
    conn.close()
    edgar = _Edgar({("10-Q", 2023): [WAT_Q3]}, {WAT_Q3.accession_number: _wat(WAT_Q3)})
    report = _run(monkeypatch, tmp_path, edgar, "WAT")
    assert (
        report.failed == 1
        and "already holds the filing for period end 2023-07-01" in report.errors[0]
    )
    assert [r[:2] for r in _filings(tmp_path)] == [
        ("2023Q1", "2023-04-01"),
        ("2023Q3", "2023-07-01"),
    ]


def test_upsert_refuses_a_label_held_by_another_period_and_accepts_a_re_ingest(
    memory_db: Database,
) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'WAT')")
    key = FilingKey(1, "10-Q", 2023, "2023Q3")
    first = db.upsert_filing(
        memory_db,
        key,
        FilingMeta(filing_date="2023-08-02", accession_number="a", period_end="2023-07-01"),
    )
    again = db.upsert_filing(
        memory_db,
        key,
        FilingMeta(filing_date="2023-08-02", accession_number="a", period_end="2023-07-01"),
    )
    assert again == first  # the same period re-ingested: an upsert, as before
    with pytest.raises(FilingLabelCollision, match="2023-07-01"):
        db.upsert_filing(
            memory_db,
            key,
            FilingMeta(filing_date="2023-11-07", accession_number="b", period_end="2023-09-30"),
        )
    row = memory_db.execute("SELECT period_end, accession_number FROM sec_filings").fetchone()
    assert (row["period_end"], row["accession_number"]) == ("2023-07-01", "a")


def test_the_latest_prior_10_k_is_the_fiscal_year_end(memory_db: Database) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker) VALUES (1, 'WAT')")
    for year, end in ((2021, "2021-12-31"), (2022, "2022-12-31"), (2023, "2023-12-31")):
        db.upsert_filing(
            memory_db,
            FilingKey(1, "10-K", year, f"FY{year}"),
            FilingMeta(filing_date=f"{year + 1}-02-20", period_end=end),
        )
    db.upsert_filing(
        memory_db,
        FilingKey(1, "10-Q", 2023, "2023Q1"),
        FilingMeta(filing_date="2023-05-09", period_end="2023-04-01"),
    )
    assert db.latest_fiscal_year_end(memory_db, 1, "2023-07-01") == "2022-12-31"
    assert db.latest_fiscal_year_end(memory_db, 1, "2021-04-03") is None
    assert db.latest_fiscal_year_end(memory_db, 2, "2023-07-01") is None
