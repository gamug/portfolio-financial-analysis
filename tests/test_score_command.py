"""WP-F: ``python -m fundamental_agent score`` -- the FUNDAMENTAL rows from the stored metrics.

The command exists because the backtest database was built with ``run --metrics-only`` (facts,
metrics, gates and rule inputs stored; no FUNDAMENTAL ``score_snapshot`` row) and a full ``run``
would re-fetch every filing from the EDGAR gateway. These tests pin:

* the deterministic (``--rule-based``) row, including its score and its ``rule-based-fallback-v1``
  label, and that it is the row a run's own fallback writes for the same filing;
* idempotency and resume, the per-filing failure record, the ``--for-dates`` point-in-time
  selection, ``--workers > 1`` writing the same rows, and the LLM path with a monkeypatched model;
* that the reads are anchored at the filing's own period end (no lookahead).

No test calls a model: ``agents.Agent`` is always replaced.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

import pytest
from conftest import cik_for, write_universe_db
from test_pipeline_multi_filing import _two_quarters

from fundamental_agent import agents, cli, db, pipeline, scoring
from fundamental_agent.agents import (
    FALLBACK_MODEL_LABEL,
    UNSCORED_MODEL_LABEL,
    FundamentalAnalyst,
    _prompt_version_hash,
)
from fundamental_agent.config import Settings
from fundamental_agent.metrics.base import MetricResult
from fundamental_agent.pipeline import RunParams
from fundamental_agent.scoring import ScoreParams, run_score
from kg_schema import connect
from kg_schema.trading_calendar import available_from
from kg_schema.versions import VersionError

#: One metric with a value per pillar, so every pillar is `present` and the fallback scores:
#: net_margin 0.20 >= 0.15 (+12 profitability), debt_to_equity 4.0 > 3.0 (-12 financial_strength),
#: free_cash_flow_margin 0.12 >= 0.10 (+12 cash_flow), revenue_growth 0.15 >= 0.10 (+8 growth).
#: With the declared equal weights (0.25 each) the total is (62 + 38 + 62 + 58) / 4 = 55.0.
#: The groups are the run's own (`CORE_GROUPS`): `debt_to_equity` is read from `leverage` and
#: `free_cash_flow_margin` from `cashflow` -- the fallback's flat keys are "group.metric".
_METRICS: tuple[tuple[str, str, float, str], ...] = (
    ("profitability", "net_margin", 0.20, "ratio"),
    ("leverage", "debt_to_equity", 4.0, "x"),
    ("cashflow", "free_cash_flow_margin", 0.12, "ratio"),
    ("growth", "revenue_growth", 0.15, "ratio"),
)
_FALLBACK_SCORE = 55.0


class _DeadAgent:
    """A model that cannot answer: every call raises (the run must fall back, never call out)."""

    calls: ClassVar[list[str]] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    def __call__(self, prompt: str) -> str:
        type(self).calls.append(prompt)
        raise RuntimeError("no model is configured in this test")


def _fake_agent(calls: list[str], ratings: int = 70) -> type:
    """A Strands-agent stand-in: JSON for the synthesis prompt, a reading for a specialist."""

    class _Agent:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __call__(self, prompt: str) -> str:
            calls.append(prompt)
            if "Reply with ONLY a raw JSON object" not in prompt:
                return "a specialist reading"
            named = prompt.split("Rate exactly those", maxsplit=1)[0]
            pillars = re.findall(r'"([a-z_]+)"', named)
            return json.dumps(
                {
                    "pillars": {
                        p: {"rating": ratings, "reason": f"reason for {p}"} for p in pillars
                    },
                    "narrative": "narrative",
                    "strengths": ["strength"],
                    "risks": ["risk"],
                }
            )

    return _Agent


def _metrics(filing_id: int, conn: Any, *, engine_version: str = db.METRICS_ENGINE_VERSION) -> None:
    db.record_metrics(
        conn,
        filing_id,
        [(g, MetricResult(n, v, u, {"provenance": {}})) for g, n, v, u in _METRICS],
        engine_version=engine_version,
        event_time="2023-09-30",
    )


def _filing(conn: Any, label: str, period_end: str, filed: str, *, accession: str) -> int:
    """A dated 10-K with its accession (the identity a ``--accessions`` run names)."""
    return db.upsert_filing(
        conn,
        db.FilingKey(1, "10-K", int(period_end[:4]), label),
        db.FilingMeta(
            filing_date=filed,
            accession_number=accession,
            period_end=period_end,
            cik=cik_for("AAPL"),
        ),
    )


def _settings(folder: Path, universe: Path) -> Settings:
    return Settings(
        db_path=folder / "kg.db",
        universe_db_path=universe,
        llm_api_key="k",
        llm_model="deepseek-chat",
        llm_url="http://llm.test",
        edgar_base_url="http://edgar.test",
    )


def _seed_db(
    tmp_path: Path,
    *,
    name: str = "db",
    filings: int = 1,
    with_metrics: bool = True,
    engine_version: str = db.METRICS_ENGINE_VERSION,
) -> Settings:
    """A database with one asset and *filings* dated 10-Ks, each with its metrics.

    Filing 1 ends 2023-09-30 and is filed 2023-11-03 (available 2023-11-06); filing 2 ends
    2024-09-30 and is filed 2024-11-04.
    """
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    conn = connect(folder / "kg.db")
    try:
        db.ensure_schema(conn)
        conn.execute(
            "INSERT INTO assets (id, ticker, company_name, cik) VALUES (1, 'AAPL', 'Apple Inc.', ?)",
            (cik_for("AAPL"),),
        )
        conn.commit()
        periods = [
            ("FY2023", "2023-09-30", "2023-11-03", "0000320193-23-000106"),
            ("FY2024", "2024-09-30", "2024-11-04", "0000320193-24-000123"),
        ]
        for label, period_end, filed, accession in periods[:filings]:
            filing_id = _filing(conn, label, period_end, filed, accession=accession)
            if with_metrics:
                _metrics(filing_id, conn, engine_version=engine_version)
    finally:
        conn.close()
    universe = write_universe_db(folder / "universe.db", [("AAPL", "2020-01-01", None)])
    return _settings(folder, universe)


def _rows(settings: Settings) -> list[dict[str, Any]]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM score_snapshot WHERE score_type = 'FUNDAMENTAL' "
                "ORDER BY asset_id, event_time"
            ).fetchall()
        ]
    finally:
        conn.close()


def _count_scores(settings: Settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM score_snapshot").fetchone()[0])
    finally:
        conn.close()


def _count_runs(settings: Settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0])
    finally:
        conn.close()


def _comparable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows without the columns two runs of the same command necessarily differ in."""
    drop = {"id", "computed_at", "run_id"}
    return [{k: v for k, v in row.items() if k not in drop} for row in rows]


