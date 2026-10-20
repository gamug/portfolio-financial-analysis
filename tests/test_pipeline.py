"""Pipeline planning, target expansion, and a full stubbed run with resume."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from conftest import filed_after, write_universe_db
from portfolio_common.db import Database
from test_pipeline_multi_filing import (
    _LISTED,
    Q1_JUN,
    Q2_OCT,
    _Edgar,
    _q1_payload,
    _run,
    _stz_q2_payload,
)
from test_pipeline_multi_filing import _settings as _stz_settings

from fundamental_agent import cli as fundamental_cli
from fundamental_agent import db, pipeline
from fundamental_agent.agents import (
    FALLBACK_MODEL_LABEL,
    AnalysisResult,
    FilingContext,
    FundamentalAssessment,
)
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta
from fundamental_agent.edgar_client import EdgarError, FilingRef
from fundamental_agent.metrics import compute_group, valuation
from fundamental_agent.metrics.base import MetricResult, TTMFlow
from fundamental_agent.pipeline import (
    _TTM_ITEMS,
    RunParams,
    _Engine,
    _plan,
    _targets,
    _ttm_flows,
    _YearTask,
)
from fundamental_agent.statements import Statements
from kg_schema.provenance import DirtyTree
from kg_schema.queries import UniverseMember

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text())
    return cast("dict[str, Any]", data["data"])


def _task(ticker: str, form: str, year: int) -> _YearTask:
    return _YearTask(
        asset_id=1, ticker=ticker, company_name=ticker, form=form, year=year, cik="0000000001"
    )


def test_plan_is_asset_x_form_x_year() -> None:
    params = RunParams(forms=["10-K", "10-Q"], since_year=2022, until_year=2024)
    tasks = _plan(cast("Any", _fake_rows()), params)
    assert len(tasks) == 2 * 2 * 3  # 2 assets, 2 forms, 3 years
    assert {t.ticker for t in tasks} == {"AAPL", "MSFT"}


def test_plan_bounds_a_leaver_by_its_exit_year() -> None:
    """T-153 part 3a: a company that left is listed only through its exit year, and its task
    carries the exit day so ``_process_filing`` drops anything filed after it."""
    params = RunParams(forms=["10-K"], since_year=2022, until_year=2026)
    tasks = _plan(cast("Any", _fake_rows()), params, until={"AAPL": "2024-06-15"})
    aapl = [t for t in tasks if t.ticker == "AAPL"]
    assert sorted(t.year for t in aapl) == [2022, 2023, 2024]
    assert {t.until for t in aapl} == {"2024-06-15"}
    msft = [t for t in tasks if t.ticker == "MSFT"]
    assert sorted(t.year for t in msft) == [2022, 2023, 2024, 2025, 2026]
    assert {t.until for t in msft} == {None}


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
    task = _YearTask(
        asset_id=asset_id,
        ticker="MSFT",
        company_name="MSFT",
        form="10-Q",
        year=2024,
        cik="0000789019",
    )
    target = _targets(stmts, task)[0]
    assert target.period.date == "2024-09-30"
    # seed the three prior quarters (a June fiscal year: 2024-06-30 is its Q4-adjacent 10-Q end)
    for label, end, ni in (("2024Q3", "2024-06-30", 30.0), ("2024Q2", "2024-03-31", 20.0)):
        fid = db.upsert_filing(
            memory_db,
            FilingKey(asset_id, "10-Q", 2024, label),
            FilingMeta(filing_date=filed_after(end), period_end=end),
        )
        inputs = {"net_income": ni, "revenue": 1.0}
        db.record_metrics(
            memory_db, fid, [("profitability", MetricResult("return_on_assets", None, "r", inputs))]
        )
    fid = db.upsert_filing(
        memory_db,
        FilingKey(asset_id, "10-Q", 2023, "2023Q1"),
        FilingMeta(filing_date=filed_after("2023-12-31"), period_end="2023-12-31"),
    )
    db.record_metrics(
        memory_db,
        fid,
        [("profitability", MetricResult("return_on_assets", None, "r", {"net_income": 10.0}))],
    )
    current = stmts.get("net_income", target.period.key)
    assert current is not None

    result = _ttm_flows(_engine(memory_db), task, stmts, target)

    assert result["net_income"] == TTMFlow(current + 30.0 + 20.0 + 10.0, "quarters")


def test_ttm_flows_falls_back_to_times_four_for_a_fresh_10q(memory_db: Database) -> None:
    """No prior filings ingested yet -- every flow item in the payload falls
    back to `current * 4` (F4, docs/model_fixes.md)."""
    db.sync_universe(memory_db, [_member("MSFT")])
    asset_id = db.load_universe(memory_db)[0]["id"]
    stmts = Statements.from_payload(_payload("financials_MSFT_10-Q_2024.json"))
    task = _YearTask(
        asset_id=asset_id,
        ticker="MSFT",
        company_name="MSFT",
        form="10-Q",
        year=2024,
        cik="0000789019",
    )
    targets = _targets(stmts, task)

    result = _ttm_flows(_engine(memory_db), task, stmts, targets[0])

    expected = {
        item: TTMFlow(value * 4, "x4")
        for item in _TTM_ITEMS
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
        if ctx.price is not None:
            ttm_floats = {k: v.value for k, v in ctx.ttm_flows.items()} if ctx.ttm_flows else None
            for result in valuation.compute(
                ctx.stmts,
                ctx.period_key,
                ctx.price,
                share_scale_factors=ctx.share_scale_factors,
                ttm=ttm_floats,
                cik=ctx.cik,
                market_cap=ctx.market_cap,
                market_cap_missing=ctx.market_cap_missing,
            ):
                pairs.append(("valuation", result))
        assessment = FundamentalAssessment(
            score=72.0, rating="bullish", narrative="stub", strengths=["s"], risks=["r"]
        )
        return AnalysisResult(
            assessment=assessment,
            metrics=pairs,
            flat_metrics={f"{g}.{r.name}": r.value for g, r in pairs},
            used_fallback=False,
            prompt_hash="stub-hash",
        )


class _FallbackStubAnalyst(_StubAnalyst):
    """T-113: the LLM's JSON verdict never came back parseable -- every filing falls back."""

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        return replace(super().analyze(ctx), used_fallback=True, prompt_hash="fallback-hash")


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


