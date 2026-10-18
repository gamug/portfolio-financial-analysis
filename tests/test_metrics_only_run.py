"""T-143 part A, P1: ``fundamental_agent run --metrics-only``.

Metrics, facts, data-quality gates and rule inputs are written; no FUNDAMENTAL ``score_snapshot``
row (not even the fallback) and no model is built or called. The same fixture filing gives identical
``fundamental_metrics`` rows under metrics-only and under a normal run whose only mock is the
model/Agent, so the two paths cannot drift.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from conftest import write_universe_db
from test_pipeline import _FakeEdgar

from fundamental_agent import agents, cli, db, pipeline, value_inputs
from fundamental_agent.config import Settings
from fundamental_agent.db import FilingKey, FilingMeta, ensure_schema
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import RunParams
from kg_schema import connect

D = "2024-06-30"


def _settings(root: Path) -> Settings:
    """A settings root with an AAPL universe; ``--metrics-only`` needs no LLM variable."""
    root.mkdir(parents=True, exist_ok=True)
    udb = write_universe_db(root / "universe.db", [("AAPL", "2020-01-01", None)])
    return Settings(
        db_path=root / "kg.db",
        universe_db_path=udb,
        llm_api_key="",
        llm_model="",
        llm_url="",
        edgar_base_url="http://edgar.test",
    )


def _params(metrics_only: bool) -> RunParams:
    return RunParams(
        forms=["10-K"],
        since_year=2023,
        until_year=2023,
        tickers=["AAPL"],
        analysis_date=D,
        metrics_only=metrics_only,
    )


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _count(db_path: Path, table_where: str) -> int:
    row = _rows(db_path, f"SELECT COUNT(*) FROM {table_where}")  # noqa: S608 - test-owned
    return cast("int", row[0][0])


_METRIC_ROWS = (
    "SELECT f.accession_number, m.metric_group, m.metric_name, m.value, m.unit, m.inputs_json "
    "FROM fundamental_metrics m JOIN sec_filings f ON f.id = m.filing_id "
    "ORDER BY f.accession_number, m.metric_group, m.metric_name"
)
_DQI_ROWS = (
    "SELECT f.accession_number, d.metric_group, d.metric_name, d.rule_id, d.severity, "
    "d.quarantined, d.value, d.gate_version FROM data_quality_issue d "
    "JOIN sec_filings f ON f.id = d.filing_id "
    "ORDER BY f.accession_number, d.metric_group, d.metric_name, d.rule_id"
)


def _snapshot_of_the_data(db_path: Path) -> list[tuple[object, ...]]:
    """Every data row a metrics-only re-run must not add (the run log is bookkeeping)."""
    out: list[tuple[object, ...]] = []
    for table in (
        "sec_filings",
        "financial_facts",
        "fundamental_metrics",
        "data_quality_issue",
        "score_snapshot",
        "analysis_run_error",
    ):
        out += _rows(db_path, f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - test-owned
    return out


class _StubAgent:
    """The Strands ``Agent`` without a model (reviewer B, item 3): the brief returns an empty
    string, so ``_synthesize`` falls back -- the real ``FundamentalAnalyst.analyze`` still runs its
    own ``compute_metrics`` path. ``constructions`` proves the model path was reached (no pillar was
    ``empty``, so ``analyze`` did not short-circuit)."""

    constructions = 0

    def __init__(self, **_kwargs: object) -> None:
        type(self).constructions += 1

    def __call__(self, _prompt: str) -> str:
        return ""


class _CountingEdgar(_FakeEdgar):
    """``_FakeEdgar`` that counts the filing-payload fetches (``financials``)."""

    financials_calls = 0

    def financials(
        self, ticker: str, form: str, year: int, accession_number: str | None = None
    ) -> dict:
        type(self).financials_calls += 1
        return super().financials(ticker, form, year, accession_number)


def _boom(*_a: object, **_k: object) -> object:
    raise AssertionError("the model/analyst was built in metrics-only mode")


# -- the mode builds nothing and writes everything but the score --------------------------------


def test_metrics_only_builds_no_model_or_analyst_and_writes_no_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", _boom)
    monkeypatch.setattr(pipeline, "FundamentalAnalyst", _boom)

    report = pipeline.run(_settings(tmp_path), _params(metrics_only=True))

    assert (report.completed, report.skipped, report.failed) == (1, 0, 0)
    assert _count(tmp_path / "kg.db", "fundamental_metrics") > 0
    assert _count(tmp_path / "kg.db", "score_snapshot") == 0
    params = _rows(
        tmp_path / "kg.db",
        "SELECT params_json FROM analysis_run ORDER BY id DESC LIMIT 1",
    )[0][0]
    assert '"metrics_only": true' in str(params)
    assert _count(tmp_path / "kg.db", "analysis_run") == 1
    assert (
        _rows(tmp_path / "kg.db", "SELECT COALESCE(SUM(fallback_units), 0) FROM analysis_run")[0][0]
        == 0
    )


def test_metrics_only_needs_no_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # no .env may supply the variables, and none may be read
    monkeypatch.setattr("fundamental_agent.config.load_dotenv", lambda *_a, **_k: None)
    for var in ("LLM_API_KEY", "LLM_MODEL", "LLM_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KG_FINANCIAL_DB", ":memory:")

    settings = Settings.load(metrics_only=True)
    assert (settings.llm_api_key, settings.llm_model, settings.llm_url) == ("", "", "")
    with pytest.raises(RuntimeError, match="missing required environment variables"):
        Settings.load()


# -- the two paths write identical rows ---------------------------------------------------------


def test_metrics_only_and_a_normal_run_write_identical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal side runs the real ``analyze`` -- only the model and the Agent are mocked -- so a
    change to ``analyze``'s own compute path is caught (reviewer B, item 3)."""
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: object())
    monkeypatch.setattr(agents, "Agent", _StubAgent)

    only = tmp_path / "only"
    normal = tmp_path / "normal"
    _StubAgent.constructions = 0
    pipeline.run(_settings(only), _params(metrics_only=True))
    pipeline.run(_settings(normal), _params(metrics_only=False))
    assert _StubAgent.constructions >= 1  # the normal run's real analyze reached the model path

    assert _rows(only / "kg.db", _METRIC_ROWS) == _rows(normal / "kg.db", _METRIC_ROWS)
    assert _rows(only / "kg.db", _DQI_ROWS) == _rows(normal / "kg.db", _DQI_ROWS)
    # the metrics-only run stored rule inputs (value_inputs, utility_coverage) too
    assert _count(only / "kg.db", "fundamental_metrics WHERE metric_group = 'rule_input'") > 0
    assert _count(only / "kg.db", "score_snapshot WHERE score_type = 'FUNDAMENTAL'") == 0
    assert _count(normal / "kg.db", "score_snapshot WHERE score_type = 'FUNDAMENTAL'") == 1