# -- the deterministic row -------------------------------------------------------------------


def test_rule_based_score_writes_the_documented_fallback_row(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path)
    report = run_score(settings, ScoreParams(rule_based=True))

    assert (report.scored, report.skipped, report.failed) == (1, 0, 0)
    assert report.model == FALLBACK_MODEL_LABEL
    assert report.unscored == 0
    rows = _rows(settings)
    assert len(rows) == 1
    row = rows[0]
    assert row["raw_value"] == _FALLBACK_SCORE
    assert row["normalized_score"] == _FALLBACK_SCORE
    assert row["rating"] == "neutral"
    assert row["model"] == FALLBACK_MODEL_LABEL
    assert row["event_time"] == "2023-09-30"
    assert row["available_at"] == available_from("2023-11-03")
    assert row["run_kind"] == "analysis"
    assert row["run_id"] is not None
    assert row["prompt_hash"] == _prompt_version_hash(FALLBACK_MODEL_LABEL)
    inputs = json.loads(row["inputs_json"])
    for group, name, value, _unit in _METRICS:
        assert inputs[f"{group}.{name}"] == value
    # the pillar block is the run's own (D4), with the fallback's nudge ratings
    assert inputs["pillars"]["profitability"]["rating"] == 62.0
    assert inputs["pillars"]["financial_strength"]["rating"] == 38.0
    assert inputs["coverage"]["scored"] == [
        "profitability",
        "financial_strength",
        "cash_flow",
        "growth",
    ]
    # the run log carries the tallies and the command's params
    conn = sqlite3.connect(settings.db_path)
    try:
        run = conn.execute(
            "SELECT status, completed_units, planned_units, params_json FROM analysis_run"
        ).fetchone()
    finally:
        conn.close()
    assert run[0] == "completed"
    assert (run[1], run[2]) == (1, 1)
    params = json.loads(run[3])
    assert params["command"] == "score"
    assert params["rule_based"] is True


def test_a_filing_whose_pillar_is_empty_is_written_unscored(tmp_path: Path) -> None:
    """D1's coverage rule, unchanged from `run`: the row exists (so a resume skips it) but carries
    no score and the `unscored-pillar-coverage-v1` label -- never a neutral 50."""
    settings = _seed_db(tmp_path)
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM fundamental_metrics WHERE metric_group = 'growth'")
        conn.commit()
    finally:
        conn.close()
    report = run_score(settings, ScoreParams(rule_based=True))
    assert (report.scored, report.unscored) == (1, 1)
    row = _rows(settings)[0]
    assert row["raw_value"] is None
    assert row["model"] == UNSCORED_MODEL_LABEL


