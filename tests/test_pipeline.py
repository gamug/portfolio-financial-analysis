"""Pipeline planning, target expansion, and a full stubbed run with resume."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from fundamental_agent import pipeline
from fundamental_agent.agents import AnalysisResult, FilingContext, FundamentalAssessment
from fundamental_agent.config import Settings
from fundamental_agent.metrics import compute_group
from fundamental_agent.pipeline import RunParams, _plan, _targets, _YearTask
from fundamental_agent.statements import Statements
from fundamental_agent.universe import Company

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


def test_targets_10q_expands_matching_year_quarters() -> None:
    stmts = Statements.from_payload(_payload("financials_MSFT_10-Q_2024.json"))
    targets = _targets(stmts, _task("MSFT", "10-Q", 2024))
    assert [t.fiscal_period for t in targets] == ["2024Q1"]
    assert _targets(stmts, _task("MSFT", "10-Q", 2019)) == []


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

    def financials(self, ticker: str, form: str, year: int) -> dict:
        return self._by_form[form]

    def filing_by_year(self, ticker: str, form: str, year: int) -> dict:
        return {"filing_date": f"{year}-02-01", "accession_number": f"acc-{ticker}-{year}"}


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
    monkeypatch.setattr(
        pipeline,
        "fetch_sp500",
        lambda: [
            Company(symbol="AAPL", name="Apple", cik="1", sector="Tech", sub_industry="x"),
            Company(symbol="MSFT", name="Microsoft", cik="2", sector="Tech", sub_industry="x"),
        ],
    )
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "kg.db",
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
            "SELECT a.ticker, s.fiscal_period, s.score, s.rating "
            "FROM fundamental_snapshot s JOIN assets a ON a.id = s.asset_id ORDER BY a.ticker"
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
    assert conn.execute("SELECT COUNT(*) FROM fundamental_snapshot").fetchone()[0] == 2