def test_tickers_take_the_symbol_of_the_run_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: ``--tickers FB`` on 2022-05-02 selects the META asset; FB on 2026-10-01 selects nothing."""
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    udb = write_universe_db(
        tmp_path / "universe.db",
        [("FB", "2022-01-03", "2022-06-08"), ("META", "2022-06-09", None)],
    )
    conn = sqlite3.connect(udb)
    conn.execute("UPDATE universe_membership SET cik = '0001326801'")  # FB is META's line
    conn.commit()
    conn.close()
    settings = Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=udb,
        llm_api_key="k",
        llm_model="deepseek-chat",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )

    report = pipeline.run(
        settings,
        RunParams(
            forms=["10-K"],
            since_year=2022,
            until_year=2022,
            tickers=["FB"],
            analysis_date="2022-05-02",
        ),
    )
    assert report.planned == 1  # the member is FB; the asset it belongs to is META
    conn = sqlite3.connect(settings.db_path)
    assert [r[0] for r in conn.execute("SELECT ticker FROM assets")] == ["META"]
    params = json.loads(
        conn.execute("SELECT params_json FROM analysis_run ORDER BY id DESC LIMIT 1").fetchone()[0]
    )
    assert params["tickers"] == ["META"]  # recorded under the ticker it processed
    conn.close()

    # FB on a 2026 date is ProShares Trust, not this line: it selects nothing
    with pytest.raises(RuntimeError, match="resolved to an asset row"):
        pipeline.run(
            settings,
            RunParams(
                forms=["10-K"],
                since_year=2026,
                until_year=2026,
                tickers=["FB"],
                analysis_date="2026-10-01",
            ),
        )


def _leaver_settings(tmp_path: Path, valid_to: str) -> Settings:
    udb = write_universe_db(tmp_path / "universe.db", [("AAPL", "2020-01-01", valid_to)])
    return Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=udb,
        llm_api_key="k",
        llm_model="deepseek-chat",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )


@pytest.mark.usefixtures("_stubbed")
def test_the_window_ingests_a_leaver_to_its_exit(tmp_path: Path) -> None:
    """T-153 part 3a: the run creates the leaver's asset and stores the filing filed before its
    exit (the fake gateway dates every filing ``{year}-02-01``)."""
    settings = _leaver_settings(tmp_path, "2022-06-15")
    report = pipeline.run(
        settings,
        RunParams(forms=["10-K"], since_year=2022, until_year=2022, analysis_date="2026-10-14"),
    )
    assert report.completed == 1
    conn = sqlite3.connect(settings.db_path)
    assert [r[0] for r in conn.execute("SELECT ticker FROM assets")] == ["AAPL"]
    rows = list(conn.execute("SELECT accession_number FROM sec_filings"))
    assert len(rows) == 1 and rows[0][0].endswith("-2022")
    conn.close()


@pytest.mark.usefixtures("_stubbed")
def test_a_filing_after_the_exit_is_not_stored(tmp_path: Path) -> None:
    """T-153 part 3a: a filing filed after the company's last membership day is skipped."""
    settings = _leaver_settings(tmp_path, "2022-01-15")  # before the fake 2022-02-01 filing
    report = pipeline.run(
        settings,
        RunParams(forms=["10-K"], since_year=2022, until_year=2022, analysis_date="2026-10-14"),
    )
    assert report.completed == 0
    conn = sqlite3.connect(settings.db_path)
    assert list(conn.execute("SELECT COUNT(*) FROM sec_filings")) == [(0,)]
    conn.close()


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