# -- the same row a run's fallback writes ----------------------------------------------------


def test_the_rule_based_row_is_the_row_a_runs_fallback_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run` with a model that cannot answer, and `score --rule-based` on a metrics-only database,
    write the same FUNDAMENTAL row from the same stored metrics (the real STZ payloads)."""
    universe = write_universe_db(tmp_path / "universe.db", [("STZ", "2020-01-01", None)])
    monkeypatch.setattr(pipeline, "EdgarClient", _two_quarters())
    monkeypatch.setattr(pipeline, "build_model", lambda _settings: None)
    monkeypatch.setattr(
        pipeline,
        "FundamentalAnalyst",
        lambda _model, _name: FundamentalAnalyst(None, FALLBACK_MODEL_LABEL),
    )
    monkeypatch.setattr(agents, "Agent", _DeadAgent)
    params = RunParams(forms=["10-Q"], since_year=2023, until_year=2023)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scored = pipeline.run(_settings(run_dir, universe), params)
    assert scored.completed == 2

    metrics_dir = tmp_path / "metrics_only"
    metrics_dir.mkdir()
    metrics_only = _settings(metrics_dir, universe)
    pipeline.run(metrics_only, replace(params, metrics_only=True))
    assert _rows(metrics_only) == []

    report = run_score(metrics_only, ScoreParams(rule_based=True))
    assert (report.scored, report.failed) == (2, 0)

    assert _comparable(_rows(_settings(run_dir, universe))) == _comparable(_rows(metrics_only))


# -- idempotency, resume, failures ----------------------------------------------------------


def test_a_second_run_skips_what_is_already_scored(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path, filings=2)
    first = run_score(settings, ScoreParams(rule_based=True))
    assert (first.scored, first.skipped) == (2, 0)
    second = run_score(settings, ScoreParams(rule_based=True))
    assert (second.scored, second.skipped, second.failed) == (0, 2, 0)
    assert len(_rows(settings)) == 2
    assert any("already has a FUNDAMENTAL row" in line for line in second.errors)


def test_limit_counts_only_the_filings_still_to_score(tmp_path: Path) -> None:
    """A killed run resumes: the first `--limit 1` scores one filing, the next run scores the
    other one instead of re-attempting the finished one."""
    settings = _seed_db(tmp_path, filings=2)
    first = run_score(settings, ScoreParams(rule_based=True, limit=1))
    assert (first.scored, first.skipped, first.planned) == (1, 0, 1)
    second = run_score(settings, ScoreParams(rule_based=True, limit=1))
    assert (second.scored, second.skipped, second.planned) == (1, 1, 1)
    assert [row["event_time"] for row in _rows(settings)] == ["2023-09-30", "2024-09-30"]


def test_an_unscorable_filing_is_recorded_and_never_dropped(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path)
    conn = connect(settings.db_path)
    try:
        _filing(
            conn,
            "FY2022",
            "2022-09-30",
            "2022-11-04",
            accession="0000320193-22-000108",
        )
    finally:
        conn.close()
    report = run_score(
        settings,
        ScoreParams(
            rule_based=True,
            accessions=("0000320193-22-000108", "0000000000-00-000000"),
        ),
    )
    assert (report.scored, report.failed) == (0, 2)
    assert any("no filing carries accession" in line for line in report.errors)
    assert any("no stored metrics under the resolved versions" in line for line in report.errors)
    raw = sqlite3.connect(settings.db_path)
    try:
        errors = raw.execute(
            "SELECT ticker, form, fiscal_period, stage, message FROM analysis_run_error ORDER BY id"
        ).fetchall()
        run = raw.execute("SELECT status, failed_units FROM analysis_run").fetchone()
    finally:
        raw.close()
    assert len(errors) == 2
    assert all(error[3] == "score" for error in errors)
    assert [error[0] for error in errors] == ["", "AAPL"]
    assert run == ("completed", 2)


def test_a_database_built_by_another_engine_version_is_scored(tmp_path: Path) -> None:
    """The versions are resolved against what the database holds (T-090), so a copy built by an
    older engine -- the real-data case -- is scored, not silently skipped for naming the current
    version."""
    settings = _seed_db(tmp_path, engine_version="metrics-v2")
    report = run_score(settings, ScoreParams(rule_based=True))
    assert (report.scored, report.failed) == (1, 0)
    assert _rows(settings)[0]["raw_value"] == _FALLBACK_SCORE
    conn = sqlite3.connect(settings.db_path)
    try:
        params = json.loads(conn.execute("SELECT params_json FROM analysis_run").fetchone()[0])
    finally:
        conn.close()
    assert params["metrics_version"] is None  # resolved, not pinned
    assert set(params["metrics_versions"].values()) == {"metrics-v2"}


def test_pinning_a_version_the_database_does_not_hold_is_refused(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path)
    with pytest.raises(VersionError, match="is not stored"):
        run_score(settings, ScoreParams(rule_based=True, metrics_version="metrics-v2"))
    assert _rows(settings) == []
    assert _count_runs(settings) == 0


# -- selectors ------------------------------------------------------------------------------


def test_for_dates_takes_each_members_filing_available_on_that_date(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path, filings=2)
    # 2023-06-01: no member has a filing available yet (the FY2023 10-K is usable from 2023-11-06)
    none_yet = run_score(settings, ScoreParams(rule_based=True, for_dates=("2023-06-01",)))
    assert (none_yet.scored, none_yet.planned, none_yet.failed) == (0, 0, 0)
    assert _rows(settings) == []

    # 2023-12-01: FY2023 is usable, FY2024 is not
    first = run_score(settings, ScoreParams(rule_based=True, for_dates=("2023-12-01",)))
    assert (first.scored, first.planned) == (1, 1)
    assert [row["event_time"] for row in _rows(settings)] == ["2023-09-30"]

    # 2024-12-01: both are usable and the newest period end is the one that date reads
    second = run_score(settings, ScoreParams(rule_based=True, for_dates=("2024-12-01",)))
    assert second.scored == 1
    assert [row["event_time"] for row in _rows(settings)] == ["2023-09-30", "2024-09-30"]


def test_two_dates_naming_one_filing_score_it_once(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path, filings=2)
    report = run_score(
        settings, ScoreParams(rule_based=True, for_dates=("2024-01-15", "2024-06-15"))
    )
    # both dates resolve to the FY2023 filing (FY2024 is available 2024-11-05): one, deduplicated
    assert (report.scored, report.planned) == (1, 1)
    assert [row["event_time"] for row in _rows(settings)] == ["2023-09-30"]


def test_accessions_select_exactly_the_named_filings(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path, filings=2)
    report = run_score(settings, ScoreParams(rule_based=True, accessions=("0000320193-24-000123",)))
    assert (report.scored, report.planned) == (1, 1)
    assert [row["event_time"] for row in _rows(settings)] == ["2024-09-30"]


def test_a_bad_for_dates_line_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an ISO date"):
        scoring.parse_dates(["2023-13-01", "2023-06-01"])


# -- dry run --------------------------------------------------------------------------------


def test_dry_run_counts_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _seed_db(tmp_path, filings=2)
    report = run_score(settings, ScoreParams(rule_based=True, dry_run=True))
    assert (report.scored, report.planned, report.failed) == (0, 2, 0)
    assert report.model_calls == 0  # --rule-based makes no model call
    assert report.prompt_chars > 0  # but the prompt the LLM path would send is measured
    assert _count_scores(settings) == 0
    assert _count_runs(settings) == 0
    out = capsys.readouterr().out
    assert len([line for line in out.splitlines() if "prompt~" in line]) == 2


def test_a_dry_run_of_the_llm_path_counts_the_model_calls(tmp_path: Path) -> None:
    settings = _seed_db(tmp_path)
    report = run_score(settings, ScoreParams(dry_run=True))
    assert (report.scored, report.planned) == (0, 1)
    assert report.model_calls > 1  # the orchestrator, the specialists with values, the synthesis
    assert _count_scores(settings) == 0
    assert _count_runs(settings) == 0


# -- the LLM path and its workers -----------------------------------------------------------


def test_the_llm_path_scores_through_the_monkeypatched_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(agents, "Agent", _fake_agent(calls))
    settings = _seed_db(tmp_path)
    report = run_score(settings, ScoreParams())
    assert (report.scored, report.failed) == (1, 0)
    row = _rows(settings)[0]
    assert row["raw_value"] == 70.0
    assert row["model"] == "deepseek-chat"
    assert row["prompt_hash"] == _prompt_version_hash("deepseek-chat")
    assert len(calls) >= 2  # the brief and the synthesis prompt
    assert "AAPL (Apple Inc.) 10-K FY2023" in calls[0]
    conn = sqlite3.connect(settings.db_path)
    try:
        assert conn.execute("SELECT fallback_units FROM analysis_run").fetchone()[0] == 0
    finally:
        conn.close()


def test_workers_write_the_same_rows_as_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(agents, "Agent", _fake_agent(calls))
    settings = _seed_db(tmp_path, filings=2)
    serial = tmp_path / "serial"
    serial.mkdir()
    shutil.copyfile(settings.db_path, serial / "kg.db")
    shutil.copyfile(settings.universe_db_path, serial / "universe.db")
    parallel = tmp_path / "parallel"
    parallel.mkdir()
    shutil.copyfile(settings.db_path, parallel / "kg.db")
    shutil.copyfile(settings.universe_db_path, parallel / "universe.db")

    one = run_score(_settings(serial, serial / "universe.db"), ScoreParams(workers=1))
    many = run_score(_settings(parallel, parallel / "universe.db"), ScoreParams(workers=4))
    assert (one.scored, many.scored) == (2, 2)
    assert _comparable(_rows(_settings(serial, serial / "universe.db"))) == _comparable(
        _rows(_settings(parallel, parallel / "universe.db"))
    )


# -- no lookahead ---------------------------------------------------------------------------


def test_the_prices_are_read_at_the_filings_own_period_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only dated reads outside the filing's own rows are the period-end close and the cap on
    that date -- never a later bar (the withholding decision, T-153 part 3d A3)."""
    seen: dict[str, Any] = {}
    real_close = scoring.close_on_or_before
    real_caps = scoring.market_caps_as_of

    def close(conn: Any, asset_id: int, period_end: str, **kwargs: Any) -> Any:
        seen["close"] = (asset_id, period_end)
        return real_close(conn, asset_id, period_end, **kwargs)

    def caps(conn: Any, asset_ids: list[int], *, as_of: str, **kwargs: Any) -> Any:
        seen["caps"] = (tuple(asset_ids), as_of)
        return real_caps(conn, asset_ids, as_of=as_of, **kwargs)

    monkeypatch.setattr(scoring, "close_on_or_before", close)
    monkeypatch.setattr(scoring, "market_caps_as_of", caps)
    settings = _seed_db(tmp_path)
    report = run_score(settings, ScoreParams(rule_based=True))
    assert report.scored == 1
    assert seen["close"] == (1, "2023-09-30")
    assert seen["caps"] == ((1,), "2023-09-30")
    # and the filing is usable only from its availability day
    assert _rows(settings)[0]["available_at"] > "2023-09-30"


# -- the command line -----------------------------------------------------------------------


def test_the_cli_prints_the_final_json_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _seed_db(tmp_path)
    monkeypatch.setenv("KG_FINANCIAL_DB", str(settings.db_path))
    code = cli.main(
        [
            "score",
            "--db",
            str(settings.db_path),
            "--universe-db",
            str(settings.universe_db_path),
            "--rule-based",
            "--dry-run",
        ]
    )
    assert code == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["scored"] == 0
    assert payload["filings"] == 1
    assert payload["model"] == FALLBACK_MODEL_LABEL
    assert payload["dry_run"] is True
    assert set(payload) >= {"scored", "skipped", "failed", "model", "seconds"}


def test_the_cli_refuses_a_bad_date_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _seed_db(tmp_path)
    dates = tmp_path / "dates.txt"
    dates.write_text("2023-06-01\nnot-a-date\n")
    code = cli.main(
        [
            "score",
            "--db",
            str(settings.db_path),
            "--universe-db",
            str(settings.universe_db_path),
            "--rule-based",
            "--dry-run",
            "--for-dates",
            str(dates),
        ]
    )
    assert code == 2
    assert "not an ISO date" in capsys.readouterr().err


def test_a_production_database_path_is_refused(tmp_path: Path) -> None:
    """`score` writes FUNDAMENTAL rows; every database under the production root is read-only
    (AGENTS.md), so the command refuses that path before opening it -- as WP-A's driver does."""
    settings = _seed_db(tmp_path)
    guarded = settings.model_copy(update={"db_path": scoring.PRODUCTION_DATA_ROOT / "financial.db"})
    with pytest.raises(ValueError, match="read-only"):
        run_score(guarded, ScoreParams(rule_based=True))


def test_a_dry_run_marks_a_filing_that_will_be_unscored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D1's coverage rule stops the model before it is called, so an unscorable filing costs no
    prompt and no call -- the estimate the director prices the run with must say so."""
    settings = _seed_db(tmp_path)
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM fundamental_metrics WHERE metric_group = 'growth'")
        conn.commit()
    finally:
        conn.close()
    report = run_score(settings, ScoreParams(dry_run=True))
    assert report.unscored == 1
    assert report.model_calls == 0
    assert report.prompt_chars == 0
    out = capsys.readouterr().out
    assert "unscored (D1): no model call" in out
    assert "prompt~" not in out
