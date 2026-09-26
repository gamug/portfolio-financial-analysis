"""The pipeline ingests every filing of a year, each under its own accession (T-092).

``portfolio-data-mining`` PR #39 made ``filing_by_year`` return every filing for a form and
year and ``financials`` take an ``accession_number``. The fixture is a *real* STZ 10-Q whose
payload carries a Q1 comparative beside its own Q2 -- the case that used to create a filing
row for the comparative stamped with the Q2 filing's accession.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import write_universe_db

from fundamental_agent import pipeline
from fundamental_agent.agents import AnalysisResult, FilingContext, FundamentalAssessment
from fundamental_agent.config import Settings
from fundamental_agent.edgar_client import EdgarError, EdgarNotFoundError, FilingRef
from fundamental_agent.metrics import compute_group
from fundamental_agent.pipeline import RunParams, _select_filings, _targets, _YearTask
from fundamental_agent.statements import Statements

FIXTURES = Path(__file__).parent / "fixtures"
# Real STZ 2023 10-Qs, as filing_by_year lists them (most recent first).
Q1_JUN = FilingRef("10-Q", "2023-06-30", "0000016918-23-000103")
Q2_OCT = FilingRef("10-Q", "2023-10-05", "0000016918-23-000190")
_LISTED = [Q2_OCT, Q1_JUN]
_PERIOD_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}( \(.*\))?$")


def _stz_q2_payload() -> dict[str, Any]:
    raw = json.loads(
        (FIXTURES / "financials_STZ_10-Q_2023_acc-0000016918-23-000190.json").read_text()
    )
    return cast("dict[str, Any]", raw["data"])


def _only_columns(payload: dict[str, Any], keep: set[str]) -> dict[str, Any]:
    """*payload* with every period column but *keep* dropped (rows keep their non-period keys)."""
    return {
        statement: [
            {k: v for k, v in row.items() if not _PERIOD_KEY.match(k) or k in keep} for row in rows
        ]
        for statement, rows in payload.items()
    }


def _q1_payload() -> dict[str, Any]:
    """The Q1 filing: the real Q2 payload cut down to its Q1 columns."""
    return _only_columns(_stz_q2_payload(), {"2023-05-31 (Q1)", "2022-05-31 (Q1)"})


def _task(form: str = "10-Q", year: int = 2023) -> _YearTask:
    return _YearTask(asset_id=1, ticker="STZ", company_name="STZ", form=form, year=year)


# -- _targets: the filing's own period only ------------------------------------------------


def test_the_real_q2_payload_carries_a_q1_comparative_but_yields_only_its_own_quarter() -> None:
    stmts = Statements.from_payload(_stz_q2_payload())
    assert "2023-05-31 (Q1)" in {p.key for p in stmts.periods}  # the comparative is in there
    targets = _targets(stmts, _task())
    assert [t.fiscal_period for t in targets] == ["2023Q2"]
    assert targets[0].period.date == "2023-08-31"
    assert targets[0].prior is not None and targets[0].prior.date == "2022-08-31"


def test_a_q1_only_payload_yields_q1() -> None:
    targets = _targets(Statements.from_payload(_q1_payload()), _task())
    assert [t.fiscal_period for t in targets] == ["2023Q1"]


# -- _select_filings: oldest first --------------------------------------------------------


def test_filings_are_ingested_oldest_first_whatever_order_the_gateway_lists_them() -> None:
    newest_first = [Q2_OCT, Q1_JUN]
    assert [r.accession_number for r in _select_filings(_task(), newest_first)] == [
        Q1_JUN.accession_number,
        Q2_OCT.accession_number,
    ]


def test_a_10k_keeps_only_the_most_recent_of_several_matches() -> None:
    original = FilingRef("10-K", "2023-04-20", "acc-orig")
    amendment = FilingRef("10-K", "2023-09-01", "acc-amend")
    picked = _select_filings(_task("10-K"), [amendment, original])
    assert [r.accession_number for r in picked] == ["acc-amend"]


# -- a full run against a fake gateway -----------------------------------------------------


class _Edgar:
    """Stands in for EdgarClient: lists filings and serves payloads per accession."""

    def __init__(
        self,
        filings: dict[tuple[str, int], list[FilingRef]],
        payloads: dict[str, dict[str, Any]],
        *,
        raises: dict[str, Exception] | None = None,
        listing_raises: Exception | None = None,
    ) -> None:
        self.filings = filings
        self.payloads = payloads
        self.raises = raises or {}
        self.listing_raises = listing_raises
        self.financials_calls: list[str | None] = []
        self.listing_calls: list[str] = []

    def __call__(self, *_a: object, **_k: object) -> _Edgar:  # EdgarClient(...) -> this fake
        return self

    def __enter__(self) -> _Edgar:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]:
        self.listing_calls.append(ticker)
        if self.listing_raises is not None:
            raise self.listing_raises
        return self.filings.get((form, year), [])

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, Any]:
        self.financials_calls.append(accession_number)
        if accession_number in self.raises:
            raise self.raises[cast("str", accession_number)]
        return self.payloads[cast("str", accession_number)]


class _StubAnalyst:
    def __init__(self, _model: object, model_name: str) -> None:
        self.model_name = model_name

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        pairs: list[Any] = []
        for group in ("profitability", "liquidity", "leverage", "cashflow"):
            for result in compute_group(group, ctx.stmts, ctx.period_key, ctx.prior_key):
                pairs.append((group, result))
        assessment = FundamentalAssessment(
            score=60.0, rating="neutral", narrative="stub", strengths=["s"], risks=["r"]
        )
        return AnalysisResult(
            assessment=assessment,
            metrics=pairs,
            flat_metrics={f"{g}.{r.name}": r.value for g, r in pairs},
        )


def _settings(tmp_path: Path) -> Settings:
    udb = tmp_path / "universe.db"
    if not udb.exists():  # a second run in the same directory reuses it
        write_universe_db(udb, [("STZ", "2020-01-01", None)])
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
    *,
    analysis_date: str = "2026-09-21",
    forms: tuple[str, ...] = ("10-Q",),
) -> pipeline.RunReport:
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    params = RunParams(forms=forms, since_year=2023, until_year=2023, analysis_date=analysis_date)
    return pipeline.run(_settings(tmp_path), params)


def _two_quarters() -> _Edgar:
    return _Edgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )


def _filing_rows(tmp_path: Path) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(tmp_path / "kg.db")
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT fiscal_period, accession_number, filing_date, period_end "
            "FROM sec_filings ORDER BY period_end"
        )
    ]


def test_each_quarter_becomes_one_filing_under_its_own_accession(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _two_quarters()
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed) == (2, 0)
    # Q1 under the June filing, Q2 under the October one -- and *nothing else*: the Q1
    # comparative inside the Q2 payload did not become a second row for the Q2 accession.
    assert _filing_rows(tmp_path) == [
        ("2023Q1", Q1_JUN.accession_number, "2023-06-30", "2023-05-31"),
        ("2023Q2", Q2_OCT.accession_number, "2023-10-05", "2023-08-31"),
    ]
    accessions = [r[1] for r in _filing_rows(tmp_path)]
    assert len(accessions) == len(set(accessions))  # no accession carries two fiscal periods


def test_filings_are_fetched_and_recorded_oldest_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F4's TTM reads the prior quarters' already-recorded values, so Q1 must land first."""
    edgar = _two_quarters()
    _run(monkeypatch, tmp_path, edgar)
    assert edgar.financials_calls == [Q1_JUN.accession_number, Q2_OCT.accession_number]
    conn = sqlite3.connect(tmp_path / "kg.db")
    order = [
        r[0]
        for r in conn.execute(
            "SELECT f.fiscal_period FROM score_snapshot s JOIN sec_filings f ON f.id = s.filing_id "
            "ORDER BY s.id"
        )
    ]
    assert order == ["2023Q1", "2023Q2"]