def test_a_reconciliation_failure_from_edgar_fails_the_unit_without_writing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #98 review, item 1: `EdgarClient.financials` now raises `EdgarError` when the
    gateway's own reconciliation failed for a statement (T-042,
    `data["reconciliation_errors"]`) -- confirms the existing per-filing failure path
    (`_run_filing`) does the right thing once it does: the unit counts as failed, not
    scored, and no facts land in `financial_facts` for it, exactly like any other
    `EdgarError` from this client. It is also retried, not skipped -- a rerun with
    `--fresh` off would pick it back up, since it is never added to `engine.completed`."""

    class _FailingEdgar(_FakeEdgar):
        def financials(
            self, ticker: str, form: str, year: int, accession_number: str | None = None
        ) -> dict:
            raise EdgarError(f"/financials/{ticker} reconciliation failed: balance_sheet: boom")

    monkeypatch.setattr(pipeline, "EdgarClient", _FailingEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)

    settings = _settings(tmp_path)
    report = pipeline.run(
        settings, RunParams(forms=["10-K"], since_year=2023, until_year=2023, tickers=["AAPL"])
    )

    assert report.completed == 0
    assert report.failed == 1
    assert "reconciliation failed" in report.errors[0]

    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM analysis_run_error WHERE stage = 'process'").fetchone()[
            0
        ]
        == 1
    )


def test_run_labels_a_fallback_score_and_records_it_on_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-113: a rule-based fallback score must never be stored under the LLM's own model_id
    (1 of production's 377 FUNDAMENTAL scores was, before this fix) -- and every score,
    fallback or not, carries a prompt_hash; the run's own fallback_units counter tracks how
    many of its scores fell back."""
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _FallbackStubAnalyst)

    settings = _settings(tmp_path)
    report = pipeline.run(settings, RunParams(forms=["10-K"], since_year=2023, until_year=2023))
    assert report.completed == 2

    conn = sqlite3.connect(settings.db_path)
    rows = conn.execute(
        "SELECT model, prompt_hash FROM score_snapshot WHERE score_type = 'FUNDAMENTAL'"
    ).fetchall()
    assert len(rows) == 2
    assert all(model == FALLBACK_MODEL_LABEL for model, _ in rows)
    assert all(prompt_hash == "fallback-hash" for _, prompt_hash in rows)

    fallback_units = conn.execute(
        "SELECT fallback_units FROM analysis_run WHERE id = ?", (report.run_id,)
    ).fetchone()[0]
    assert fallback_units == 2


def test_run_refuses_a_dirty_code_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    monkeypatch.setattr(pipeline, "code_version", lambda: "deadbee-dirty")

    settings = _settings(tmp_path)
    with pytest.raises(DirtyTree, match="deadbee-dirty"):
        pipeline.run(settings, RunParams(forms=["10-K"], since_year=2023, until_year=2023))

    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == 0


