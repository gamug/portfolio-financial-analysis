"""Pipeline planning, target expansion, and a full stubbed run with resume."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database

from fundamental_agent import db, pipeline
from fundamental_agent.agents import AnalysisResult, FilingContext, FundamentalAssessment
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.edgar_client import FilingRef
from fundamental_agent.metrics import compute_group
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import RunParams, _Engine, _plan, _targets, _ttm_flows, _YearTask
from fundamental_agent.statements import Statements
from kg_schema.queries import UniverseMember

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text())
    return cast("dict[str, Any]", data["data"])


def _task(ticker: str, form: str, year: int) -> _YearTask:
    return _YearTask(asset_id=1, ticker=ticker, company_name=ticker, form=form, year=year)


def test_plan_is_asset_x_form_x_year() -> None:
    params = RunParams(forms=["10-K", "10-Q"], since_year=2022, until_year=2024)
    tasks = _plan(cast("Any", _fake_rows()), params)
    assert len(tasks) == 2 * 2 * 3  # 2 assets, 2 forms, 3 years
    assert {t.ticker for t in tasks} == {"AAPL", "MSFT"}


def test_targets_10k_picks_latest_fiscal_year() -> None:
    stmts = Statements.from_payload(_payload("financials_AAPL_10-K_2023.json"))
    targets = _targets(stmts, _task("AAPL", "10-K", 2023))
    assert len(targets) == 1
    assert targets[0].fiscal_period == "FY2023"
    prior = targets[0].prior
    assert prior is not None
    assert prior.date == "2022-09-24"


def test_targets_10q_is_the_filings_own_quarter() -> None:
    stmts = Statements.from_payload(_payload("financials_MSFT_10-Q_2024.json"))
    assert [t.fiscal_period for t in _targets(stmts, _task("MSFT", "10-Q", 2024))] == ["2024Q1"]
    # task.year is the *filing* year, not a period filter (a January 10-Q reports a
    # December quarter): the own period comes from the payload, whatever year was asked.
    assert [t.fiscal_period for t in _targets(stmts, _task("MSFT", "10-Q", 2019))] == ["2024Q1"]


def _member(symbol: str) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        security=symbol,
        cik="0000000001",
        gics_sector="Technology",
        gics_sub_industry="Sub",
        hq_location=None,
        date_added=None,
        founded=None,
        valid_from="2020-01-01",
        valid_to=None,
    )


def _engine(conn: Database) -> _Engine:
    """A minimal `_Engine` for `_ttm_flows`, which only touches `.conn`."""
    return _Engine(
        conn=conn,
        edgar=cast("Any", None),
        analyst=cast("Any", None),
        params=RunParams(),
        report=cast("Any", None),
        completed=set(),
    )


def test_ttm_flows_empty_for_10k(memory_db: Database) -> None:
    """A 10-K already reports an annual flow -- no TTM adjustment applies."""
    db.sync_universe(memory_db, [_member("AAPL")])
    stmts = Statements.from_payload(_payload("financials_AAPL_10-K_2023.json"))
    task = _task("AAPL", "10-K", 2023)
    targets = _targets(stmts, task)

    assert _ttm_flows(_engine(memory_db), task, stmts, targets[0]) == {}


def test_ttm_flows_reads_the_prior_quarters_by_the_targets_period_end(
    memory_db: Database,
) -> None:
    """The pipeline hands ``db.ttm_flows`` the target's own period-end date (T-094); the three
    preceding quarters, 3/6/9 months earlier, are found by it -- not by any label."""
    db.sync_universe(memory_db, [_member("MSFT")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    stmts = Statements.from_payload(_payload("financials_MSFT_10-Q_2024.json"))
    task = _YearTask(asset_id=asset_id, ticker="MSFT", company_name="MSFT", form="10-Q", year=2024)
    target = _targets(stmts, task)[0]
    assert target.period.date == "2024-09-30"
    # seed the three prior quarters (a June fiscal year: 2024-06-30 is its Q4-adjacent 10-Q end)
    for label, end, ni in (("2024Q3", "2024-06-30", 30.0), ("2024Q2", "2024-03-31", 20.0)):
        fid = db.upsert_filing(
            memory_db,
            FilingKey(asset_id, "10-Q", 2024, label),
            FilingMeta(period_end=end),
        )
        inputs = {"net_income": ni, "revenue": 1.0}
        db.record_metrics(
            memory_db, fid, [("profitability", MetricResult("return_on_assets", None, "r", inputs))]
        )
    fid = db.upsert_filing(
        memory_db, FilingKey(asset_id, "10-Q", 2023, "2023Q1"), FilingMeta(period_end="2023-12-31")
    )
    db.record_metrics(
        memory_db,
        fid,
        [("profitability", MetricResult("return_on_assets", None, "r", {"net_income": 10.0}))],
    )
    current = stmts.get("net_income", target.period.key)
    assert current is not None

    result = _ttm_flows(_engine(memory_db), task, stmts, target)

    assert result["net_income"] == current + 30.0 + 20.0 + 10.0


def test_ttm_flows_falls_back_to_times_four_for_a_fresh_10q(memory_db: Database) -> None:
    """No prior filings ingested yet -- every flow item in the payload falls
    back to `current * 4` (F4, docs/model_fixes.md)."""
    db.sync_universe(memory_db, [_member("MSFT")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    stmts = Statements.from_payload(_payload("financials_MSFT_10-Q_2024.json"))
    task = _YearTask(asset_id=asset_id, ticker="MSFT", company_name="MSFT", form="10-Q", year=2024)
    targets = _targets(stmts, task)

    result = _ttm_flows(_engine(memory_db), task, stmts, targets[0])

    expected = {
        item: value * 4
        for item in ("net_income", "revenue", "cogs")
        if (value := stmts.get(item, targets[0].period.key)) is not None
    }
    assert expected  # the fixture does carry at least one of these
    assert result == expected


def _fake_rows() -> list[dict[str, Any]]:
    return [
        {"id": 1, "ticker": "AAPL", "company_name": "Apple", "cik": "1"},
        {"id": 2, "ticker": "MSFT", "company_name": "Microsoft", "cik": "2"},
    ]


class _FakeEdgar:
    """Stands in for EdgarClient: serves fixture payloads, no network."""

    def __init__(self, *_a: object, **_k: object) -> None:
        self._by_form = {
            "10-K": _payload("financials_AAPL_10-K_2023.json"),
            "10-Q": _payload("financials_MSFT_10-Q_2024.json"),
        }

    def __enter__(self) -> _FakeEdgar:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict:
        return self._by_form[form]

    def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]:
        return [FilingRef(form, f"{year}-02-01", f"acc-{ticker}-{year}")]


class _StubAnalyst:
    def __init__(self, _model: object, model_name: str) -> None:
        self.model_name = model_name

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        pairs: list = []
        for group in ("profitability", "liquidity", "leverage", "cashflow"):
            for result in compute_group(group, ctx.stmts, ctx.period_key, ctx.prior_key):
                pairs.append((group, result))
        assessment = FundamentalAssessment(
            score=72.0, rating="bullish", narrative="stub", strengths=["s"], risks=["r"]
        )
        return AnalysisResult(
            assessment=assessment,
            metrics=pairs,
            flat_metrics={f"{g}.{r.name}": r.value for g, r in pairs},
        )


@pytest.fixture
def _stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)


def _settings(tmp_path: Path) -> Settings:
    udb = write_universe_db(
        tmp_path / "universe.db",
        [("AAPL", "2020-01-01", None), ("MSFT", "2020-01-01", None)],
    )
    return Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=udb,
        llm_api_key="k",
        llm_model="deepseek-chat",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )


@pytest.mark.usefixtures("_stubbed")
def test_run_writes_snapshots_then_resumes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    params = RunParams(forms=["10-K"], since_year=2023, until_year=2024)

    first = pipeline.run(settings, params)
    assert first.completed == 2  # one FY2023 snapshot per ticker
    assert first.skipped == 2  # the 2024 task dedupes onto FY2023

    conn = sqlite3.connect(settings.db_path)
    snaps = list(
        conn.execute(
            "SELECT a.ticker, f.fiscal_period, s.raw_value, s.rating "
            "FROM score_snapshot s JOIN assets a ON a.id = s.asset_id "
            "JOIN sec_filings f ON f.id = s.filing_id "
            "WHERE s.score_type = 'FUNDAMENTAL' ORDER BY a.ticker"
        )
    )
    assert [(r[0], r[1], r[2], r[3]) for r in snaps] == [
        ("AAPL", "FY2023", 72.0, "bullish"),
        ("MSFT", "FY2023", 72.0, "bullish"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM fundamental_metrics").fetchone()[0] > 0
    run_row = conn.execute(
        "SELECT status, completed_units FROM analysis_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run_row[0] == "completed"

    second = pipeline.run(settings, params)
    assert second.completed == 0
    assert second.skipped == 4
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.usefixtures("_stubbed")
def test_run_gates_every_filing_it_analyses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-065: the Ring-1 data-quality gates run over each newly stored filing's metrics,
    under the engine version just written and the run that wrote them."""
    calls: list[tuple[str, int | None, int | None]] = []
    real = pipeline.quality.gate_version

    def spy(
        conn: Any, version: str, *, filing_id: int | None = None, run_id: int | None = None
    ) -> Any:
        calls.append((version, filing_id, run_id))
        return real(conn, version, filing_id=filing_id, run_id=run_id)

    monkeypatch.setattr(pipeline.quality, "gate_version", spy)
    report = pipeline.run(
        _settings(tmp_path), RunParams(forms=["10-K"], since_year=2023, until_year=2023)
    )
    assert report.completed == 2
    assert len(calls) == 2
    assert {c[0] for c in calls} == {db.METRICS_ENGINE_VERSION}
    assert {c[2] for c in calls} == {report.run_id}
    assert all(c[1] is not None for c in calls)