def test_a_resumed_run_skips_the_financials_call_for_ingested_accessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run(monkeypatch, tmp_path, _two_quarters())
    second_edgar = _two_quarters()
    second = _run(monkeypatch, tmp_path, second_edgar)
    assert second.completed == 0 and second.skipped == 2
    assert second_edgar.financials_calls == []  # the listing is still one cheap call
    assert second_edgar.listing_calls == ["STZ"]


def test_a_filing_dated_after_the_analysis_date_is_skipped_without_being_fetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _two_quarters()
    report = _run(monkeypatch, tmp_path, edgar, analysis_date="2023-08-01")
    assert (report.completed, report.skipped) == (1, 1)
    assert edgar.financials_calls == [Q1_JUN.accession_number]  # October was never fetched


def test_a_failing_filing_does_not_stop_its_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _two_quarters()
    edgar.raises[Q1_JUN.accession_number] = EdgarError("boom")
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.failed) == (1, 1)
    assert Q1_JUN.accession_number in report.errors[0] and "boom" in report.errors[0]
    assert [r[0] for r in _filing_rows(tmp_path)] == ["2023Q2"]


def test_a_year_with_no_filing_is_a_quiet_skip_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """APO filed no 10-K in 2022: the gateway now says ``data: []`` (it used to be an error)."""
    edgar = _Edgar({}, {})
    report = _run(monkeypatch, tmp_path, edgar, forms=("10-K",))
    assert (report.completed, report.skipped, report.failed) == (0, 1, 0)
    assert edgar.financials_calls == []


def test_a_company_no_spelling_matches_is_a_failure_worth_seeing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar({}, {}, listing_raises=EdgarNotFoundError("Company not found"))
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.skipped, report.failed) == (0, 1)
    assert "no EDGAR match" in report.errors[0]


def test_the_pre_pr39_object_payload_fails_loudly_instead_of_crashing_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    edgar = _Edgar({}, {}, listing_raises=EdgarError("returned dict, expected a list"))
    report = _run(monkeypatch, tmp_path, edgar)
    assert report.failed == 1 and "expected a list" in report.errors[0]


def test_an_undated_filing_is_skipped_and_the_run_carries_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T-107: a filing the gateway lists with no date can never be shown usable, so it is not
    fetched, scored or stored -- the database would refuse its metrics anyway."""
    undated = FilingRef("10-Q", None, Q1_JUN.accession_number)
    edgar = _Edgar(
        {("10-Q", 2023): [Q2_OCT, undated]},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert (report.completed, report.skipped, report.failed) == (1, 1, 0)
    assert edgar.financials_calls == [Q2_OCT.accession_number]
    conn = sqlite3.connect(tmp_path / "kg.db")
    assert conn.execute("SELECT fiscal_period, available_at FROM sec_filings").fetchall() == [
        ("2023Q2", "2023-10-06")  # filed Thursday 2023-10-05
    ]
