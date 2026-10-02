"""Pricing collector pipeline: full stubbed run, by-year windows, resume."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import write_universe_db

from kg_schema.provenance import DirtyTree
from pricing_agent import cli, pipeline
from pricing_agent.config import Settings
from pricing_agent.pipeline import RunParams
from pricing_agent.pricing_client import Candle, DailyPrices

_UNIVERSE_SYMS = ["AAPL", "MSFT"]


def _series() -> list[Candle]:
    rows: list[Candle] = []
    for year, base in ((2022, 100.0), (2023, 150.0)):
        for day, bump in enumerate((0.0, 3.0, -2.0, 5.0), start=3):
            close = base + bump
            rows.append(
                Candle(
                    date=f"{year}-01-{day:02d}",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000.0,
                    source="yfinance",
                )
            )
    return rows


class _FakePricingClient:
    def __init__(self, *_a: object, **_k: object) -> None: ...

    def __enter__(self) -> _FakePricingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def daily_any_spelling(self, ticker: str, start: str, end: str) -> DailyPrices:
        return DailyPrices(
            ticker=ticker,
            start_date=start,
            end_date=end,
            source="yfinance",
            candles=_series(),
            warning=None,
        )


@pytest.fixture
def _stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "PricingClient", _FakePricingClient)


def _settings(tmp_path: Path) -> Settings:
    udb = write_universe_db(
        tmp_path / "universe.db", [(s, "2020-01-01", None) for s in _UNIVERSE_SYMS]
    )
    return Settings(
        db_path=tmp_path / "kg.db",
        universe_db_path=udb,
        pricing_base_url="http://pricing.test",
    )


@pytest.mark.usefixtures("_stubbed")
def test_run_writes_full_and_by_year_windows_then_resumes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    params = RunParams(start_date="2022-01-01", end_date="2023-12-31", by_year=True)

    first = pipeline.run(settings, params)
    assert first.completed == 2
    assert first.failed == 0

    conn = sqlite3.connect(settings.db_path)
    rows = list(
        conn.execute(
            "SELECT a.ticker, w.label, w.first_close, w.last_close "
            "FROM price_window w JOIN assets a ON a.id = w.asset_id "
            "ORDER BY a.ticker, w.label"
        )
    )
    labels = {(r[0], r[1]) for r in rows}
    assert labels == {
        ("AAPL", "2022"),
        ("AAPL", "2023"),
        ("AAPL", "full"),
        ("MSFT", "2022"),
        ("MSFT", "2023"),
        ("MSFT", "full"),
    }
    aapl_2022 = next(r for r in rows if r[0] == "AAPL" and r[1] == "2022")
    assert aapl_2022[2] == 100.0  # first close of the 2022 slice
    assert aapl_2022[3] == 105.0  # last close of the 2022 slice

    run_row = conn.execute(
        "SELECT status, completed_units FROM pricing_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert run_row[0] == "completed"

    second = pipeline.run(settings, params)
    assert second.completed == 0
    assert second.skipped == 2
    assert conn.execute("SELECT COUNT(*) FROM price_window").fetchone()[0] == 6


@pytest.mark.usefixtures("_stubbed")
def test_store_daily_flag_persists_bars(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline.run(
        settings,
        RunParams(start_date="2022-01-01", end_date="2023-12-31", store_daily=True, limit=1),
    )
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] == len(_series())


@pytest.mark.usefixtures("_stubbed")
def test_observations_without_store_daily_is_refused(tmp_path: Path) -> None:
    """T-110: an observation is an analytic over a specific stored price_daily bar --
    writing it without also storing that bar leaves an orphan observation date."""
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="store_daily"):
        pipeline.run(
            settings,
            RunParams(start_date="2022-01-01", end_date="2023-12-31", observations=True),
        )
    # refused before opening the DB at all -- no pricing_run row, nothing written
    assert not settings.db_path.exists()


@pytest.mark.usefixtures("_stubbed")
def test_observations_together_with_store_daily_leaves_no_orphan(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pipeline.run(
        settings,
        RunParams(
            start_date="2022-01-01",
            end_date="2023-12-31",
            store_daily=True,
            observations=True,
            limit=1,
        ),
    )
    conn = sqlite3.connect(settings.db_path)
    daily_dates = {r[0] for r in conn.execute("SELECT date FROM price_daily")}
    obs_dates = {r[0] for r in conn.execute("SELECT obs_date FROM price_observation")}
    assert obs_dates <= daily_dates
    assert obs_dates  # the fixture's series is non-empty, so this isn't vacuous


def test_the_cli_refuses_observations_without_store_daily(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "--observations"])
    assert exc.value.code == 2
    assert "--store-daily" in capsys.readouterr().err


# -- clean-tree guard (T-114) --------------------------------------------


def test_run_refuses_a_dirty_code_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(pipeline, "code_version", lambda: "deadbee-dirty")

    with pytest.raises(DirtyTree, match="deadbee-dirty"):
        pipeline.run(settings, RunParams(analysis_date="2023-12-31"))

    # refused right after ensure_schema, before the universe is even loaded
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM pricing_run").fetchone()[0] == 0


@pytest.mark.usefixtures("_stubbed")
def test_allow_dirty_overrides_the_guard_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path).model_copy(update={"allow_dirty": True})
    monkeypatch.setattr(pipeline, "code_version", lambda: "deadbee-dirty")

    report = pipeline.run(settings, RunParams(start_date="2022-01-01", end_date="2023-12-31"))

    assert report.dirty_tree_bypassed is not None
    assert "deadbee-dirty" in report.dirty_tree_bypassed
    conn = sqlite3.connect(settings.db_path)
    params = json.loads(
        conn.execute("SELECT params_json FROM pricing_run ORDER BY id DESC LIMIT 1").fetchone()[0]
    )
    assert params["dirty_tree_bypassed"] == report.dirty_tree_bypassed


@pytest.mark.usefixtures("_stubbed")
def test_an_interrupted_run_is_marked_failed_not_left_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_run_task", _interrupt)
    settings = _settings(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(settings, RunParams(start_date="2022-01-01", end_date="2023-12-31"))
    conn = sqlite3.connect(settings.db_path)
    status, finished = conn.execute(
        "SELECT status, finished_at FROM pricing_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert status == "failed" and finished is not None