def test_allow_dirty_overrides_the_guard_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)
    monkeypatch.setattr(pipeline, "code_version", lambda: "deadbee-dirty")

    settings = _settings(tmp_path).model_copy(update={"allow_dirty": True})
    report = pipeline.run(settings, RunParams(forms=["10-K"], since_year=2023, until_year=2023))

    assert report.dirty_tree_bypassed is not None
    assert "deadbee-dirty" in report.dirty_tree_bypassed
    conn = sqlite3.connect(settings.db_path)
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM analysis_run WHERE id = ?", (report.run_id,)
        ).fetchone()[0]
    )
    assert params["dirty_tree_bypassed"] == report.dirty_tree_bypassed


def test_allow_dirty_flag_exists_on_run() -> None:
    parser = fundamental_cli.build_parser()
    assert parser.parse_args(["run"]).allow_dirty is False
    assert parser.parse_args(["run", "--allow-dirty"]).allow_dirty is True


@pytest.mark.usefixtures("_stubbed")
def test_an_interrupted_run_is_marked_failed_not_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_drive", _interrupt)
    settings = _settings(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(settings, RunParams(forms=["10-K"], since_year=2023, until_year=2023))
    conn = sqlite3.connect(settings.db_path)
    status, finished = conn.execute(
        "SELECT status, finished_at FROM analysis_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert status == "failed" and finished is not None


def test_10k_market_cap_resolves_from_prior_10q_cover_count_in_fresh_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: in a fresh build, 10-Q filed before 10-K must be ingested first so that 10-K
    resolves its market cap from the 10-Q's cover count within 200 days."""
    raw_k = (
        (FIXTURES / "financials_AAPL_10-K_2023.json")
        .read_text()
        .replace("2023-09-30", "2023-12-31")
    )
    k_data = json.loads(raw_k)["data"]
    k_data["cover"] = {
        "shares_outstanding": [
            {"value": 110_000_000.0, "as_of_date": "2024-02-10", "class_member": ""}
        ]
    }

    raw_q = (
        (FIXTURES / "financials_MSFT_10-Q_2024.json")
        .read_text()
        .replace("2024-09-30 (Q1)", "2023-09-30 (Q3)")
        .replace("2024-09-30", "2023-09-30")
        .replace("2024-06-30", "2022-12-31")
    )
    q_data = json.loads(raw_q)["data"]
    q_data["cover"] = {
        "shares_outstanding": [
            {"value": 100_000_000.0, "as_of_date": "2023-10-25", "class_member": ""}
        ]
    }

    class _ChronologicalEdgar(_FakeEdgar):
        def filing_by_year(self, ticker: str, form: str, year: int) -> list[FilingRef]:
            if form == "10-Q":
                return [
                    FilingRef(
                        "10-Q", "2023-11-05", f"acc-{ticker}-10q", period_of_report="2023-09-30"
                    )
                ]
            return [
                FilingRef("10-K", "2024-02-15", f"acc-{ticker}-10k", period_of_report="2023-12-31")
            ]

        def financials(
            self, ticker: str, form: str, year: int, accession_number: str | None = None
        ) -> dict[str, Any]:
            return cast("dict[str, Any]", q_data if form == "10-Q" else k_data)

    monkeypatch.setattr(pipeline, "EdgarClient", _ChronologicalEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _StubAnalyst)

    settings = _settings(tmp_path)
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS price_daily ("
        "id INTEGER PRIMARY KEY, asset_id INTEGER, date TEXT, close REAL, source TEXT, "
        "UNIQUE(asset_id, date))"
    )
    conn.execute("INSERT INTO price_daily (asset_id, date, close) VALUES (1, '2023-12-31', 25.0)")
    conn.commit()
    conn.close()

    report = pipeline.run(
        settings,
        RunParams(
            forms=["10-K", "10-Q"],
            since_year=2023,
            until_year=2024,
            tickers=["AAPL"],
            analysis_date="2024-06-30",
        ),
    )
    assert report.completed == 2

    conn = sqlite3.connect(settings.db_path)
    k_filing_id = conn.execute(
        "SELECT id FROM sec_filings WHERE form = '10-K' AND fiscal_period = 'FY2023'"
    ).fetchone()[0]
    m_row = conn.execute(
        "SELECT value FROM fundamental_metrics WHERE filing_id = ? AND metric_name = 'market_capitalization'",
        (k_filing_id,),
    ).fetchone()
    assert m_row is not None
    assert m_row[0] == pytest.approx(2_500_000_000.0)


# -- P3 round 1 (reviewer A): items B and K -----------------------------------------------------


class _FlakyEdgar(_Edgar):
    """Fails the first *failures* calls of an accession, then serves it."""

    def __init__(self, *args: Any, failures: dict[str, int], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = dict(failures)

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict[str, Any]:
        left = self.failures.get(accession_number or "", 0)
        if left:
            self.failures[accession_number or ""] = left - 1
            self.financials_calls.append(accession_number)
            raise EdgarError("/financials/1 failed after 3 attempts")
        return cast("dict[str, Any]", super().financials(ticker, form, year, accession_number))


class _TtmStubAnalyst(_StubAnalyst):
    """Records, for every filing it analyses, the trailing-twelve-month flows it was given and
    the filings already in the database."""

    db_path: ClassVar[str] = ""
    seen: ClassVar[list[tuple[str, dict[str, Any], list[str]]]] = []

    def analyze(self, ctx: FilingContext) -> AnalysisResult:
        conn = sqlite3.connect(_TtmStubAnalyst.db_path)
        stored = [str(row[0]) for row in conn.execute("SELECT fiscal_period FROM sec_filings")]
        conn.close()
        _TtmStubAnalyst.seen.append((ctx.period_key, dict(ctx.ttm_flows or {}), stored))
        return super().analyze(ctx)


def _run_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    edgar: Any,
    analyst: Any,
) -> pipeline.RunReport:
    monkeypatch.setattr(pipeline, "EdgarClient", edgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: None)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", analyst)
    params = RunParams(forms=["10-Q"], since_year=2023, until_year=2023, analysis_date="2026-09-21")
    return pipeline.run(_stz_settings(tmp_path), params)


def test_a_service_failure_is_retried_in_place_before_the_next_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item B, after the review: a filing that fails for a **service** reason is retried right
    away, before the company's next filing is analysed, so the next filing's TTM reads the
    recovered one.

    The append-only key cannot rewrite a filing's rows, so the later filings cannot be recomputed:
    the recovery has to happen where it still has an effect, in place.
    """
    monkeypatch.setattr(pipeline, "RETRY_WAITS_SECONDS", (0.0, 0.0, 0.0))
    edgar = _FlakyEdgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
        failures={Q1_JUN.accession_number: 2},
    )
    _TtmStubAnalyst.db_path = str(tmp_path / "kg.db")
    _TtmStubAnalyst.seen = []
    _run_with(monkeypatch, tmp_path, edgar, _TtmStubAnalyst)
    # Q1 fails twice, serves on the third try, and only then is Q2 fetched
    assert edgar.financials_calls == [
        Q1_JUN.accession_number,
        Q1_JUN.accession_number,
        Q1_JUN.accession_number,
        Q2_OCT.accession_number,
    ]
    conn = sqlite3.connect(tmp_path / "kg.db")
    stored = [
        row[0] for row in conn.execute("SELECT fiscal_period FROM sec_filings ORDER BY period_end")
    ]
    conn.close()
    assert stored == ["2023Q1", "2023Q2"]
    # the recovered filing was in the database when the next filing was analysed: the next
    # filing's context (its TTM, its growth, its rule inputs) reads it
    assert _TtmStubAnalyst.seen[0][2] == ["2023Q1"]  # Q1: only its own row
    assert "2023Q1" in _TtmStubAnalyst.seen[1][2]  # Q2: the recovered Q1 is already stored
    assert _TtmStubAnalyst.seen[1][1]  # Q2's trailing-twelve-month flows were computed


def test_a_data_failure_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A filing that fails for a **data** reason fails the same way on every run: it is recorded
    at once, and the run goes on."""
    monkeypatch.setattr(pipeline, "RETRY_WAITS_SECONDS", (0.0, 0.0, 0.0))
    edgar = _Edgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
        raises={
            Q1_JUN.accession_number: EdgarError(
                "unsuccessful payload: {'success': False, 'error': \"Failed to extract "
                "financials: 'NoneType' object has no attribute 'to_dataframe'\"}"
            )
        },
    )
    _run(monkeypatch, tmp_path, edgar)
    assert edgar.financials_calls == [Q1_JUN.accession_number, Q2_OCT.accession_number]


def test_a_code_error_is_recorded_without_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the **gateway's** failures are retried: a code error is ours, not the gateway's, and
    is recorded at once (PR #178, review round 2)."""
    edgar = _Edgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
        raises={Q1_JUN.accession_number: KeyError("a code error")},
    )
    report = _run(monkeypatch, tmp_path, edgar)
    assert edgar.financials_calls == [Q1_JUN.accession_number, Q2_OCT.accession_number]
    assert report.failed == 1  # recorded at once, and the run went on to the next filing


def test_a_filing_already_scored_is_not_fetched_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item B, the other side: a run with nothing to recover leaves the scored filings alone."""
    edgar = _Edgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: _q1_payload(), Q2_OCT.accession_number: _stz_q2_payload()},
    )
    _run(monkeypatch, tmp_path, edgar)
    edgar.financials_calls.clear()
    _run(monkeypatch, tmp_path, edgar)
    assert edgar.financials_calls == []


def _drop_correction(accession: str) -> dict[str, Any]:
    return {
        "statement": "income_statement",
        "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
        "column": "2023-05-31 (Q1)",
        "original": 4003533000.0,
        "corrected": None,
        "rule": "T-042",
        "reason": "no_filed_nondimensional_fact",
    }


def test_every_gateway_correction_that_nulled_a_value_is_logged_keyed_by_accession(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item K: the run logs every gateway correction whose ``corrected`` is null -- the rows the
    gateway dropped -- with the statement, the concept, the column, the original value and the
    reason, keyed by the accession. A correction the gateway *changed* is not a drop."""
    log = tmp_path / "out" / "gateway_drops.jsonl"
    monkeypatch.setenv(pipeline.DROP_LOG_ENV, str(log))
    payload = _q1_payload()
    payload["corrections"] = [
        _drop_correction(Q1_JUN.accession_number),
        {
            "statement": "income_statement",
            "concept": "us-gaap_CostOfRevenue",
            "column": "2023-05-31 (Q1)",
            "original": 1.0,
            "corrected": 2.0,
            "rule": "T-041",
            "reason": "reconciled",
        },
    ]
    assert pipeline.log_gateway_drops(payload, Q1_JUN.accession_number) == 1
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "accession": Q1_JUN.accession_number,
            "statement": "income_statement",
            "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "column": "2023-05-31 (Q1)",
            "original": 4003533000.0,
            "rule": "T-042",
            "reason": "no_filed_nondimensional_fact",
        }
    ]