def test_a_normal_run_after_a_metrics_only_run_scores_and_keeps_the_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer A, 2026-10-18: the skip applies only to a metrics-only run; a normal run on the same
    database still treats the filing as pending and scores it, leaving the metric rows identical."""
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr(pipeline, "build_model", lambda _s: object())
    monkeypatch.setattr(agents, "Agent", _StubAgent)

    settings = _settings(tmp_path)
    pipeline.run(settings, _params(metrics_only=True))
    before = _rows(settings.db_path, _METRIC_ROWS)
    assert _count(settings.db_path, "score_snapshot WHERE score_type = 'FUNDAMENTAL'") == 0

    report = pipeline.run(settings, _params(metrics_only=False))

    assert (report.completed, report.failed) == (1, 0)
    assert _count(settings.db_path, "score_snapshot WHERE score_type = 'FUNDAMENTAL'") == 1
    assert _rows(settings.db_path, _METRIC_ROWS) == before


# -- resume and idempotency ---------------------------------------------------------------------


def test_a_second_metrics_only_run_fetches_no_filing_and_writes_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _CountingEdgar)
    settings = _settings(tmp_path)
    pipeline.run(settings, _params(metrics_only=True))

    before = _snapshot_of_the_data(settings.db_path)
    _CountingEdgar.financials_calls = 0
    report = pipeline.run(settings, _params(metrics_only=True))

    assert report.completed == 0
    assert _CountingEdgar.financials_calls == 0  # the done-set skips the filing before its fetch
    assert _snapshot_of_the_data(settings.db_path) == before


def test_a_filing_killed_before_its_value_inputs_row_is_not_done(tmp_path: Path) -> None:
    """Reviewer B, item 2: done = a metrics-only run wrote the filing AND its ``value_inputs`` row
    exists. A filing stopped between ``upsert_filing`` and ``value_inputs.write`` is not done, and a
    filing written by a normal run is never in the set."""
    conn = connect(tmp_path / "d.db")
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO assets (ticker, company_name, cik) VALUES ('AAA', 'AAA Inc.', '1')"
        )
        asset_id = int(conn.execute("SELECT id FROM assets").fetchone()["id"])
        metrics_run = db.start_run(conn, params={"metrics_only": True}, as_of=D)
        normal_run = db.start_run(conn, params={"metrics_only": False}, as_of=D)
        stopped = db.upsert_filing(
            conn,
            FilingKey(asset_id, "10-K", 2023, "FY2023"),
            FilingMeta(
                filing_date="2023-11-01", accession_number="acc-stopped", period_end="2023-09-30"
            ),
            run_id=metrics_run,
        )
        finished = db.upsert_filing(
            conn,
            FilingKey(asset_id, "10-Q", 2024, "2024Q1"),
            FilingMeta(
                filing_date="2024-05-01", accession_number="acc-finished", period_end="2024-03-31"
            ),
            run_id=metrics_run,
        )
        normal = db.upsert_filing(
            conn,
            FilingKey(asset_id, "10-Q", 2024, "2024Q2"),
            FilingMeta(
                filing_date="2024-08-01", accession_number="acc-normal", period_end="2024-06-30"
            ),
            run_id=normal_run,
        )
        # only the finished filing reached value_inputs.write
        db.record_metrics(
            conn,
            finished,
            [(value_inputs.GROUP, MetricResult(value_inputs.NAME, 1.0, "usd", {}))],
            run_id=metrics_run,
        )
        # a normal run's filing keeps its value_inputs row but is never metrics-only
        db.record_metrics(
            conn,
            normal,
            [(value_inputs.GROUP, MetricResult(value_inputs.NAME, 1.0, "usd", {}))],
            run_id=normal_run,
        )

        assert stopped and normal  # both are stored
        assert db.metrics_only_accessions(conn) == {"acc-finished"}
    finally:
        conn.close()


# -- the CLI flag -------------------------------------------------------------------------------


def test_the_cli_metrics_only_flag_runs_without_an_llm_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "EdgarClient", _FakeEdgar)
    monkeypatch.setattr("fundamental_agent.config.load_dotenv", lambda *_a, **_k: None)
    for var in ("LLM_API_KEY", "LLM_MODEL", "LLM_URL"):
        monkeypatch.delenv(var, raising=False)
    udb = write_universe_db(tmp_path / "universe.db", [("AAPL", "2020-01-01", None)])
    monkeypatch.setenv("KG_FINANCIAL_DB", str(tmp_path / "kg.db"))
    monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))

    code = cli.main(
        [
            "run",
            "--metrics-only",
            "--db",
            str(tmp_path / "kg.db"),
            "--universe-db",
            str(udb),
            "--forms",
            "10-K",
            "--since-year",
            "2023",
            "--until-year",
            "2023",
            "--tickers",
            "AAPL",
            "--analysis-date",
            D,
        ]
    )

    assert code == 0
    assert _count(tmp_path / "kg.db", "score_snapshot") == 0
    assert _count(tmp_path / "kg.db", "fundamental_metrics") > 0