def test_the_run_writes_the_drop_log_of_every_payload_it_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item K, end to end: the log is written during the run, not by a separate pass."""
    log = tmp_path / "out" / "gateway_drops.jsonl"
    monkeypatch.setenv(pipeline.DROP_LOG_ENV, str(log))
    payload = _q1_payload()
    payload["corrections"] = [_drop_correction(Q1_JUN.accession_number)]
    edgar = _Edgar(
        {("10-Q", 2023): _LISTED},
        {Q1_JUN.accession_number: payload, Q2_OCT.accession_number: _stz_q2_payload()},
    )
    _run(monkeypatch, tmp_path, edgar)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["accession"] for row in rows] == [Q1_JUN.accession_number]


def test_an_empty_payload_is_named_as_an_empty_payload_not_as_a_missing_quarter() -> None:
    """Item A: the gateway served ``success: true`` with no statements at all (AEP
    0000004904-23-000072). The reason says so, so the classifier can call it a service failure
    instead of a data failure about the filing's period."""
    stmts = Statements.from_payload(
        {"income_statement": [], "balance_sheet": [], "cash_flow": []},
    )
    assert stmts.is_empty
    assert pipeline._resolve_target(stmts, _task("AAPL", "10-Q", 2024)) == (
        "the gateway served an empty payload"
    )
